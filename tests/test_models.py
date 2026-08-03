"""Tests for APstorage data models and calculations."""

import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "apstorage_ble" / "models.py"
SPEC = importlib.util.spec_from_file_location("apstorage_models", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODELS = importlib.util.module_from_spec(SPEC)
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
