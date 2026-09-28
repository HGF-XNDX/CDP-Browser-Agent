"""One budget contract for planners, memory and processing workers.

Token counts are conservative estimates, not a provider tokenizer. Server usage
can tighten the estimate only on the same model route.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
import re


def route_key(options: dict) -> str:
    identity = [options.get("baseUrl", "http://localhost:8080/v1").rstrip("/"),
                options.get("provider", "llama.cpp"), options.get("model", ""),
                hashlib.sha256(str(options.get("apiKey", "")).encode()).hexdigest(),
                bool(options.get("trustEnv", False))]
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def estimate_tokens(value: object, chars_per_token: float = 3.0) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    cjk = len(re.findall(r"[\u3400-\u9fff\u3040-\u30ff\uac00-\ud7af]", text))
    return max(1, math.ceil(cjk + (len(text) - cjk) / max(1., float(chars_per_token))))


class ContextBudgetExceeded(ValueError):
    """Required prompt cannot fit without changing the task or instructions."""


class ContextWindowExceeded(RuntimeError):
    """The provider explicitly rejected the context length (not a format error)."""


@dataclass(frozen=True)
class ContextBudget:
    model_capacity_tokens: int | None
    context_window_tokens: int
    reserved_output_tokens: int
    prompt_budget_ratio: float
    available_prompt_tokens: int
    chars_per_token: float
    capacity_source: str
    route: str

    @classmethod
    def from_settings(cls, model=None, agent=None, metrics=None):
        model, agent = model or {}, agent or {}
        capability = model.get("_model_capabilities", {})
        capacity = capability.get("context_window_tokens")
        limits = [int(v) for v in (agent.get("context_window_tokens"), model.get("contextWindowTokens"),
                  model.get("context_window_tokens"), model.get("maxContextTokens")) if v is not None]
        if any(v <= 0 for v in limits):
            raise ValueError("Context limits must be positive integers or null (auto)")
        window = min(([int(capacity)] if capacity else []) + limits) if capacity or limits else 32768
        reserve = max(int(model.get("maxTokens", 2048)), int(agent.get("reserved_output_tokens") or 0))
        ratio = float(agent.get("prompt_budget_ratio", .85))
        if reserve <= 0 or window <= reserve or not 0 < ratio <= 1:
            raise ValueError("Context must exceed positive output reservation; prompt_budget_ratio must be in (0,1]")
        route = route_key(model)
        observed = (metrics or {}).get("model_routes", {}).get(route, {}).get("observed_chars_per_token", 4)
        chars = min(float(agent.get("chars_per_token", 3)), float(observed))
        if not math.isfinite(chars) or chars < 1:
            raise ValueError("chars_per_token must be finite and >= 1")
        return cls(capacity, window, reserve, ratio, int((window - reserve) * ratio), chars,
                   capability.get("source", "explicit" if limits else "fallback_32768"), route)

    def estimate(self, value):
        return estimate_tokens(value, self.chars_per_token)

    def estimate_messages(self, messages):
        total = 3
        for message in messages:
            total += 8
            content = message.get("content", "")
            if isinstance(content, list):
                for part in content:
                    # Image tokenization is provider-specific: reserve conservatively,
                    # never count the base64 URL as ordinary prompt text.
                    total += 8192 if part.get("type") == "image_url" else self.estimate(part)
            else:
                total += self.estimate(content)
        return total

    def check(self, messages):
        estimated = self.estimate_messages(messages)
        if estimated > self.available_prompt_tokens:
            raise ContextBudgetExceeded(f"Prompt estimate {estimated} exceeds prompt budget {self.available_prompt_tokens}; "
                                        "split evidence or increase an explicit soft limit")
        return estimated

    def as_dict(self):
        return asdict(self)
