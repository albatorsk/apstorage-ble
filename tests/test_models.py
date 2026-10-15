"""Tests for APstorage data models and calculations."""

import importlib.util
from pathlib import Path
import sys


MODULE_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "apstorage_ble" / "models.py"
SPEC = importlib.util.spec_from_file_location("apstorage_models", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODELS = importlib.util.module_from_spec(SPEC)
# dataclasses look the module up in sys.modules while the class is created.
sys.modules[SPEC.name] = MODELS
SPEC.loader.exec_module(MODELS)

PCSData = MODELS.PCSData


class TestSignedBatteryPower:
    """Tests for the signed_battery_power property."""

    def test_both_none_returns_none(self) -> None:
        """When both battery_power and battery_charging_power are None, return None."""
        data = PCSData()
        assert data.signed_battery_power is None

    def test_discharging_only(self) -> None:
        """When only discharging (positive power), return the discharging value."""
        data = PCSData(battery_power=1500.0, battery_charging_power=None)
        assert data.signed_battery_power == 1500.0

    def test_charging_only(self) -> None:
        """When only charging power is available, return negative of charging value."""
        data = PCSData(battery_power=None, battery_charging_power=1000.0)
        assert data.signed_battery_power == -1000.0

    def test_discharging_negative_returns_as_is(self) -> None:
        """When battery_power is negative, return it as-is (no subtraction of charging)."""
        data = PCSData(battery_power=-500.0, battery_charging_power=200.0)
        assert data.signed_battery_power == -500.0

    def test_both_positive_discharging_minus_charging(self) -> None:
        """When both are positive, return discharging - charging."""
        data = PCSData(battery_power=2000.0, battery_charging_power=500.0)
        assert data.signed_battery_power == 1500.0

    def test_both_positive_with_charging_greater(self) -> None:
        """When charging > discharging, result is negative."""
        data = PCSData(battery_power=500.0, battery_charging_power=1000.0)
        assert data.signed_battery_power == -500.0

    def test_both_zero(self) -> None:
        """When both are zero, return 0."""
        data = PCSData(battery_power=0.0, battery_charging_power=0.0)
        assert data.signed_battery_power == 0.0

    def test_discharging_zero_charging_positive(self) -> None:
        """When discharging is 0 and charging is positive, return negative charging."""
        data = PCSData(battery_power=0.0, battery_charging_power=300.0)
        assert data.signed_battery_power == -300.0

    def test_decimal_values(self) -> None:
        """Test with decimal values."""
        data = PCSData(battery_power=1234.5, battery_charging_power=678.9)
        assert data.signed_battery_power == 1234.5 - 678.9


class TestFirmwareVersionParts:
    """Tests for firmware version parsing."""

    def test_firmware_version_1(self) -> None:
        """Test extraction of first firmware version part."""
        data = PCSData(pcs_firmware_version="v1_2_3")
        assert data.pcs_firmware_version_1 == "v1"
        assert data.pcs_firmware_version_2 == "2"
        assert data.pcs_firmware_version_3 == "3"

    def test_firmware_version_with_underscores(self) -> None:
        """Test version with multiple underscores."""
        data = PCSData(pcs_firmware_version="1_2_3")
        assert data.pcs_firmware_version_1 == "1"
        assert data.pcs_firmware_version_2 == "2"
        assert data.pcs_firmware_version_3 == "3"

    def test_firmware_version_none(self) -> None:
        """Test with None firmware version."""
        data = PCSData(pcs_firmware_version=None)
        assert data.pcs_firmware_version_1 is None
        assert data.pcs_firmware_version_2 is None
        assert data.pcs_firmware_version_3 is None

    def test_firmware_version_single_part(self) -> None:
        """Test version with only one part."""
        data = PCSData(pcs_firmware_version="v1")
        assert data.pcs_firmware_version_1 == "v1"
        assert data.pcs_firmware_version_2 is None
        assert data.pcs_firmware_version_3 is None

    def test_firmware_version_two_parts(self) -> None:
        """Test version with two parts."""
        data = PCSData(pcs_firmware_version="1_2")
        assert data.pcs_firmware_version_1 == "1"
        assert data.pcs_firmware_version_2 == "2"
        assert data.pcs_firmware_version_3 is None


class TestGridFrequency:
    """Grid frequency comes from ABD[].GF (per PCS) in getDeviceLastDataLocal."""

    def _parse(self, data: dict) -> "PCSData":
        parsed = PCSData()
        parsed.apply_last_data(data)
        return parsed

    def test_from_abd(self) -> None:
        assert self._parse({"ABD": [{"ABID": "B05000000000", "GF": "49.970"}]}).grid_frequency == 49.97

    def test_top_level_key(self) -> None:
        assert self._parse({"GF": "50.02"}).grid_frequency == 50.02

    def test_scaled_values(self) -> None:
        assert self._parse({"ABD": [{"GF": "4998"}]}).grid_frequency == 49.98
        assert self._parse({"ABD": [{"GF": "501"}]}).grid_frequency == 50.1

    def test_missing_or_invalid_is_none(self) -> None:
        # No made-up 50 Hz when the PCS does not report it.
        assert self._parse({"ABD": [{"ABID": "B05000000000", "GS": "0"}]}).grid_frequency is None
        assert self._parse({"ABD": [{"GF": "0"}]}).grid_frequency is None
        assert self._parse({}).grid_frequency is None

    def test_cleared_when_no_longer_reported(self) -> None:
        parsed = PCSData()
        parsed.apply_last_data({"ABD": [{"GF": "50.01"}]})
        parsed.apply_last_data({"ABD": [{"GS": "0"}]})
        assert parsed.grid_frequency is None


class TestBatteryCapacity:
    """Capacity: storage config info first; 0 means not configured."""

    def test_zero_is_unknown(self) -> None:
        data = PCSData()
        data.apply_device_info({"capacity": "0"})
        data.apply_last_data({"Capacity": "0"})
        assert data.battery_capacity is None

    def test_config_info_wins(self) -> None:
        data = PCSData()
        data.apply_last_data({"Capacity": "5.8"})
        data.apply_device_info({"capacity": "0"})
        assert data.battery_capacity == 5.8
        data.apply_config_info({"capacity": "20"})
        assert data.battery_capacity == 20

    def test_short_key_and_pcs_list(self) -> None:
        data = PCSData()
        data.apply_config_info({"C": "10.24"})
        assert data.battery_capacity == 10.24
        data.apply_config_info({"capacity": "0", "pcs_list": {"battery_capacity": "15.36"}})
        assert data.battery_capacity == 15.36
        data.apply_config_info({"pcs_list": [{"battery_capacity": "10"}, {"battery_capacity": "10"}]})
        assert data.battery_capacity == 20

    def test_falls_back_when_config_reports_zero(self) -> None:
        data = PCSData()
        data.apply_device_info({"capacity": "12"})
        data.apply_config_info({"capacity": "0"})
        assert data.battery_capacity == 12
