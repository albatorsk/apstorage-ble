"""Tests for shared APstorage protocol mode helpers."""

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "apstorage_ble" / "protocol.py"
SPEC = importlib.util.spec_from_file_location("apstorage_protocol", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
PROTOCOL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROTOCOL)

MODE_CODE_TO_OPTION = PROTOCOL.MODE_CODE_TO_OPTION
mode_name = PROTOCOL.mode_name
normalize_mode_code = PROTOCOL.normalize_mode_code
resolve_mode_code = PROTOCOL.resolve_mode_code
supports_backup_soc = PROTOCOL.supports_backup_soc
supports_peak_power = PROTOCOL.supports_peak_power
supports_peak_valley_switches = PROTOCOL.supports_peak_valley_switches


def test_normalize_mode_code_accepts_numeric_forms() -> None:
    assert normalize_mode_code("1") == "1"
    assert normalize_mode_code("01") == "1"
    assert normalize_mode_code("1.0") == "1"
    assert normalize_mode_code(5) == "5"
    assert normalize_mode_code("7") is None


def test_normalize_mode_code_accepts_legacy_labels() -> None:
    assert normalize_mode_code("Self-Consumption") == "1"
    assert normalize_mode_code("Self-consumption") == "1"
    assert normalize_mode_code("self consumption") == "1"
    assert normalize_mode_code("self-consumption") == "1"
    assert normalize_mode_code("Advanced") == "3"
    assert normalize_mode_code("Intelligent") == "6"
    assert normalize_mode_code("peak-shaving") == "5"


def test_mode_name_maps_known_values() -> None:
    assert mode_name("5") == MODE_CODE_TO_OPTION["5"]
    assert mode_name("peak shaving") == MODE_CODE_TO_OPTION["5"]
    assert mode_name("unknown") is None


def test_resolve_mode_prefers_system_mode_then_system_state() -> None:
    assert resolve_mode_code("3", "5") == "3"
    assert resolve_mode_code(None, "Intelligent") == "6"
    assert resolve_mode_code(None, None) is None


def test_support_predicates_use_shared_mode_resolution() -> None:
    assert supports_peak_power("5")
    assert not supports_peak_power("Advanced")

    assert supports_backup_soc("Self-Consumption")
    assert supports_backup_soc("advanced")
    assert not supports_backup_soc("5")

    assert supports_peak_valley_switches("Peak Valley")
    assert not supports_peak_valley_switches("1")
