"""Model "lines": each family gets one signage colour key, letter and number.

Templates, the public catalog JSON and the live departure board all read this one mapping.
"""
from __future__ import annotations

import re

_FAMILIES = (
    ("luna", "luna", "L"),
    ("terra", "terra", "T"),
    ("astra", "astra", "A"),
    ("sol", "sol", "S"),
    ("codex", "codex", "C"),
    ("image", "image", "I"),
)


def line_for(model_id: str | None) -> dict:
    model = (model_id or "").lower()
    family, letter = "other", "G" if model.startswith("gpt") else (model[:1] or "?").upper()
    for keyword, name, code in _FAMILIES:
        if keyword in model:
            family, letter = name, code
            break
    version = re.search(r"(\d+)(?:\.(\d+))?", model)
    number = (version.group(1) + (version.group(2) or "0"))[:2] if version else "00"
    return {"family": family, "letter": letter, "number": number}


def board_state(model_id: str, text_candidate: bool) -> dict:
    """The one status vocabulary every page uses: callable, interface not open, or not priced."""
    if text_candidate:
        return {"tone": "ok", "label": "可调用"}
    if model_id.startswith("gpt-image-"):
        return {"tone": "off", "label": "接口未开放"}
    return {"tone": "warn", "label": "暂未计价"}


def verification_note(verification: dict | None, text_candidate: bool) -> str:
    """Short test-coverage note shared by the departure board and the model cards."""
    protocols = (verification or {}).get("protocols") or []
    if protocols:
        return f"已实测 {len(protocols)} 种协议"
    return "待本站实测" if text_candidate else "—"
