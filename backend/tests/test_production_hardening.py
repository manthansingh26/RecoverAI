"""Tests for Milestone 13A — Critical Production Hardening.

Covers:
- P0-2: Webhook payload size limit (Content-Length fast path + streaming cap)
- P0-3: Configurable CORS origins (per-environment behavior + safe parsing)

The P0-1 recovery-checkout concurrency tests live in test_recovery_resolver.py.
"""

import json

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from app.api.routes.webhooks import _read_webhook_body
from app.core.config import Settings, settings
from app.main import _get_cors_origins, app
from tests.conftest import make_valid_payment_failed_body, make_razorpay_signature


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _exact_length_payment_failed_body(target_len: int) -> bytes:
    """Build a valid payment.failed JSON body of an exact byte length.

    Adds an innocuous `_pad` string field sized so the serialized body is
    exactly `target_len` bytes. The normalizer ignores unknown top-level fields.
    """
    base = make_valid_payment_failed_body()
    skeleton = {**base, "_pad": ""}
    base_encoded = json.dumps(skeleton, separators=(",", ":"))
    pad_len = target_len - len(base_encoded)
    assert pad_len >= 0, (
        f"target_len {target_len} too small; base body is {len(base_encoded)} bytes"
    )
    skeleton["_pad"] = "x" * pad_len
    body = json.dumps(skeleton, separators=(",", ":")).encode()
    assert len(body) == target_len
    return body


class _FakeStreamRequest:
    """Minimal starlette-Request stand-in exposing headers + async stream().

    Uses an async-iterator class (not an async generator) so abandoned
    iterations on the 413 path do not emit "coroutine was never awaited"
    runtime warnings.
    """

    def __init__(self, chunks, headers=None):
        self._chunks = chunks
        self.headers = headers if headers is not None else {}

    def stream(self):
        return _AsyncChunkIterator(self._chunks)


class _AsyncChunkIterator:
    """Async iterator over a sync iterable of bytes chunks."""

    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration


class _NeverStreamedChunks:
    """Raises if ever iterated — proves the fast path skips body reads."""

    def __iter__(self):
        raise AssertionError("body must not be streamed on early rejection")


# ---------------------------------------------------------------------------
# P0-2: Webhook payload size limit — unit tests for _read_webhook_body
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestWebhookBodySizeLimitUnit:
    """Unit tests for the guarded body reader in isolation."""

    async def test_read_body_returns_exact_raw_bytes(self) -> None:
        """Without a Content-Length header the body is returned byte-for-byte."""
        req = _FakeStreamRequest([b'{"a":', b"1}"])
        body = await _read_webhook_body(req, max_bytes=1024)
        assert body == b'{"a":1}'

    async def test_content_length_over_limit_rejected_early(self) -> None:
        """A declared Content-Length above the limit is rejected before streaming."""
        req = _FakeStreamRequest(
            _NeverStreamedChunks(), {"content-length": "100"}
        )
        with pytest.raises(HTTPException) as excinfo:
            await _read_webhook_body(req, max_bytes=10)
        assert excinfo.value.status_code == 413

    async def test_stream_cap_rejects_oversize_without_content_length(self) -> None:
        """Chunked / missing Content-Length bodies are capped by the stream guard."""
        req = _FakeStreamRequest([b"abc", b"de"])
        with pytest.raises(HTTPException) as excinfo:
            await _read_webhook_body(req, max_bytes=4)
        assert excinfo.value.status_code == 413

    async def test_stream_cap_rejects_lying_content_length(self) -> None:
        """A lying (too-small) Content-Length is overruled by the stream guard."""
        req = _FakeStreamRequest([b"abc", b"de"], {"content-length": "4"})
        with pytest.raises(HTTPException) as excinfo:
            await _read_webhook_body(req, max_bytes=4)
        assert excinfo.value.status_code == 413

    async def test_exact_boundary_accepted(self) -> None:
        """A body exactly at the limit is accepted (not rejected)."""
        req = _FakeStreamRequest([b"abcd"])
        body = await _read_webhook_body(req, max_bytes=4)
        assert body == b"abcd"

    async def test_malformed_content_length_falls_through_to_stream(self) -> None:
        """A non-numeric Content-Length is ignored; the stream cap still applies."""
        req = _FakeStreamRequest([b'{"ok":1}'], {"content-length": "not-a-number"})
        body = await _read_webhook_body(req, max_bytes=1024)
        assert body == b'{"ok":1}'


class TestWebhookSettings:
    """Sync-only settings/configuration assertions."""

    def test_config_default_is_1_mib(self) -> None:
        """RAZORPAY_WEBHOOK_MAX_BODY_BYTES defaults to 1 MiB."""
        assert settings.RAZORPAY_WEBHOOK_MAX_BODY_BYTES == 1048576

    def test_invalid_config_rejected(self) -> None:
        """A non-positive webhook body limit is rejected at settings load."""
        with pytest.raises(ValidationError):
            Settings(RAZORPAY_WEBHOOK_MAX_BODY_BYTES=0)


# ---------------------------------------------------------------------------
# P0-2: Webhook payload size limit — integration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestWebhookBodySizeLimitIntegration:
    """End-to-end size enforcement on POST /webhooks/razorpay."""

    async def _post(self, client, body_bytes, sig, event_id, content_type="application/json"):
        return await client.post(
            "/webhooks/razorpay",
            content=body_bytes,
            headers={
                "Content-Type": content_type,
                "X-Razorpay-Signature": sig,
                "x-razorpay-event-id": event_id,
            },
        )

    async def test_normal_signed_payload_still_works(self, db_session) -> None:
        """A legitimate signed payload under the default limit is accepted."""
        secret = "hardening_webhook_secret"
        body_bytes = json.dumps(make_valid_payment_failed_body()).encode()
        sig = make_razorpay_signature(body_bytes, secret)

        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        original_max = settings.RAZORPAY_WEBHOOK_MAX_BODY_BYTES
        settings.RAZORPAY_WEBHOOK_SECRET = secret
        settings.RAZORPAY_WEBHOOK_MAX_BODY_BYTES = 1048576
        try:
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await self._post(client, body_bytes, sig, "evt_size_ok_001")
            assert resp.status_code == 200, resp.text
            assert resp.json()["accepted"] is True
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret
            settings.RAZORPAY_WEBHOOK_MAX_BODY_BYTES = original_max

    async def test_content_length_over_limit_rejected(self, db_session) -> None:
        """A payload whose declared length exceeds the limit returns 413."""
        original_max = settings.RAZORPAY_WEBHOOK_MAX_BODY_BYTES
        settings.RAZORPAY_WEBHOOK_MAX_BODY_BYTES = 2048
        try:
            body_bytes = _exact_length_payment_failed_body(4096)
            # Signature is intentionally bogus: the size check runs BEFORE
            # signature verification, so the request must be rejected as 413,
            # not 401.
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await self._post(
                    client, body_bytes, "invalid_sig", "evt_size_over_001"
                )
            assert resp.status_code == 413, resp.text
        finally:
            settings.RAZORPAY_WEBHOOK_MAX_BODY_BYTES = original_max

    async def test_exact_boundary_accepted(self, db_session) -> None:
        """A signed payload exactly at the limit is accepted (200)."""
        secret = "hardening_boundary_secret"
        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        original_max = settings.RAZORPAY_WEBHOOK_MAX_BODY_BYTES
        settings.RAZORPAY_WEBHOOK_SECRET = secret
        settings.RAZORPAY_WEBHOOK_MAX_BODY_BYTES = 2048
        try:
            body_bytes = _exact_length_payment_failed_body(2048)
            sig = make_razorpay_signature(body_bytes, secret)
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await self._post(client, body_bytes, sig, "evt_size_boundary_001")
            assert resp.status_code == 200, resp.text
            assert resp.json()["accepted"] is True
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret
            settings.RAZORPAY_WEBHOOK_MAX_BODY_BYTES = original_max

    async def test_over_limit_rejected_before_signature_verification(
        self, db_session
    ) -> None:
        """Oversized bodies get 413 even with a VALID signature present.

        Proves the size guard short-circuits before HMAC verification and
        before any JSON parsing, preserving the "raw bytes verified later"
        semantics for legitimate in-limit payloads.
        """
        secret = "hardening_oversize_sig_secret"
        original_secret = settings.RAZORPAY_WEBHOOK_SECRET
        original_max = settings.RAZORPAY_WEBHOOK_MAX_BODY_BYTES
        settings.RAZORPAY_WEBHOOK_SECRET = secret
        settings.RAZORPAY_WEBHOOK_MAX_BODY_BYTES = 2048
        try:
            body_bytes = _exact_length_payment_failed_body(4096)
            sig = make_razorpay_signature(body_bytes, secret)  # valid over raw bytes
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await self._post(client, body_bytes, sig, "evt_size_over_sig_001")
            assert resp.status_code == 413, resp.text
        finally:
            settings.RAZORPAY_WEBHOOK_SECRET = original_secret
            settings.RAZORPAY_WEBHOOK_MAX_BODY_BYTES = original_max

    async def test_oversized_malformed_body_rejected(self, db_session) -> None:
        """A malformed/oversized body is rejected by size before JSON parsing."""
        original_max = settings.RAZORPAY_WEBHOOK_MAX_BODY_BYTES
        settings.RAZORPAY_WEBHOOK_MAX_BODY_BYTES = 1024
        try:
            body_bytes = b"x" * 4096  # not valid JSON, but oversized
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                resp = await self._post(client, body_bytes, "somesig", "evt_size_malformed_001")
            assert resp.status_code == 413, resp.text
        finally:
            settings.RAZORPAY_WEBHOOK_MAX_BODY_BYTES = original_max


# ---------------------------------------------------------------------------
# P0-3: Configurable CORS origins
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestCorsConfig:
    """Per-environment CORS behavior from _get_cors_origins()."""

    async def test_development_keeps_local_origins(self, db_session) -> None:
        """Development still allows the local frontend origins."""
        original_env = settings.APP_ENV
        settings.APP_ENV = "development"
        try:
            origins = _get_cors_origins()
            assert "http://localhost:3000" in origins
            assert "http://127.0.0.1:3000" in origins
            assert "http://localhost:5173" in origins
            assert "http://127.0.0.1:5173" in origins
        finally:
            settings.APP_ENV = original_env

    async def test_test_env_keeps_local_origins(self, db_session) -> None:
        """Test environment still allows the local frontend origins."""
        original_env = settings.APP_ENV
        settings.APP_ENV = "test"
        try:
            origins = _get_cors_origins()
            assert "http://localhost:3000" in origins
        finally:
            settings.APP_ENV = original_env

    async def test_production_uses_only_configured_origins(self, db_session) -> None:
        """Production allows ONLY the origins in CORS_ORIGINS."""
        original_env = settings.APP_ENV
        original_cors = settings.CORS_ORIGINS
        settings.APP_ENV = "production"
        settings.CORS_ORIGINS = (
            "https://app.recoverai.example.com, https://admin.recoverai.example.com"
        )
        try:
            origins = _get_cors_origins()
            assert origins == [
                "https://app.recoverai.example.com",
                "https://admin.recoverai.example.com",
            ]
        finally:
            settings.APP_ENV = original_env
            settings.CORS_ORIGINS = original_cors

    async def test_production_empty_config_disables_all(self, db_session) -> None:
        """Production with empty CORS_ORIGINS allows no cross-origin access."""
        original_env = settings.APP_ENV
        original_cors = settings.CORS_ORIGINS
        settings.APP_ENV = "production"
        settings.CORS_ORIGINS = ""
        try:
            assert _get_cors_origins() == []
        finally:
            settings.APP_ENV = original_env
            settings.CORS_ORIGINS = original_cors


class TestCorsOriginParsing:
    """Sync-only assertions on CORS_ORIGINS parsing and validation."""

    def test_parser_handles_whitespace_and_empty_entries(self) -> None:
        """CORS_ORIGINS parsing is safe for padding and trailing commas."""
        s = Settings(CORS_ORIGINS=" , https://a.example.com ,,")
        assert s.cors_origins_list == ["https://a.example.com"]

    def test_parser_returns_empty_for_blank(self) -> None:
        assert Settings(CORS_ORIGINS="").cors_origins_list == []
        assert Settings(CORS_ORIGINS="  ,,  ").cors_origins_list == []

    def test_wildcard_origin_rejected(self) -> None:
        """'*' must never be accepted as a CORS origin (credentials are used)."""
        with pytest.raises(ValidationError):
            Settings(CORS_ORIGINS="*")
