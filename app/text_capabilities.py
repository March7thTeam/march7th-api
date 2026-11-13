"""Reviewed text model routing and output limits, independent of token prices."""
from __future__ import annotations

import json
import os
from pathlib import Path

TEXT_CAPABILITY_DATA = json.loads(Path(__file__).with_name("text_model_capabilities.json").read_text(encoding="utf-8"))
MAX_OUTPUT_TOKENS = 128_000
DEFAULT_OUTPUT_TOKENS = 2048
AUTO_OUTPUT_TIERS = (32768, 16384, 8192, 4096, DEFAULT_OUTPUT_TOKENS)
NATIVE_RESPONSES_MODELS = frozenset(model for model, card in TEXT_CAPABILITY_DATA["models"].items()
                                  if "responses" in card["supported_endpoints"])


def text_capability(model: str) -> dict | None:
    return TEXT_CAPABILITY_DATA["models"].get(model)


def output_token_ceiling(model: str) -> int:
    card = text_capability(model)
    return card["max_output_tokens"] if card is not None else MAX_OUTPUT_TOKENS


def automatic_output_tiers(model: str) -> tuple[int, ...]:
    card = text_capability(model)
    if card is None:
        return (DEFAULT_OUTPUT_TOKENS,)
    cap = card["max_output_tokens"]
    return tuple(sorted({min(tier, cap) for tier in AUTO_OUTPUT_TIERS}, reverse=True))


def native_responses_enabled() -> bool:
    return os.getenv("GATEWAY_NATIVE_RESPONSES", "false").strip().lower() == "true"


def native_responses_available() -> bool:
    return native_responses_enabled() and bool(os.getenv("GATEWAY_NATIVE_RESPONSES_API_KEY", "").strip())
