"""Stage 6 transport: our own ``LLMClient`` protocol so the engine never imports LiteLLM.

Tests use ``FakeLLMClient``; production uses ``LiteLLMClient`` (Gemini primary, Groq fallback).
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

DEFAULT_CONTEXT_WINDOW = 128_000

Message = dict[str, str]


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: int = 0


class LLMError(Exception):
    """The provider call failed. Messages are safe to store (no keys, no prompt content)."""


class LLMContextWindowError(LLMError):
    """The prompt did not fit the model's context window."""


class LLMClient(Protocol):
    async def generate(
        self, *, model: str, messages: Sequence[Message], json_schema: dict[str, Any] | None
    ) -> LLMResponse: ...

    def context_window(self, model: str) -> int: ...


# ---------------------------------------------------------------- LiteLLM


class LiteLLMClient:
    def __init__(
        self,
        *,
        api_keys: dict[str, str] | None = None,  # provider prefix -> key, e.g. {"gemini": "..."}
        timeout_seconds: float = 90.0,
        max_output_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> None:
        self._api_keys = {k: v for k, v in (api_keys or {}).items() if v}
        self._timeout = timeout_seconds
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature

    def _api_key(self, model: str) -> str | None:
        return self._api_keys.get(model.split("/", 1)[0])

    def context_window(self, model: str) -> int:
        import litellm

        try:
            info = litellm.get_model_info(model)
        except Exception:
            return DEFAULT_CONTEXT_WINDOW
        return int(info.get("max_input_tokens") or DEFAULT_CONTEXT_WINDOW)

    @staticmethod
    def _response_format(model: str, schema: dict[str, Any] | None) -> dict[str, Any]:
        import litellm

        if schema is not None:
            try:
                if litellm.supports_response_schema(model=model):
                    return {
                        "type": "json_schema",
                        "json_schema": {"name": "ReleaseReviewReport", "schema": schema},
                    }
            except Exception:
                pass
        return {"type": "json_object"}  # the schema is also in the system prompt

    async def generate(
        self, *, model: str, messages: Sequence[Message], json_schema: dict[str, Any] | None
    ) -> LLMResponse:
        import litellm
        from litellm.exceptions import ContextWindowExceededError

        started = time.monotonic()
        try:
            response = await litellm.acompletion(
                model=model,
                messages=list(messages),
                response_format=self._response_format(model, json_schema),
                temperature=self._temperature,
                max_tokens=self._max_output_tokens,
                timeout=self._timeout,
                api_key=self._api_key(model),
                seed=7,
                drop_params=True,  # e.g. seed is not supported everywhere
                num_retries=0,  # the engine owns retry/fallback
            )
        except ContextWindowExceededError as exc:
            raise LLMContextWindowError(f"{model}: prompt exceeds the context window") from exc
        except Exception as exc:
            raise LLMError(f"{model}: {type(exc).__name__}") from exc

        try:
            cost: float | None = float(litellm.completion_cost(completion_response=response))
        except Exception:
            cost = None
        usage = getattr(response, "usage", None)
        return LLMResponse(
            text=response.choices[0].message.content or "",
            model=model,
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            cost_usd=cost,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


# ---------------------------------------------------------------- fake


@dataclass
class FakeCall:
    model: str
    messages: list[Message]
    json_schema: dict[str, Any] | None


@dataclass
class FakeLLMClient:
    """Scripted responses: each item is a JSON string (returned) or an exception (raised)."""

    script: list[str | Exception]
    context_windows: dict[str, int] = field(default_factory=dict)
    calls: list[FakeCall] = field(default_factory=list)
    prompt_tokens: int = 1000
    completion_tokens: int = 200

    def context_window(self, model: str) -> int:
        return self.context_windows.get(model, DEFAULT_CONTEXT_WINDOW)

    async def generate(
        self, *, model: str, messages: Sequence[Message], json_schema: dict[str, Any] | None
    ) -> LLMResponse:
        self.calls.append(FakeCall(model, list(messages), json_schema))
        if not self.script:
            raise AssertionError("FakeLLMClient script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResponse(
            text=item,
            model=model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cost_usd=0.001,
            latency_ms=5,
        )
