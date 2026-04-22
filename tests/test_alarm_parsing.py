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

ha_module = types.ModuleType("homeassistant")
sys.modules.setdefault("homeassistant", ha_module)

ha_components_module = types.ModuleType("homeassistant.components")
sys.modules.setdefault("homeassistant.components", ha_components_module)

ha_bluetooth_module = types.ModuleType("homeassistant.components.bluetooth")

class _DummyBluetoothScanningMode:
    ACTIVE = "active"


class _DummyBluetoothChange:
    pass


class _DummyBluetoothServiceInfoBleak:
    def __init__(self, address: str = "AA:BB:CC:DD:EE:FF", connectable: bool = True) -> None:
        self.connectable = connectable
        self.device = types.SimpleNamespace(address=address)
        self.rssi = -60


ha_bluetooth_module.BluetoothScanningMode = _DummyBluetoothScanningMode
ha_bluetooth_module.BluetoothChange = _DummyBluetoothChange
ha_bluetooth_module.BluetoothServiceInfoBleak = _DummyBluetoothServiceInfoBleak
ha_bluetooth_module.async_ble_device_from_address = lambda hass, address, connectable=True: object()
sys.modules.setdefault("homeassistant.components.bluetooth", ha_bluetooth_module)

ha_bluetooth_active_module = types.ModuleType("homeassistant.components.bluetooth.active_update_coordinator")

class _DummyActiveBluetoothDataUpdateCoordinator:
    def __init__(self, *args, **kwargs) -> None:
        self.hass = kwargs.get("hass")
        self.available = True
        self._last_service_info = None

    @classmethod
    def __class_getitem__(cls, _item):
        return cls

    async def async_start(self) -> None:
        return None

    def async_update_listeners(self) -> None:
        return None

    def _async_handle_bluetooth_event(self, service_info, change) -> None:
        return None

    def _async_handle_unavailable(self, service_info) -> None:
        return None


ha_bluetooth_active_module.ActiveBluetoothDataUpdateCoordinator = _DummyActiveBluetoothDataUpdateCoordinator
sys.modules.setdefault(
    "homeassistant.components.bluetooth.active_update_coordinator",
    ha_bluetooth_active_module,
)

ha_core_module = types.ModuleType("homeassistant.core")

class _DummyCoreState:
    running = "running"


ha_core_module.CoreState = _DummyCoreState
ha_core_module.HomeAssistant = object
ha_core_module.callback = lambda func: func
sys.modules.setdefault("homeassistant.core", ha_core_module)

custom_components_module = types.ModuleType("custom_components")
custom_components_module.__path__ = []
sys.modules.setdefault("custom_components", custom_components_module)

apstorage_pkg = types.ModuleType("custom_components.apstorage_ble")
apstorage_pkg.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components" / "apstorage_ble")]
sys.modules.setdefault("custom_components.apstorage_ble", apstorage_pkg)

const_module = types.ModuleType("custom_components.apstorage_ble.const")
const_module.DOMAIN = "apstorage_ble"
sys.modules.setdefault("custom_components.apstorage_ble.const", const_module)

models_module = types.ModuleType("custom_components.apstorage_ble.models")

class _DummyPCSData:
    pass


models_module.PCSData = _DummyPCSData
sys.modules.setdefault("custom_components.apstorage_ble.models", models_module)

MODULE_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "apstorage_ble" / "soc_client.py"
SPEC = importlib.util.spec_from_file_location("apstorage_soc_client", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
SOC_CLIENT = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SOC_CLIENT
SPEC.loader.exec_module(SOC_CLIENT)
_extract_metrics = SOC_CLIENT._extract_metrics
_extract_version_info = SOC_CLIENT._extract_version_info

COORDINATOR_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "apstorage_ble" / "coordinator.py"
COORDINATOR_SPEC = importlib.util.spec_from_file_location("custom_components.apstorage_ble.coordinator", COORDINATOR_PATH)
assert COORDINATOR_SPEC is not None and COORDINATOR_SPEC.loader is not None
COORDINATOR = importlib.util.module_from_spec(COORDINATOR_SPEC)
sys.modules[COORDINATOR_SPEC.name] = COORDINATOR
COORDINATOR_SPEC.loader.exec_module(COORDINATOR)
APstorageCoordinator = COORDINATOR.APstorageCoordinator


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


class _FakeConnectedClient:
    def __init__(self) -> None:
        self.is_connected = True
        self.disconnect_calls = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self.is_connected = False


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


class VersionRefreshTests(unittest.TestCase):
    def test_does_not_requery_when_version_info_is_already_complete(self) -> None:
        self.assertFalse(
            SOC_CLIENT._should_refresh_version_info(
                {
                    "pcs_firmware_version": "EZ1_1.2.3_20260418",
                    "pcs_latest_firmware_version": "EZ1_1.2.4_20260420",
                },
                now=3600,
                last_attempt=0,
            )
        )

    def test_does_not_keep_retrying_when_some_version_metadata_is_known(self) -> None:
        self.assertFalse(
            SOC_CLIENT._should_refresh_version_info(
                {"pcs_firmware_version": "EZ1_1.2.3_20260418"},
                now=30,
                last_attempt=0,
            )
        )

    def test_retries_soon_only_when_no_version_metadata_was_found(self) -> None:
        self.assertFalse(
            SOC_CLIENT._should_refresh_version_info(
                None,
                now=29,
                last_attempt=0,
            )
        )
        self.assertTrue(
            SOC_CLIENT._should_refresh_version_info(
                None,
                now=30,
                last_attempt=0,
            )
        )



class VersionQueryEfficiencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_version_query_stops_after_first_useful_response(self) -> None:
        client = SOC_CLIENT.APstorageSocClient()
        calls: list[str] = []

        async def _fake_send_property_request(
            _ble_client,
            *,
            method,
            identifier,
            storage_id,
            params_extra,
            system_id="",
            response_timeout_seconds=None,
        ):
            calls.append(identifier)
            if identifier == "pcsVersion":
                return {
                    "data": {
                        "currentVersion": "EZ1_1.2.3_20260418",
                        "latestVersion": "EZ1_1.2.4_20260420",
                    }
                }
            return {
                "data": {
                    "hardwareVersion": "HW-ELT12",
                }
            }

        client._send_property_request = _fake_send_property_request

        info = await client._query_version_info(object(), "B05000001878")

        self.assertEqual(calls, ["pcsVersion"])
        self.assertEqual(info.get("pcs_latest_firmware_version"), "EZ1_1.2.4_20260420")


class CoordinatorShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_periodic_poll_is_ignored_after_shutdown(self) -> None:
        coordinator = object.__new__(APstorageCoordinator)
        coordinator._shutdown = True

        calls: list[str] = []

        async def _fake_poll() -> None:
            calls.append("poll")

        coordinator._async_poll = _fake_poll

        await coordinator.async_periodic_poll()

        self.assertEqual(calls, [])

    def test_needs_poll_returns_false_after_shutdown(self) -> None:
        coordinator = object.__new__(APstorageCoordinator)
        coordinator._shutdown = True
        coordinator.hass = types.SimpleNamespace(state=COORDINATOR.CoreState.running)

        result = coordinator._needs_poll(COORDINATOR.BluetoothServiceInfoBleak(), None)

        self.assertFalse(result)


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


class SessionResetRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_query_metrics_clears_blufi_session_state_after_success(self) -> None:
        client = SOC_CLIENT.APstorageSocClient()
        fake_ble_client = _FakeConnectedClient()

        async def _fake_establish_connection(*args, **kwargs):
            return fake_ble_client

        async def _fake_ensure_services_ready(_client) -> None:
            return None

        async def _fake_query_soc_once(_client, _ble_device, *, device_name_hint=None):
            client.session_key = b"0123456789abcdef"
            client.parsed_frames = [SOC_CLIENT.BlufiFrame(1, 19, 0, 1, b"payload")]
            client._frame_cursor = 1
            return SOC_CLIENT.SocMetrics(battery_soc=77.0)

        original_establish_connection = SOC_CLIENT.establish_connection
        original_has_crypto = SOC_CLIENT.HAS_CRYPTO
        client._ensure_services_ready = _fake_ensure_services_ready
        client._query_soc_once = _fake_query_soc_once
        ble_device = types.SimpleNamespace(address="AA:BB:CC:DD:EE:FF")

        try:
            SOC_CLIENT.HAS_CRYPTO = True
            SOC_CLIENT.establish_connection = _fake_establish_connection
            metrics = await client.async_query_metrics(ble_device, max_retries=1)
        finally:
            SOC_CLIENT.establish_connection = original_establish_connection
            SOC_CLIENT.HAS_CRYPTO = original_has_crypto

        self.assertIsNotNone(metrics)
        self.assertEqual(metrics.battery_soc, 77.0)
        self.assertIsNone(client.session_key)
        self.assertEqual(client.parsed_frames, [])
        self.assertEqual(client._frame_cursor, 0)
        self.assertEqual(fake_ble_client.disconnect_calls, 1)


if __name__ == "__main__":
    unittest.main()
