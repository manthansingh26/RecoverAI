"""Structured JSON logging and per-request correlation ID support.

Milestone 15B: close the operational observability gap.

- ``configure_logging()`` wires the root logger to a JSON formatter that emits
  one JSON object per line (timestamp, level, logger, message, correlation_id,
  optional exception info). It honours ``settings.LOG_LEVEL``.
- ``CorrelationIdMiddleware`` is a pure-ASGI middleware that assigns or
  propagates an ``X-Request-ID`` and exposes it as ``request.state.correlation_id``
  and via a ``contextvars.ContextVar`` consumed by ``JsonFormatter``.

CRITICAL WEBHOOK INVARIANT
--------------------------
This middleware MUST NOT read ``request.body()`` or ``request.stream()``, and
MUST NOT parse JSON. The webhook pipeline consumes the raw body itself
(body-size cap -> raw body -> HMAC verification -> event ID -> JSON parse ->
freshness -> processing). Any body consumption here would corrupt HMAC
verification semantics. This middleware touches only headers and scope state.
"""

import contextvars
import json
import logging
import re
import secrets
import sys
from datetime import datetime, timezone
from typing import Any, Callable

from app.core.config import settings

# ---------------------------------------------------------------------------
# Correlation ID context
# ---------------------------------------------------------------------------

X_REQUEST_ID = "x-request-id"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)


def current_correlation_id() -> str | None:
    """Return the correlation ID active in the current execution context."""
    return _correlation_id.get()


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line.

    Includes: ISO-8601 UTC timestamp, level, logger name, message, and the
    active correlation ID (when present). Exception tracebacks are serialized
    as a string field so they remain a single JSON object.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        corr = _correlation_id.get()
        if corr:
            payload["correlation_id"] = corr
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


_configured = False


def _resolve_level(level_name: str) -> int:
    """Map a LOG_LEVEL string to a logging level, defaulting to INFO."""
    return getattr(logging, level_name.upper(), logging.INFO)


def configure_logging(*, force: bool = False) -> None:
    """Configure the root logger once with structured JSON output.

    Idempotent: a second call is a no-op unless ``force=True`` (used by tests
    to re-apply a different level). Handlers are replaced, not appended, so
    uvicorn reload / repeated calls never duplicate output.
    """
    global _configured
    if _configured and not force:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(_resolve_level(settings.LOG_LEVEL))
    _configured = True


# ---------------------------------------------------------------------------
# Correlation ID middleware (pure ASGI — never touches the request body)
# ---------------------------------------------------------------------------


class CorrelationIdMiddleware:
    """Assign or propagate an ``X-Request-ID`` for every HTTP request.

    Rules:
    - If the incoming ``X-Request-ID`` header exists and matches the sane-ID
      pattern, it is propagated.
    - Otherwise a cryptographically random request ID is generated.
    - The ID is stored in ``scope["state"]["correlation_id"]`` (readable as
      ``request.state.correlation_id``), set as the active context var for the
      request's logging, and echoed on the ``X-Request-ID`` response header.

    The middleware is intentionally a pure-ASGI wrapper: it only inspects the
    HTTP headers and never calls ``receive``, so the webhook's raw-body HMAC
    pipeline is completely unaffected.
    """

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        incoming = _extract_header(scope, X_REQUEST_ID)
        if incoming and _REQUEST_ID_PATTERN.match(incoming):
            corr = incoming
        else:
            corr = secrets.token_urlsafe(16)

        # Make it available to routes via request.state.correlation_id.
        scope.setdefault("state", {})["correlation_id"] = corr

        token = _correlation_id.set(corr)
        try:
            await self.app(scope, receive, _header_injecting_send(send, corr))
        finally:
            _correlation_id.reset(token)


def _extract_header(scope: dict, name: str) -> str:
    """Pull a header value from the ASGI scope (lowercase bytes keys)."""
    target = name.lower().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key == target:
            return value.decode("latin-1")
    return ""


def _header_injecting_send(send: Callable, corr: str) -> Callable:
    """Wrap ``send`` to append the X-Request-ID on the response start message."""
    header_bytes = X_REQUEST_ID.encode("latin-1")
    corr_bytes = corr.encode("latin-1")

    async def wrapped(message: dict) -> None:
        if message.get("type") == "http.response.start":
            headers = list(message.get("headers", []))
            headers.append((header_bytes, corr_bytes))
            message["headers"] = headers
        await send(message)

    return wrapped
