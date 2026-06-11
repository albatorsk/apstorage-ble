"""Shared protocol helpers for APstorage BLE entities.

This module centralizes mode/state normalization and capability checks so
sensor, select, switch, and number platforms can share a single source of
truth.
"""

from __future__ import annotations

from typing import Any


MODE_CODE_TO_OPTION: dict[str, str] = {
    "0": "Peak Valley",
    "1": "Self-Consumption",
    "2": "Manual Control",
    "3": "Advanced",
    "4": "Backup power supply",
    "5": "Peak-Shaving",
    "6": "Intelligent",
}

OPTION_TO_MODE_CODE: dict[str, str] = {label: code for code, label in MODE_CODE_TO_OPTION.items()}

# Normalize known legacy spellings/casing to canonical mode codes.
LEGACY_STATE_TO_CODE: dict[str, str] = {
    "peak valley": "0",
    "self-consumption": "1",
    "self consumption": "1",
    "manual control": "2",
    "advanced": "3",
    "backup power supply": "4",
    "peak-shaving": "5",
    "peak shaving": "5",
    "intelligent": "6",
}


def _normalize_label(value: Any) -> str:
    """Normalize a human-readable label for tolerant matching."""
    return "".join(ch for ch in str(value).strip().lower() if ch.isalnum())


LEGACY_STATE_TO_CODE_NORMALIZED: dict[str, str] = {
    _normalize_label(key): code for key, code in LEGACY_STATE_TO_CODE.items()
}

# Accept canonical option labels as additional legacy aliases.
for _code, _label in MODE_CODE_TO_OPTION.items():
    LEGACY_STATE_TO_CODE_NORMALIZED[_normalize_label(_label)] = _code


def normalize_mode_code(value: Any) -> str | None:
    """Return canonical mode code (as string) for raw protocol value."""
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    if text.isdigit():
        code = str(int(text))
        return code if code in MODE_CODE_TO_OPTION else None

    try:
        number = float(text)
    except (TypeError, ValueError):
        number = None

    if number is not None and number.is_integer():
        code = str(int(number))
        return code if code in MODE_CODE_TO_OPTION else None

    if text in MODE_CODE_TO_OPTION:
        return text

    lowered = text.lower()
    if lowered in LEGACY_STATE_TO_CODE:
        return LEGACY_STATE_TO_CODE[lowered]

    normalized = _normalize_label(text)
    return LEGACY_STATE_TO_CODE_NORMALIZED.get(normalized)


def resolve_mode_code(system_mode: Any, system_state: Any) -> str | None:
    """Resolve current mode from system_mode first, then system_state."""
    mode_code = normalize_mode_code(system_mode)
    if mode_code is not None:
        return mode_code
    return normalize_mode_code(system_state)

    return None


def mode_name(value: Any) -> str | None:
    """Return canonical display name for raw protocol value."""
    code = normalize_mode_code(value)
    if code is None:
        return None
    return MODE_CODE_TO_OPTION.get(code)


def supports_peak_power(system_mode: Any) -> bool:
    """Whether peak-shaving specific controls should be available."""
    return normalize_mode_code(system_mode) == "5"


def supports_backup_soc(system_mode: Any) -> bool:
    """Whether backup SoC controls should be available."""
    return normalize_mode_code(system_mode) in {"1", "3"}


def supports_peak_valley_switches(system_mode: Any) -> bool:
    """Whether peak-valley switch controls should be available."""
    return normalize_mode_code(system_mode) == "0"
