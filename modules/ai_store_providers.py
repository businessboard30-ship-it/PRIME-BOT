"""
AI Store — provider adapters and pricing.

IMPORTANT: every call in this module runs against YOUR OWN Anthropic/OpenAI/
Gemini API key (env vars below), never a buyer's or seller's personal
account/subscription. Sellers list "personas" (a name + system prompt) that
shape how the assistant talks — they never connect their own credentials.
That's what keeps this legal: API keys are metered and licensed for exactly
this kind of third-party resale, unlike an individual Pro/Plus/Max login.

GHS_PER_CREDIT and the MODELS table are placeholders — recheck against
current Anthropic/OpenAI/Google pricing pages before relying on the margin
math for anything (e.g. future revenue-share features).
"""

import os
import logging
from typing import List, Dict, Tuple

import httpx

logger = logging.getLogger(__name__)

GHS_PER_CREDIT = 0.05  # 1 credit = 0.05 GHS => 20 credits = 1 GHS

# creditsPer1k = what buyers are charged. rawCostPer1k = what the provider
# actually bills you. Kept separate so margin (charged - raw) is always
# computable — useful now for profitability tracking, and load-bearing later
# if a revenue-share feature is ever turned back on.
MODELS: Dict[str, Dict[str, dict]] = {
    "anthropic": {
        "claude-sonnet": {
            "api_model": "claude-sonnet-4-6",
            "input_credits_per_1k": 6,
            "output_credits_per_1k": 30,
            "input_raw_cost_per_1k": 3,
            "output_raw_cost_per_1k": 15,
            "label": "Claude Sonnet — balanced, great default",
        },
        "claude-opus": {
            "api_model": "claude-opus-4-1",
            "input_credits_per_1k": 30,
            "output_credits_per_1k": 150,
            "input_raw_cost_per_1k": 15,
            "output_raw_cost_per_1k": 75,
            "label": "Claude Opus — most capable, priciest",
        },
    },
    "openai": {
        "gpt-4o": {
            "api_model": "gpt-4o",
            "input_credits_per_1k": 5,
            "output_credits_per_1k": 15,
            "input_raw_cost_per_1k": 2.5,
            "output_raw_cost_per_1k": 7.5,
            "label": "GPT-4o — fast, well-rounded",
        },
        "gpt-4o-mini": {
            "api_model": "gpt-4o-mini",
            "input_credits_per_1k": 1,
            "output_credits_per_1k": 4,
            "input_raw_cost_per_1k": 0.5,
            "output_raw_cost_per_1k": 2,
            "label": "GPT-4o mini — cheap quick chat",
        },
    },
    "gemini": {
        "gemini-pro": {
            "api_model": "gemini-1.5-pro",
            "input_credits_per_1k": 5,
            "output_credits_per_1k": 15,
            "input_raw_cost_per_1k": 2.5,
            "output_raw_cost_per_1k": 7.5,
            "label": "Gemini 1.5 Pro — long context",
        },
        "gemini-flash": {
            "api_model": "gemini-1.5-flash",
            "input_credits_per_1k": 1,
            "output_credits_per_1k": 3,
            "input_raw_cost_per_1k": 0.3,
            "output_raw_cost_per_1k": 1,
            "label": "Gemini Flash — cheap & fast",
        },
    },
}


def list_providers() -> List[str]:
    return list(MODELS.keys())


def list_models(provider: str) -> List[Tuple[str, str]]:
    """Returns [(model_key, label), ...] for a provider."""
    return [(k, v["label"]) for k, v in MODELS.get(provider, {}).items()]


def _spec(provider: str, model: str) -> dict:
    spec = MODELS.get(provider, {}).get(model)
    if not spec:
        raise ValueError(f"Unknown provider/model: {provider}/{model}")
    return spec


def estimate_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    spec = _spec(provider, model)
    cost = (input_tokens / 1000) * spec["input_credits_per_1k"] + (output_tokens / 1000) * spec["output_credits_per_1k"]
    import math
    return math.ceil(cost * 100) / 100  # round up, never undercharge


def estimate_raw_cost(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    spec = _spec(provider, model)
    cost = (input_tokens / 1000) * spec["input_raw_cost_per_1k"] + (output_tokens / 1000) * spec["output_raw_cost_per_1k"]
    return round(cost, 2)


def estimate_margin(provider: str, model: str, input_tokens: int, output_tokens: int) -> float:
    charged = estimate_cost(provider, model, input_tokens, output_tokens)
    raw = estimate_raw_cost(provider, model, input_tokens, output_tokens)
    return max(0.0, charged - raw)


def preflight_estimate(provider: str, model: str, input_tokens: int, assumed_max_output: int = 500) -> float:
    return estimate_cost(provider, model, input_tokens, assumed_max_output)


def rough_token_count(text: str) -> int:
    return max(1, len(text) // 4)  # ~4 chars/token heuristic


class ProviderCallError(Exception):
    pass


async def call_anthropic(messages: List[Dict], api_model: str, max_tokens: int = 1024) -> Tuple[str, int, int]:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ProviderCallError("ANTHROPIC_API_KEY is not set")
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": api_model,
                "max_tokens": max_tokens,
                "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            },
        )
        resp.raise_for_status()
        data = resp.json()
    text = "\n".join(b["text"] for b in data.get("content", []) if b.get("type") == "text")
    usage = data.get("usage", {})
    return text, usage.get("input_tokens", 0), usage.get("output_tokens", 0)


async def call_openai(messages: List[Dict], api_model: str, max_tokens: int = 1024) -> Tuple[str, int, int]:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ProviderCallError("OPENAI_API_KEY is not set")
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
            json={
                "model": api_model,
                "max_tokens": max_tokens,
                "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
            },
        )
        resp.raise_for_status()
        data = resp.json()
    choice = data["choices"][0]
    usage = data.get("usage", {})
    return choice["message"]["content"], usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


async def call_gemini(messages: List[Dict], api_model: str, max_tokens: int = 1024) -> Tuple[str, int, int]:
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise ProviderCallError("GEMINI_API_KEY is not set")
    contents = [
        {"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
        for m in messages
    ]
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{api_model}:generateContent",
            params={"key": api_key},
            json={"contents": contents, "generationConfig": {"maxOutputTokens": max_tokens}},
        )
        resp.raise_for_status()
        data = resp.json()
    candidate = (data.get("candidates") or [{}])[0]
    parts = candidate.get("content", {}).get("parts", [])
    text = "\n".join(p.get("text", "") for p in parts)
    usage = data.get("usageMetadata", {})
    return text, usage.get("promptTokenCount", 0), usage.get("candidatesTokenCount", 0)


_DISPATCH = {"anthropic": call_anthropic, "openai": call_openai, "gemini": call_gemini}


async def chat(provider: str, model: str, messages: List[Dict], max_tokens: int = 1024) -> dict:
    """Single entry point regardless of provider. Returns dict with text,
    input_tokens, output_tokens, cost_credits."""
    spec = _spec(provider, model)
    fn = _DISPATCH.get(provider)
    if not fn:
        raise ProviderCallError(f"No adapter for provider: {provider}")
    text, input_tokens, output_tokens = await fn(messages, spec["api_model"], max_tokens)
    cost_credits = estimate_cost(provider, model, input_tokens, output_tokens)
    return {
        "text": text,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost_credits": cost_credits,
    }
