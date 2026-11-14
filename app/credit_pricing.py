"""Versioned official Standard token prices and exact decimal credit conversion."""
from __future__ import annotations
from app.price_store import exact_context

import json
import hashlib
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
import posixpath
from app.image_input import IMAGE_INPUT_TOKEN_CEILING, image_safe_bytes, validate_image_inputs
from app.text_capabilities import (MAX_OUTPUT_TOKENS, DEFAULT_OUTPUT_TOKENS, AUTO_OUTPUT_TIERS,
                                   automatic_output_tiers, output_token_ceiling)

PRICE_DATA = json.loads(Path(__file__).with_name("credit_prices.json").read_text(encoding="utf-8"))
MODEL_ALIAS_DATA = json.loads(Path(__file__).with_name("credit_model_aliases.json").read_text(encoding="utf-8"))
UNITS_PER_CREDIT = 1_000_000
CREDITS_PER_USD = 720
MAX_TOKEN_COUNT = 10_000_000


class PricingError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class BillingQuote:
    amount_units: int
    snapshot: str


def priced_models() -> list[str]:
    from app.price_store import current_release
    return sorted(current_release()["text"]["models"])


def model_price(model: str, data=None) -> dict:
    from app.price_store import current_release
    card = (data or current_release()["text"])["models"].get(model)
    if card is None or card.get("output") is None:
        raise PricingError("price_unavailable", "This model has no supported official text-generation price. Choose a priced text model.")
    return dict(card)


def protected_ai_target(base_url: str, path: str) -> bool:
    """Generic/tester routes must not bypass the metered generation executor."""
    from app.config import upstream_base_url
    host = (urlsplit(base_url).hostname or "").lower()
    upstream_host = (urlsplit(upstream_base_url()).hostname or "").lower()
    target = urlsplit(urlsplit(base_url).path.rstrip("/") + "/" + path.lstrip("/")).path
    for _ in range(3):
        target = unquote(target)
    target = posixpath.normpath("/" + target.lstrip("/")).lower()
    free = target in {"/v1/models", "/models", "/health", "/ready"}
    if host in {upstream_host, "codex-proxy", "api.openai.com", "cli.luomo.moe"}:
        return not free
    return any(p in target for p in ("/chat/completions", "/responses", "/images/generations", "/images/edits", "/audio/", "/embeddings", "/completions", "/agents/", "/realtime"))


def _count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TOKEN_COUNT:
        raise PricingError("usage_invalid", "Token counts must be bounded non-negative integers.")
    return value


def _aliased_count(mapping: dict, first: str, second: str) -> int:
    values = [_count(mapping[key]) for key in (first, second) if key in mapping]
    if not values or len(set(values)) != 1:
        raise PricingError("usage_invalid", "Token aliases are missing or contradictory.")
    return values[0]


def _reject_nontext(value: Any, image_parts: set[int] | None = None) -> None:
    if isinstance(value, dict):
        if image_parts and id(value) in image_parts:
            return
        if value.get("type") in {"item_reference", "image_url", "input_image", "input_file", "input_audio", "file", "audio", "image", "computer", "computer_use_preview", "image_generation", "web_search", "web_search_preview", "file_search", "code_interpreter", "mcp", "shell"}:
            raise PricingError("billing_shape_unsupported", "Built-in tools, files, images and audio require separate pricing and are not enabled for credit billing.")
        if "prompt_cache_breakpoint" in value:
            raise PricingError("billing_shape_unsupported", "Explicit cache breakpoints are not currently supported by this gateway.")
        for child in value.values():
            _reject_nontext(child, image_parts)
    elif isinstance(value, list):
        for child in value:
            _reject_nontext(child, image_parts)


def output_limit_requested(payload: Any, *, protocol: str) -> bool:
    """True when the caller chose an output limit itself (the gateway must honour it)."""
    if not isinstance(payload, dict):
        return False
    keys = ("max_tokens", "max_completion_tokens") if protocol == "chat.completions" else ("max_output_tokens",)
    return any(key in payload for key in keys)


def checked_output_limit(payload: dict, *, protocol: str, default_output_tokens: int = DEFAULT_OUTPUT_TOKENS) -> int:
    """Validate explicit limits unchanged; constrain only an absent limit."""
    keys = ("max_tokens", "max_completion_tokens") if protocol == "chat.completions" else ("max_output_tokens",)
    limits = [payload[key] for key in keys if key in payload]
    if len(limits) == 2 and (type(limits[0]) is not type(limits[1]) or limits[0] != limits[1]):
        raise PricingError("billing_limit_invalid", "Use only one output token limit.")
    limit = limits[0] if limits else min(default_output_tokens, automatic_output_tiers(payload["model"])[0])
    ceiling = output_token_ceiling(payload["model"])
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= ceiling:
        raise PricingError("billing_limit_invalid", f"The output token limit for {payload['model']} must be between 1 and {ceiling}.")
    return limit


@exact_context
def prepare_billable_request(payload: dict, *, protocol: str, default_output_tokens: int = DEFAULT_OUTPUT_TOKENS) -> tuple[dict, BillingQuote]:
    from app.price_store import current_release, conversion
    release = current_release()
    data = release["text"]
    factors = conversion(release)
    card = model_price(payload["model"], data)
    # Copy through JSON: neither the caller nor the quote may mutate after admission.
    outgoing = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    images = validate_image_inputs(outgoing, protocol)
    _reject_nontext(outgoing, {id(part) for part in images})
    if outgoing.get("prompt") or outgoing.get("background") or outgoing.get("previous_response_id") or outgoing.get("conversation") or outgoing.get("context_management") or outgoing.get("multi_agent") or outgoing.get("delegation"):
        raise PricingError("billing_shape_unsupported", "Background generation and server-side conversation references are not enabled for credit billing. Send the text context explicitly.")
    if outgoing.get("modalities", ["text"]) != ["text"] or outgoing.get("audio"):
        raise PricingError("billing_shape_unsupported", "Only text output is supported for credit billing.")
    if not isinstance(outgoing.get("service_tier", "default"), str) or outgoing.get("service_tier", "default") not in {"default", "auto"}:
        raise PricingError("billing_shape_unsupported", "Only OpenAI Standard processing is supported.")
    outgoing["service_tier"] = "default"
    if "stream" in outgoing and not isinstance(outgoing["stream"], bool):
        raise PricingError("billing_shape_unsupported", "stream must be true or false.")
    if isinstance(outgoing.get("n", 1), bool) or outgoing.get("n", 1) != 1:
        raise PricingError("billing_shape_unsupported", "Credit billing supports one completion per request (n=1).")
    tools = outgoing.get("tools", [])
    if not isinstance(tools, list) or any(not isinstance(t, dict) or t.get("type") not in {"function", "custom"} for t in tools):
        raise PricingError("billing_shape_unsupported", "Only caller-run function tools are supported.")
    limit = checked_output_limit(outgoing, protocol=protocol, default_output_tokens=default_output_tokens)
    if protocol == "chat.completions":
        outgoing.pop("max_tokens", None)
        outgoing["max_completion_tokens"] = limit
        if outgoing.get("stream") is True:
            options = outgoing.get("stream_options") or {}
            if not isinstance(options, dict):
                raise PricingError("billing_shape_unsupported", "stream_options must be an object.")
            outgoing["stream_options"] = {**options, "include_usage": True}
    else:
        outgoing["max_output_tokens"] = limit
        outgoing["store"] = False
    encoded = image_safe_bytes(outgoing) if images else json.dumps(outgoing, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    # Every text token contains at least one UTF-8 byte. Reserve additional framing
    # headroom. An unexpected upstream expansion is held for review, never capped
    # into a silently understated charge.
    input_bound = len(encoded) + 4096 + len(images) * IMAGE_INPUT_TOKEN_CEILING
    if input_bound > 1_000_000:
        raise PricingError("billing_shape_unsupported", "The request exceeds the supported text context budget.")
    long = bool(card["long_context_threshold"] and input_bound > card["long_context_threshold"])
    prefix = "long_" if long else ""
    input_rate = max(Decimal(card[prefix+"input"]), Decimal(card[prefix+"cache_write"] or card[prefix+"input"]))
    output_rate = Decimal(card[prefix+"output"])
    units = int(((input_bound * input_rate + limit * output_rate) * Decimal(factors["credits_per_usd"])).to_integral_value(rounding=ROUND_CEILING))
    snapshot = {"version": data["version"], "source": data["source"], "model": payload["model"], "card": card,
                **factors, "input_bound": input_bound, "output_bound": limit}
    if images:
        snapshot.update(images=len(images), image_token_ceiling=IMAGE_INPUT_TOKEN_CEILING)
    return outgoing, BillingQuote(units, json.dumps(snapshot, separators=(",", ":")))


def billing_details(payload: Any, *, protocol: str) -> dict | None:
    if protocol == "images.generations":
        from app.image_generation import image_billing_details
        return image_billing_details(payload)
    if not isinstance(payload, dict):
        return None
    response = payload.get("response") if protocol.startswith("response") and isinstance(payload.get("response"), dict) else payload
    usage = response.get("usage")
    if not isinstance(usage, dict):
        return None
    try:
        inputs = _aliased_count(usage, "input_tokens", "prompt_tokens")
        outputs = _aliased_count(usage, "output_tokens", "completion_tokens")
        if "total_tokens" in usage and _count(usage["total_tokens"]) != inputs + outputs:
            return None
        detail_aliases = [usage[key] for key in ("input_tokens_details", "prompt_tokens_details") if key in usage]
        if any(not isinstance(value, dict) for value in detail_aliases):
            return None
        if len(detail_aliases) == 2 and json.dumps(detail_aliases[0], sort_keys=True) != json.dumps(detail_aliases[1], sort_keys=True):
            return None
        parts = detail_aliases[0] if detail_aliases else {}
        if not isinstance(parts, dict):
            return None
        cached = _count(parts.get("cached_tokens", 0))
        write_fields = [key for key in ("cache_write_tokens", "cached_creation_tokens") if key in parts]
        write_values = [_count(parts[key]) for key in write_fields]
        if len(set(write_values)) > 1:
            return None
        # CLIProxyAPI exposes cached_creation_tokens as an explicit alias for
        # cache_write_tokens. Both are already included in the input total;
        # identical aliases must not be counted twice.
        writes = write_values[0] if write_values else 0
        if cached + writes > inputs:
            return None
    except PricingError:
        return None
    return {"input_tokens": inputs, "output_tokens": outputs, "cached_tokens": cached, "cache_write_tokens": writes,
            "cached_tokens_reported": "cached_tokens" in parts,
            "cache_write_reported": bool(write_fields), "cache_write_source": write_fields, "model": response.get("model"),
            "service_tier": response.get("service_tier"), "total_tokens": inputs + outputs}


def billing_receipt_present(payload: Any, *, protocol: str) -> bool:
    if not isinstance(payload, dict):
        return False
    if "usage" in payload and payload["usage"] is not None:
        return True
    response = payload.get("response") if protocol.startswith("response") else None
    return isinstance(response, dict) and "usage" in response and response["usage"] is not None


def _price_equivalent_snapshot(snap: dict, actual_model: Any) -> bool:
    """Accept only a reviewed current snapshot under its exact pinned price card.

    This affects receipt verification only: request grants, response model names,
    price SKUs and historical holds remain unchanged. No prefix/date inference.
    """
    if not isinstance(actual_model, str) or snap.get("version") != MODEL_ALIAS_DATA["price_version"]:
        return False
    entry = MODEL_ALIAS_DATA["models"].get(snap["model"])
    if not entry or actual_model != entry["snapshot"]:
        return False
    try:
        canonical_card = json.dumps(snap["card"], sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        return False
    return hashlib.sha256(canonical_card).hexdigest() == entry["price_card_sha256"]


@exact_context
def calculate_charge(snapshot: str, details: dict) -> int:
    snap = json.loads(snapshot)
    if snap.get("kind") == "image_generation":
        from app.image_generation import image_charge
        return image_charge(snap, details)
    card = snap["card"]
    inputs, outputs = _count(details["input_tokens"]), _count(details["output_tokens"])
    cached, writes = _count(details["cached_tokens"]), _count(details["cache_write_tokens"])
    if cached + writes > inputs:
        raise PricingError("usage_invalid", "Cache counts exceed input tokens.")
    actual_model = details.get("model")
    if actual_model is not None and actual_model != snap["model"] and not _price_equivalent_snapshot(snap, actual_model):
        raise PricingError("usage_model_mismatch", "The upstream model differs from the priced request model.")
    if details.get("service_tier") not in {None, "default", "standard", "auto"}:
        raise PricingError("usage_tier_mismatch", "The upstream did not report Standard processing.")
    if card["cache_write"] is not None and not details.get("cache_write_reported"):
        raise PricingError("usage_breakdown_missing", "Upstream cache-write usage was not reported; the hold requires reconciliation.")
    if card["cached_input"] is not None and not details.get("cached_tokens_reported"):
        raise PricingError("usage_breakdown_missing", "Upstream cached-input usage was not reported; the hold requires reconciliation.")
    prefix = "long_" if card["long_context_threshold"] and inputs > card["long_context_threshold"] else ""
    ordinary = inputs - cached - writes
    if (cached and card[prefix+"cached_input"] is None) or (writes and card[prefix+"cache_write"] is None):
        raise PricingError("usage_invalid", "The reported cache usage has no applicable official price.")
    usd_per_million_sum = (ordinary * Decimal(card[prefix+"input"]) + outputs * Decimal(card[prefix+"output"])
                          + cached * Decimal(card[prefix+"cached_input"] or "0") + writes * Decimal(card[prefix+"cache_write"] or "0"))
    # USD/million tokens * credits/USD * microcredits/credit / million cancels.
    return int((usd_per_million_sum * Decimal(snap["credits_per_usd"])).to_integral_value(rounding=ROUND_CEILING))
