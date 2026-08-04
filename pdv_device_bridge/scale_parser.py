from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(slots=True)
class ParsedWeight:
    grams: int
    kilograms: float
    stable: bool
    raw_text: str
    raw_hex: str


_WEIGHT_RE = re.compile(r"([-+]?\d+(?:[\.,]\d+)?)(?:\s*(kg|g))?", re.IGNORECASE)


def parse_weight_payload(payload: bytes) -> ParsedWeight | None:
    if not payload:
        return None

    raw_text = payload.decode("ascii", errors="ignore")
    if not raw_text.strip():
        return None

    normalized = raw_text.replace(",", ".")
    matches = list(_WEIGHT_RE.finditer(normalized))
    if not matches:
        return None

    candidate = _pick_best_match(matches)
    if candidate is None:
        return None

    value_str, unit = candidate

    try:
        value = float(value_str)
    except ValueError:
        return None

    unit = (unit or "").lower()
    if unit == "g":
        grams = int(round(value))
    elif unit == "kg":
        grams = int(round(value * 1000))
    else:
        # Em balancas sem unidade explicita, o mais comum e retorno em kg.
        grams = int(round(value * 1000 if abs(value) < 50 else value))

    stable = _infer_stability(raw_text)

    return ParsedWeight(
        grams=grams,
        kilograms=grams / 1000,
        stable=stable,
        raw_text=raw_text,
        raw_hex=payload.hex(),
    )


def _pick_best_match(matches: list[re.Match[str]]) -> tuple[str, str | None] | None:
    with_unit = [item for item in matches if item.group(2)]
    selected = with_unit[-1] if with_unit else matches[-1]
    return selected.group(1), selected.group(2)


def _infer_stability(text: str) -> bool:
    normalized = text.upper()

    unstable_tokens = ("UNSTABLE", "MOTION", "US,", "US ", "US+")
    if any(token in normalized for token in unstable_tokens):
        return False

    stable_tokens = ("STABLE", "ST,", "ST ", "ST+")
    if any(token in normalized for token in stable_tokens):
        return True

    return True
