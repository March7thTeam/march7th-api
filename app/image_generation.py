"""Strict text-to-PNG admission and independent official Image API pricing.

No URLs, edits, tools, streaming or arbitrary provider options pass this boundary.
The token ceilings are admission budgets, never substitutes for a usage receipt.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import struct
import zlib
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
from typing import Any

from app.credit_pricing import BillingQuote, CREDITS_PER_USD, PricingError, _count

IMAGE_PRICES = json.loads(Path(__file__).with_name("image_generation_prices.json").read_text(encoding="utf-8"))
IMAGE_ENDPOINT = "/v1/images/generations"
IMAGE_UPSTREAM_URL = "https://api.openai.com/v1/images/generations"
IMAGE_OUTPUT_CEILINGS = {"low": 512, "medium": 2048, "high": 8192}
IMAGE_TEXT_OUTPUT_CEILING = 4096
IMAGE_SIZES = {"1024x1024", "1024x1536", "1536x1024"}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def image_generation_enabled() -> bool:
    return os.getenv("GATEWAY_IMAGE_GENERATION", "false").strip().lower() == "true"


def image_generation_available() -> bool:
    return image_generation_enabled() and bool(os.getenv("GATEWAY_IMAGE_API_KEY", "").strip())


def image_models() -> list[str]:
    return sorted(IMAGE_PRICES["models"])


def prepare_image_request(payload: dict) -> tuple[dict, BillingQuote]:
    allowed = {"model", "prompt", "quality", "size", "n", "stream", "output_format"}
    if set(payload) - allowed:
        raise PricingError("image_parameter_unsupported", "Unsupported image parameter(s): " + ", ".join(sorted(set(payload) - allowed)) + ".")
    model = payload.get("model")
    if not isinstance(model, str) or model not in IMAGE_PRICES["models"]:
        raise PricingError("price_unavailable", "Choose gpt-image-2 or gpt-image-1.5 for image generation.")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 32000:
        raise PricingError("image_prompt_invalid", "prompt must be non-empty text of at most 32000 characters.")
    quality = payload.get("quality", "medium")
    quality = "medium" if quality == "auto" else quality
    size = payload.get("size", "1024x1024")
    size = "1024x1024" if size == "auto" else size
    if not isinstance(quality, str) or quality not in IMAGE_OUTPUT_CEILINGS:
        raise PricingError("image_quality_invalid", "quality must be low, medium, high or auto (medium).")
    if not isinstance(size, str) or size not in IMAGE_SIZES:
        raise PricingError("image_size_invalid", "size must be 1024x1024, 1024x1536, 1536x1024 or auto (1024x1024).")
    if type(payload.get("n", 1)) is not int or payload.get("n", 1) != 1:
        raise PricingError("image_batch_unsupported", "Only n=1 is supported.")
    if type(payload.get("stream", False)) is not bool or payload.get("stream", False):
        raise PricingError("image_stream_unsupported", "Image streaming is not enabled.")
    if payload.get("output_format", "png") != "png":
        raise PricingError("image_format_unsupported", "Only PNG output is supported.")
    outgoing = {"model": model, "prompt": prompt, "quality": quality, "size": size, "n": 1, "output_format": "png"}
    card = dict(IMAGE_PRICES["models"][model])
    input_bound = len(prompt.encode("utf-8")) + 4096
    # The two rectangular canvases contain 1.5 times the square pixel area.
    image_bound = IMAGE_OUTPUT_CEILINGS[quality] * (1 if size == "1024x1024" else 3) // (1 if size == "1024x1024" else 2)
    text_bound = IMAGE_TEXT_OUTPUT_CEILING if card["text_output"] is not None else 0
    weighted = (input_bound * Decimal(card["text_input"]) + image_bound * Decimal(card["image_output"])
                + text_bound * Decimal(card["text_output"] or "0"))
    units = int((weighted * CREDITS_PER_USD).to_integral_value(rounding=ROUND_CEILING))
    snapshot = {"kind": "image_generation", "version": IMAGE_PRICES["version"], "source": IMAGE_PRICES["source"],
                "model": model, "card": card, "credits_per_usd": CREDITS_PER_USD, "images": 1,
                "input_bound": input_bound, "image_input_bound": 0, "image_output_bound": image_bound,
                "text_output_bound": text_bound, "quality": quality, "size": size}
    return outgoing, BillingQuote(units, json.dumps(snapshot, separators=(",", ":")))


def image_billing_details(payload: Any) -> dict | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("usage"), dict):
        return None
    usage = payload["usage"]
    try:
        # Fail closed on future modality/cache fields rather than silently ignoring
        # a new kind of charged usage. GPT Image 2 uses no cache discount here.
        if set(usage) - {"input_tokens", "input_tokens_details", "output_tokens", "output_tokens_details", "total_tokens"}:
            return None
        inputs, outputs, total = (_count(usage[k]) for k in ("input_tokens", "output_tokens", "total_tokens"))
        parts = usage["input_tokens_details"]
        if not isinstance(parts, dict) or set(parts) != {"text_tokens", "image_tokens"}:
            return None
        text_in, image_in = _count(parts["text_tokens"]), _count(parts["image_tokens"])
        if image_in != 0 or text_in != inputs or total != inputs + outputs:
            return None
        out = usage.get("output_tokens_details")
        if out is None and "output_tokens_details" in usage:
            return None
        if out is not None:
            if not isinstance(out, dict) or set(out) != {"text_tokens", "image_tokens"}:
                return None
            text_out, image_out = _count(out["text_tokens"]), _count(out["image_tokens"])
            if text_out + image_out != outputs:
                return None
        else:
            text_out, image_out = 0, outputs
    except (PricingError, KeyError, TypeError):
        return None
    return {"kind": "image_generation", "model": payload.get("model"), "input_tokens": inputs,
            "output_tokens": outputs, "total_tokens": total, "input_text_tokens": text_in,
            "input_image_tokens": image_in, "output_text_tokens": text_out, "output_image_tokens": image_out,
            "output_breakdown_reported": out is not None}


def image_charge(snap: dict, details: dict) -> int:
    if details.get("kind") != "image_generation" or details.get("model") not in {None, snap["model"]}:
        raise PricingError("usage_model_mismatch", "The Image API receipt does not match the image request.")
    card = snap["card"]
    inputs, outputs, total, text_in, image_in, text_out, image_out = (
        _count(details[k]) for k in ("input_tokens", "output_tokens", "total_tokens", "input_text_tokens",
                                    "input_image_tokens", "output_text_tokens", "output_image_tokens"))
    if inputs != text_in + image_in or outputs != text_out + image_out or total != inputs + outputs or image_in != 0:
        raise PricingError("usage_invalid", "The image receipt has contradictory modality totals.")
    if card["text_output"] is not None and details.get("output_breakdown_reported") is not True:
        raise PricingError("usage_breakdown_missing", "This model requires an explicit text/image output breakdown.")
    if text_out and card["text_output"] is None:
        raise PricingError("usage_invalid", "Text output has no applicable official price for this image model.")
    weighted = (text_in * Decimal(card["text_input"]) + image_in * Decimal(card["image_input"])
                + image_out * Decimal(card["image_output"]) + text_out * Decimal(card["text_output"] or "0"))
    return int((weighted * Decimal(snap["credits_per_usd"])).to_integral_value(rounding=ROUND_CEILING))


def image_budget_exceeded(snap: dict, details: dict) -> bool:
    """Admission ceilings require review, never erase an authoritative price."""
    return any(_count(details[field]) > snap[bound] for field, bound in (
        ("input_tokens", "input_bound"), ("input_image_tokens", "image_input_bound"),
        ("output_image_tokens", "image_output_bound"), ("output_text_tokens", "text_output_bound")))


def valid_image_response(payload: Any, *, expected_size: str) -> bool:
    """Decode one bounded native PNG and check its canvas and scanlines.

    Native Image API PNGs use 8-bit RGB/RGBA without interlacing. Reject other
    encodings rather than accepting a file the browser cannot safely decode.
    Inflate at most the selected canvas's exact scanline bytes plus one byte;
    never call an unbounded zlib flush on provider-controlled data.
    """
    if expected_size not in IMAGE_SIZES:
        return False
    expected_width, expected_height = map(int, expected_size.split("x"))
    if not isinstance(payload, dict) or payload.get("error") or not isinstance(payload.get("data"), list) or len(payload["data"]) != 1:
        return False
    item = payload["data"][0]
    if not isinstance(item, dict) or "url" in item or not isinstance(item.get("b64_json"), str):
        return False
    # Match the transport's bound even for direct validation callers.
    if len(item["b64_json"]) > 100 * 1024 * 1024:
        return False
    try:
        raw = base64.b64decode(item["b64_json"], validate=True)
        if not raw.startswith(PNG_SIGNATURE):
            return False
        offset, seen_header, seen_data, data_ended, seen_palette = 8, False, False, False, False
        decoder = zlib.decompressobj()
        scanlines = bytearray()
        expected_bytes = stride = 0
        while offset + 12 <= len(raw):
            length = struct.unpack_from(">I", raw, offset)[0]
            kind = raw[offset + 4:offset + 8]
            end = offset + 12 + length
            if end > len(raw) or any(not (65 <= c <= 90 or 97 <= c <= 122) for c in kind) or not 65 <= kind[2] <= 90:
                return False
            data = raw[offset + 8:offset + 8 + length]
            crc = struct.unpack_from(">I", raw, offset + 8 + length)[0]
            if zlib.crc32(kind + data) & 0xffffffff != crc:
                return False
            if not seen_header:
                if kind != b"IHDR" or length != 13:
                    return False
                width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", data)
                if ((width, height) != (expected_width, expected_height) or width * height > 1024 * 1536
                        or depth != 8 or color not in {2, 6} or (compression, filtering, interlace) != (0, 0, 0)):
                    return False
                stride = width * (3 if color == 2 else 4) + 1
                expected_bytes = stride * height
                seen_header = True
            elif kind == b"IHDR":
                return False
            elif kind == b"PLTE":
                if seen_palette or seen_data or not 3 <= length <= 768 or length % 3:
                    return False
                seen_palette = True
            elif kind == b"IDAT":
                if data_ended:
                    return False
                seen_data = True
                decoded = decoder.decompress(data, expected_bytes - len(scanlines) + 1)
                scanlines.extend(decoded)
                if len(scanlines) > expected_bytes or decoder.unconsumed_tail or decoder.unused_data:
                    return False
            elif kind == b"IEND":
                if not (seen_data and length == 0 and end == len(raw) and decoder.eof
                        and not decoder.unconsumed_tail and not decoder.unused_data and len(scanlines) == expected_bytes):
                    return False
                # All five PNG row filters are lossless for byte-valued RGB/A
                # samples; exact stride and legal selectors guarantee each row
                # can be reconstructed without interpreting arbitrary metadata.
                return all(scanlines[row] <= 4 for row in range(0, expected_bytes, stride))
            elif kind[0] & 0x20 == 0:
                return False  # Unknown critical chunks have undefined decoding.
            if seen_data and kind != b"IDAT":
                data_ended = True
            offset = end
    except (ValueError, TypeError, binascii.Error, struct.error, zlib.error):
        return False
    return False
