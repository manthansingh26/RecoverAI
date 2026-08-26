"""Provider-agnostic LLM abstraction for the RecoverAI advisory layer.

The ``LLMProvider`` class wraps the concrete vendor SDK (initial: Anthropic
Claude) behind a ``call()`` method that returns structured JSON, with built-in
timeout, retry, and exception handling. The rest of the agent package never
imports ``anthropic`` directly — only this module does.

Safety properties:
- ``call()`` always returns ``LLMResult`` (never raises to the caller).
- A malformed, empty, or timed-out response produces ``LLMResult(error=...)``.
- The caller (diagnostician / recommender) is responsible for schema validation
  and deterministic fallback.
- No API key or secrets are ever logged.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------

@dataclass
class LLMResult:
    """Unified return type for every LLM provider call.

    Exactly one of ``content`` or ``error`` is non-None.
    """

    content: dict[str, Any] | None = None
    error: str | None = None


T = TypeVar("T")


@dataclass
class AdvisoryResult(Generic[T]):
    """Typed result of an advisory operation (diagnosis or recommendation).

    Provides explicit, machine-readable provenance so downstream code never
    has to infer whether AI was used by inspecting reasoning text.

    Attributes:
        value: The validated structured result (Diagnosis or Recommendation).
        used_llm: True when the LLM produced this result (not fallback).
        fallback_used: True when the deterministic fallback was used because
            the LLM was unavailable, errored, timed out, or returned invalid
            output.
        provider: The LLM provider name (e.g. "anthropic") or None when the
            deterministic path was used and no provider was involved.
        model: The LLM model ID that produced this result, or None when the
            deterministic path was used.
        prompt_version: The prompt template version used.
        confidence: The validated confidence (0.0-1.0).
    """

    value: T
    used_llm: bool
    fallback_used: bool
    provider: str | None
    model: str | None
    prompt_version: str
    confidence: float

    @property
    def source(self) -> str:
        """Explicit source label: "ai" when the LLM produced the result,
        "deterministic" otherwise."""
        return "ai" if self.used_llm else "deterministic"


# ---------------------------------------------------------------------------
# Provider abstraction
# ---------------------------------------------------------------------------

class LLMProvider:
    """Thread-safe, provider-agnostic LLM wrapper.

    Supports Anthropic Claude, OpenAI, and Google Gemini providers configured
    via ``settings.LLM_PROVIDER`` ("gemini" | "openai" | "anthropic"). The
    provider key is resolved at call time (not init time) so it can be
    overridden in tests without re-creating the provider.
    """

    def __init__(self, model: str) -> None:
        self.model = model

    @staticmethod
    def _get_api_key(provider: str) -> str:
        """Resolve the API key for the active provider."""
        if provider == "gemini":
            return settings.GEMINI_API_KEY or settings.LLM_API_KEY
        if provider == "openai":
            return settings.OPENAI_API_KEY or settings.LLM_API_KEY
        if provider == "anthropic":
            return settings.ANTHROPIC_API_KEY or settings.LLM_API_KEY
        return settings.LLM_API_KEY

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def call(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        response_format: type | None = None,
    ) -> LLMResult:
        """Call the configured LLM provider and return a structured result.

        Args:
            system_prompt: System-level instruction (role/constraints).
            user_prompt: The per-request context/question.
            max_tokens: Maximum output tokens.
            temperature: Sampling temperature (0.0 = deterministic).
            response_format: Unused in this provider (JSON parsing is
                handled by the caller). Included for interface compatibility.

        Returns:
            LLMResult with parsed JSON content or error description.
        """
        del response_format  # unused; JSON parsing handled below
        provider = (settings.LLM_PROVIDER or "anthropic").lower()
        key = self._get_api_key(provider)
        if not key:
            return LLMResult(error="LLM_API_KEY is not configured")

        try:
            if provider == "gemini":
                content = self._do_gemini_call(key, system_prompt, user_prompt, max_tokens, temperature)
            elif provider == "openai":
                content = self._do_openai_call(key, system_prompt, user_prompt, max_tokens, temperature)
            else:
                content = self._do_anthropic_call(key, system_prompt, user_prompt, max_tokens, temperature)
        except Exception as exc:
            # Log only the exception TYPE, never the message — provider error
            # strings could theoretically contain sensitive material.
            logger.warning("LLM provider call failed (%s)", type(exc).__name__)
            return LLMResult(error="provider_error")

        if not content or not content.strip():
            return LLMResult(error="empty_response")

        cleaned_content = content.strip()
        if cleaned_content.startswith("```"):
            lines = cleaned_content.splitlines()
            if len(lines) >= 2 and lines[0].startswith("```"):
                if lines[-1].strip() == "```":
                    cleaned_content = "\n".join(lines[1:-1]).strip()
                else:
                    cleaned_content = "\n".join(lines[1:]).strip()

        # Attempt JSON parse.
        try:
            parsed = json.loads(cleaned_content)
        except json.JSONDecodeError:
            logger.warning("LLM returned non-JSON content")
            return LLMResult(error="non_json_response")

        if not isinstance(parsed, dict):
            return LLMResult(error="non_dict_json")

        return LLMResult(content=parsed)

    # ------------------------------------------------------------------
    # Concrete provider implementations
    # ------------------------------------------------------------------

    def _do_anthropic_call(
        self,
        api_key: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Execute Anthropic Claude API call with timeout and retry."""
        import anthropic

        timeout_s = max(settings.LLM_TIMEOUT_SECONDS, 5)
        deadline = time.monotonic() + timeout_s

        last_exc: Exception | None = None
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining < 5:
                remaining = 5  # give each attempt a minimum window

            try:
                client = anthropic.Anthropic(api_key=api_key)
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                    "max_tokens": max_tokens,
                    "timeout": min(remaining, timeout_s),
                }
                if temperature is not None:
                    kwargs["extra_body"] = {"temperature": temperature}

                resp = client.messages.create(**kwargs)
                text_parts = [
                    b.text for b in resp.content if hasattr(b, "text")
                ]
                return "".join(text_parts) if text_parts else ""
            except anthropic.APIStatusError as e:
                logger.warning("Anthropic API error (attempt %d): %s", attempt + 1, e)
                last_exc = e
                if e.status_code in (400, 401, 403):
                    break  # non-retryable
                time.sleep(1.5 ** attempt)
            except Exception as e:
                logger.warning("Anthropic call exception (attempt %d): %s", attempt + 1, e)
                last_exc = e
                time.sleep(1.5 ** attempt)

        raise last_exc or RuntimeError("Anthropic call failed after 3 attempts")

    def _do_openai_call(
        self,
        api_key: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Execute OpenAI API call with timeout and retry."""
        from openai import OpenAI, APIStatusError

        timeout_s = max(settings.LLM_TIMEOUT_SECONDS, 5)
        deadline = time.monotonic() + timeout_s

        last_exc: Exception | None = None
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining < 5:
                remaining = 5  # give each attempt a minimum window

            try:
                client = OpenAI(
                    api_key=api_key,
                    base_url=settings.OPENAI_BASE_URL or None,
                    timeout=min(remaining, timeout_s),
                )
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
                kwargs: dict[str, Any] = {
                    "model": self.model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                }
                # o-series models (e.g. o3-mini) do not accept custom temperature parameter
                if not self.model.startswith("o") and temperature is not None:
                    kwargs["temperature"] = temperature

                resp = client.chat.completions.create(**kwargs)
                if not resp.choices:
                    return ""
                choice = resp.choices[0]
                return choice.message.content or ""
            except APIStatusError as e:
                logger.warning("OpenAI API error (attempt %d): %s", attempt + 1, e)
                last_exc = e
                if e.status_code in (400, 401, 403):
                    break  # non-retryable
                time.sleep(1.5 ** attempt)
            except Exception as e:
                logger.warning("OpenAI call exception (attempt %d): %s", attempt + 1, e)
                last_exc = e
                time.sleep(1.5 ** attempt)

        raise last_exc or RuntimeError("OpenAI call failed after 3 attempts")

    def _do_gemini_call(
        self,
        api_key: str,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """Execute Google Gemini API call via httpx with timeout and retry."""
        import httpx

        timeout_s = max(settings.LLM_TIMEOUT_SECONDS, 5)
        deadline = time.monotonic() + timeout_s

        model_name = self.model.removeprefix("models/")
        base_url = settings.GEMINI_BASE_URL or "https://generativelanguage.googleapis.com"
        url = f"{base_url.rstrip('/')}/v1beta/models/{model_name}:generateContent"

        full_prompt = f"{system}\n\n{user}" if system else user
        payload = {
            "contents": [
                {
                    "parts": [{"text": full_prompt}]
                }
            ],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": temperature if temperature is not None else 0.0,
                "maxOutputTokens": max_tokens,
            },
        }

        last_exc: Exception | None = None
        for attempt in range(3):
            remaining = deadline - time.monotonic()
            if remaining < 5:
                remaining = 5

            try:
                resp = httpx.post(
                    url,
                    params={"key": api_key},
                    json=payload,
                    timeout=min(remaining, timeout_s),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    candidates = data.get("candidates", [])
                    if not candidates:
                        return ""
                    parts = candidates[0].get("content", {}).get("parts", [])
                    return "".join(p.get("text", "") for p in parts if "text" in p)

                logger.warning("Gemini API error (attempt %d): status=%d", attempt + 1, resp.status_code)
                last_exc = RuntimeError(f"Gemini API returned status {resp.status_code}")
                if resp.status_code in (400, 401, 403):
                    break  # non-retryable
                if resp.status_code == 429:
                    time.sleep(12.0 * (attempt + 1))
                else:
                    time.sleep(1.5 ** attempt)
            except Exception as e:
                logger.warning("Gemini call exception (attempt %d): %s", attempt + 1, type(e).__name__)
                last_exc = e
                time.sleep(1.5 ** attempt)

        raise last_exc or RuntimeError("Gemini call failed after 3 attempts")