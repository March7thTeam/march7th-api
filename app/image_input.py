"""Explicit image admission; the gateway never downloads image URLs itself."""
import base64
import binascii
import copy
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

IMAGE_INPUT_TOKEN_CEILING = 8000
MAX_IMAGES = 16
MAX_IMAGE_BYTES = 8 * 1024 * 1024
VISION_MODELS = frozenset(json.loads(Path(__file__).with_name("model_capabilities.json").read_text())["vision_models"])


def supports_vision(model: str) -> bool:
    return model in VISION_MODELS


def image_input_enabled() -> bool:
    return os.getenv("GATEWAY_IMAGE_INPUT", "").lower() in {"1", "true", "yes", "on"}


def validate_image_inputs(payload: dict, protocol: str) -> list[dict]:
    from app.credit_pricing import PricingError

    def reject(message):
        raise PricingError("billing_shape_unsupported", message)

    parts = []
    messages = payload.get("messages" if protocol == "chat.completions" else "input", [])
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, dict) and isinstance(message.get("content"), list):
                for part in message["content"]:
                    if isinstance(part, dict) and part.get("type") in {"image_url", "input_image"}:
                        parts.append(part)
    if not parts:
        return []
    if not image_input_enabled():
        reject("Image input is disabled.")
    if not supports_vision(payload["model"]):
        reject("This model does not support image input.")
    if len(parts) > MAX_IMAGES:
        reject("A request may contain at most 16 images.")
    for part in parts:
        if protocol == "chat.completions":
            if part.get("type") != "image_url" or set(part) != {"type", "image_url"}:
                reject("Invalid chat image shape.")
            image = part["image_url"]
            if not isinstance(image, dict) or "url" not in image or set(image) - {"url", "detail"}:
                reject("image_url must contain url and optional detail.")
            url, detail = image["url"], image.get("detail", "auto")
        else:
            if part.get("type") != "input_image" or "image_url" not in part or set(part) - {"type", "image_url", "detail"}:
                reject("Invalid responses image shape.")
            url, detail = part["image_url"], part.get("detail", "auto")
        if not isinstance(detail, str) or detail not in {"auto", "low", "high"} or not isinstance(url, str):
            reject("Invalid image URL or detail.")
        if url.startswith("data:"):
            match = re.fullmatch(r"data:image/(png|jpeg|webp|gif);base64,([A-Za-z0-9+/]*={0,2})", url)
            if not match or not match[2] or len(match[2]) > ((MAX_IMAGE_BYTES + 2) // 3) * 4:
                reject("Image data must be valid PNG/JPEG/WebP/GIF base64, at most 8 MB decoded.")
            try:
                data = base64.b64decode(match[2], validate=True)
            except (ValueError, binascii.Error):
                reject("Invalid image base64.")
            if len(data) > MAX_IMAGE_BYTES:
                reject("Image data exceeds 8 MB decoded.")
        else:
            try:
                parsed = urlsplit(url)
                valid = (url.startswith("https://") and parsed.scheme == "https" and parsed.hostname
                         and parsed.username is None and parsed.password is None and parsed.port != 0
                         and not any(c.isspace() or ord(c) < 32 for c in url) and "\\" not in url)
            except ValueError:
                valid = False
            if not valid:
                reject("Image URLs must use https:// or supported base64 data URLs.")
    return parts


def image_safe_bytes(payload: dict) -> bytes:
    """Count all framing and text bytes while replacing data URLs with a small marker."""
    def clean(value: str) -> str:
        return "[image]" if value.startswith("data:image/") else value
    # Only replace admitted image URL fields. Text containing a data URL is text
    # and must still reserve its full byte upper bound.
    safe = copy.deepcopy(payload)
    for message in safe.get("messages", []) if isinstance(safe.get("messages"), list) else []:
        for part in message.get("content", []) if isinstance(message, dict) and isinstance(message.get("content"), list) else []:
            if isinstance(part, dict) and part.get("type") == "image_url":
                part["image_url"]["url"] = clean(part["image_url"]["url"])
    for message in safe.get("input", []) if isinstance(safe.get("input"), list) else []:
        for part in message.get("content", []) if isinstance(message, dict) and isinstance(message.get("content"), list) else []:
            if isinstance(part, dict) and part.get("type") == "input_image":
                part["image_url"] = clean(part["image_url"])
    return json.dumps(safe, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
