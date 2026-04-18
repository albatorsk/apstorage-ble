import importlib.util
import json
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
_extract_version_info = SOC_CLIENT._extract_version_info


class _FakeNotifyClient:
    def __init__(self, codec, session_key: bytes, response_payload: dict) -> None:
        self._codec = codec
        self._session_key = session_key
        self._response_payload = response_payload
        self._callback = None
        self.start_notify_calls = 0

    async def start_notify(self, _uuid, callback) -> None:
        self.start_notify_calls += 1
        if self.start_notify_calls > 1:
            raise AssertionError("notify should stay active for the whole secure session")
        self._callback = callback

    async def stop_notify(self, _uuid) -> None:
        self._callback = None

    async def write_gatt_char(self, _uuid, data, response=True) -> None:
        if self._callback is None:
            raise AssertionError("request sent without an active notify callback")

        cmd_custom = SOC_CLIENT._make_cmd(1, 19)
        if data[0] != cmd_custom or (data[1] & 0x10):
            return

        response_json = json.dumps(self._response_payload, separators=(",", ":"))
        encrypted_payload = SOC_CLIENT._ema_encrypt_json(response_json)
        packets = self._codec.build_packets(
            cmd_custom,
            encrypted_payload,
            encrypt=True,
            checksum=True,
            aes_key=self._session_key,
        )
        for packet in packets:
            self._callback(None, bytearray(packet))


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


class VersionParsingTests(unittest.TestCase):
    def test_extracts_versions_from_stringified_payload_and_camelcase_keys(self) -> None:
        parsed = {
            "data": json.dumps(
                {
                    "currentVersion": "EZ1_1.2.3_20260418",
                    "latestVersion": "EZ1_1.2.4_20260420",
                    "softwareVersion": "EZ1_1.2.3_20260418",
                    "hardwareVersion": "HW-ELT12",
                }
            )
        }

        info = _extract_version_info(parsed)

        self.assertEqual(info.get("pcs_firmware_version"), "EZ1_1.2.3_20260418")
        self.assertEqual(info.get("pcs_latest_firmware_version"), "EZ1_1.2.4_20260420")
        self.assertEqual(info.get("pcs_hardware_version"), "HW-ELT12")


class NotifySessionRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_soc_request_reuses_existing_notify_session(self) -> None:
        client = SOC_CLIENT.APstorageSocClient()
        client.session_key = b"0123456789abcdef"

        fake_ble = _FakeNotifyClient(
            client._codec,
            client.session_key,
            {"code": 200, "data": {"ssoc": "77"}},
        )

        original_delay = SOC_CLIENT.PACKET_WRITE_DELAY_SECONDS
        original_encrypt = SOC_CLIENT._ema_encrypt_json
        original_encrypt_hex = SOC_CLIENT._ema_encrypt_json_hexascii
        original_decrypt = SOC_CLIENT._ema_decrypt_payload
        original_cfb_encrypt = SOC_CLIENT._aes_cfb_encrypt
        original_cfb_decrypt = SOC_CLIENT._aes_cfb_decrypt
        try:
            SOC_CLIENT.PACKET_WRITE_DELAY_SECONDS = 0
            SOC_CLIENT._ema_encrypt_json = lambda text: text.encode("utf-8")
            SOC_CLIENT._ema_encrypt_json_hexascii = lambda text: text.encode("ascii")
            SOC_CLIENT._ema_decrypt_payload = lambda payload: payload.decode("utf-8")
            SOC_CLIENT._aes_cfb_encrypt = lambda key, seq, payload: payload
            SOC_CLIENT._aes_cfb_decrypt = lambda key, seq, payload: payload
            await fake_ble.start_notify(SOC_CLIENT.NOTIFY_CHAR, client._on_notify_impl)
            parsed = await client._send_soc_request(fake_ble, "B05000001878", system_id="")
        finally:
            SOC_CLIENT.PACKET_WRITE_DELAY_SECONDS = original_delay
            SOC_CLIENT._ema_encrypt_json = original_encrypt
            SOC_CLIENT._ema_encrypt_json_hexascii = original_encrypt_hex
            SOC_CLIENT._ema_decrypt_payload = original_decrypt
            SOC_CLIENT._aes_cfb_encrypt = original_cfb_encrypt
            SOC_CLIENT._aes_cfb_decrypt = original_cfb_decrypt

        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.get("code"), 200)
        self.assertEqual(parsed.get("data", {}).get("ssoc"), "77")
        self.assertEqual(fake_ble.start_notify_calls, 1)


if __name__ == "__main__":
    unittest.main()
