import importlib.util
from pathlib import Path
import sys
import types
import unittest

bleak_module = types.ModuleType("bleak")
bleak_module.BleakClient = object
sys.modules.setdefault("bleak", bleak_module)

bleak_backends_module = types.ModuleType("bleak.backends")
sys.modules.setdefault("bleak.backends", bleak_backends_module)

bleak_device_module = types.ModuleType("bleak.backends.device")
bleak_device_module.BLEDevice = object
sys.modules.setdefault("bleak.backends.device", bleak_device_module)

bleak_exc_module = types.ModuleType("bleak.exc")
bleak_exc_module.BleakError = Exception
sys.modules.setdefault("bleak.exc", bleak_exc_module)

retry_module = types.ModuleType("bleak_retry_connector")
retry_module.BleakClientWithServiceCache = object
retry_module.establish_connection = lambda *args, **kwargs: None
sys.modules.setdefault("bleak_retry_connector", retry_module)

MODULE_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "apstorage_ble" / "soc_client.py"
SPEC = importlib.util.spec_from_file_location("apstorage_soc_client", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
SOC_CLIENT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SOC_CLIENT
SPEC.loader.exec_module(SOC_CLIENT)
_extract_metrics = SOC_CLIENT._extract_metrics


class AlarmParsingTests(unittest.TestCase):
    def test_extracts_active_battery_and_pcs_alarm_summaries(self) -> None:
        parsed = {
            "data": {
                "storageAlarm": "BAT_UNDERVOLTAGE",
                "inverterAlarm": '{"OVP":"1","temp":"0"}',
                "essAlarm": '{"contactor":"open"}',
            }
        }

        metrics = _extract_metrics(parsed)

        self.assertEqual(metrics.battery_alarm, "BAT_UNDERVOLTAGE")
        self.assertIn("OVP", metrics.pcs_alarm)
        self.assertIn("Battery:", metrics.alarm_summary)
        self.assertIn("PCS:", metrics.alarm_summary)

    def test_reports_clear_when_alarm_payload_is_empty(self) -> None:
        parsed = {
            "data": {
                "storageAlarm": "0",
                "inverterAlarm": '{}',
                "essAlarm": '{"status":"0"}',
            }
        }

        metrics = _extract_metrics(parsed)

        self.assertEqual(metrics.battery_alarm, "Clear")
        self.assertEqual(metrics.pcs_alarm, "Clear")
        self.assertEqual(metrics.alarm_summary, "Clear")


if __name__ == "__main__":
    unittest.main()
