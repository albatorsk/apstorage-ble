"""Test pcs_client.py against a simulated PCS (BluFi framing + static AES, as on the ELT-12).

Needs: pip install pytest bleak bleak-retry-connector cryptography
"""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

COMP = Path(__file__).resolve().parents[1] / "custom_components" / "apstorage_ble"


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, COMP / file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pc = _load("pcs_client_under_test", "pcs_client.py")
models = _load("models_under_test", "models.py")

KEY, IV = b"E7MiPPrs9v6i3DY3", b"8914934610490056"
DEVICE_ID = "B05000000000"

LAST_DATA = {"MODE": "6", "SSOC": "98", "status": 0,
             "P0": "885", "P1": "0", "P2": "0", "P3": "989", "P4": "0", "P5": "104", "P6": "0",
             "DE0": "0.262026", "DE1": "0.000000", "DE2": "0.000000", "DE3": "0.303764",
             "DE4": "0.134232", "DE5": "0.129042",
             "T2": "8114.218750", "T3": "10080.858398", "CO2": "5966.34",
             "BUZ": "0", "BUZAlarm1": "0", "BUZAlarm2": "0", "BUZAlarm3": "0",
             "ABD": [{"ABID": DEVICE_ID, "GS": "0", "GES": "0", "IMS": "0", "SM": "10"}]}
DEVICE_INFO = {"deviceId": DEVICE_ID, "eid": DEVICE_ID, "devType": "6", "dcmVer": "0.1.66.0",
               "moduleVer1": "1.1.8.12", "moduleVer2": "1.1.6.2", "moduleVer3": "1.0.14.9",
               "capacity": "0", "ssid": "home", "rssi": "-61", "dcmIP": "192.168.1.40",
               "connetStatus": "1"}


class FakePCS:
    """Device side: reassembles phone frames, answers like the ELT-12."""

    def __init__(self, mtu=247, ack_before_reply=False, silent=(), config_info=None):
        self.mtu = mtu
        self.ack_before_reply = ack_before_reply
        self.silent = set(silent)
        # getStorageConfigurationInfo reply data; None = firmware without it.
        self.config_info = {"capacity": "20", "model": "ELT-12", "pcs_num": "1"} if config_info is None else config_info
        self.sysmode = {"mode": "6", "backupSOC": "10", "socMin": "10", "peakTime": [], "valleyTime": [],
                        "schedule": [{"peakTime": [], "valleyTime": ["024500121500"], "W": [5]}],
                        "sellingFirst": "0", "peakPower": "1500", "essStatus": "0",
                        "maxDischargePower": "0", "maxChargePower": "0", "area": "SE4", "income": "0"}
        self.requests = []
        self.notify = None
        self.rx_seq = 0
        self.tx_seq = 0
        self.buf = bytearray()
        self.frames_in = 0

    def on_write(self, frame):
        self.frames_in += 1
        ftype, fc, seq, n = frame[0], frame[1], frame[2], frame[3]
        assert seq == self.rx_seq & 0xFF, f"phone seq {seq} != {self.rx_seq}"
        assert len(frame) <= max(self.mtu - 3, 20), "frame larger than ATT payload"
        assert n == len(frame) - 4
        self.rx_seq += 1
        body = frame[4:4 + n]
        if fc & 0x10:
            body = body[2:]
        self.buf += body
        if fc & 0x10:
            return
        msg, self.buf = bytes(self.buf), bytearray()
        assert ftype == 0x4D
        ct = bytes.fromhex(msg.decode("ascii"))  # phone sends hex text
        dec = Cipher(algorithms.AES(KEY), modes.CBC(IV)).decryptor()
        req = json.loads((dec.update(ct) + dec.finalize()).rstrip(b"\0"))
        self.requests.append(req)
        asyncio.get_running_loop().call_soon(self._answer, req)

    def _answer(self, req):
        ident = req["identifier"]
        if ident in self.silent:
            return
        if req["deviceId"] != DEVICE_ID:
            return self._send({"code": 202, "message": "DEVICE ID MISMATCH", "id": "2972245456"})
        base = {"identifier": ident, "method": req["method"] + "_reply", "code": 200, "deviceId": DEVICE_ID}
        if ident == "deviceInfo":
            data = DEVICE_INFO
        elif ident == "getDeviceLastDataLocal":
            data = LAST_DATA
            base["deviceTime"] = "20261015002546"
        elif ident == "getStorageConfigurationInfo" and self.config_info:
            data = self.config_info
        elif ident == "getsysmode":
            data = self.sysmode
        elif ident == "setsysmode":
            p = {k: v for k, v in req["params"].items() if k not in ("T", "V", "userId", "EID")}
            self.sysmode.update(p)
            data = {}
        elif ident in ("buzzerMode", "ClearBuzzer", "setClearBuzzer", "pcsReboot"):
            data = {}
        else:
            return self._send({"code": 400, "message": "unknown", "identifier": ident})
        if self.ack_before_reply:
            self._raw(bytes([0x00, 0x04, self.tx_seq & 0xFF, 1, 0]))
        self._send({**base, "data": data})

    def _raw(self, frame):
        self.tx_seq += 1
        self.notify(None, bytearray(frame))

    def _send(self, reply):
        raw = json.dumps(reply).encode()
        raw += b"\0" * (-len(raw) % 16)
        enc = Cipher(algorithms.AES(KEY), modes.CBC(IV)).encryptor()
        payload = enc.update(raw) + enc.finalize()  # device sends raw ciphertext
        max_frame = min(self.mtu - 4, 259) if self.mtu > 24 else 20
        chunk = max_frame - 6
        i = 0
        while True:
            part = payload[i:i + chunk]
            i += len(part)
            rest = len(payload) - i
            if rest:
                body = (len(part) + rest).to_bytes(2, "little") + part
                fc = 0x14
            else:
                body, fc = part, 0x04
            self._raw(bytes([0x4D, fc, self.tx_seq & 0xFF, len(body)]) + body)
            if not rest:
                return


class FakeClient:
    def __init__(self, pcs):
        self.pcs = pcs
        self.mtu_size = pcs.mtu
        self.connected = True

    async def start_notify(self, char, cb):
        assert char == pc.NOTIFY_CHAR
        self.pcs.notify = cb

    async def write_gatt_char(self, char, data, response=True):
        assert char == pc.WRITE_CHAR
        self.pcs.on_write(bytes(data))

    async def disconnect(self):
        self.connected = False


class FakeDevice:
    name = "PCS_" + DEVICE_ID
    address = "AA:BB:CC:DD:EE:FF"


def run(pcs, coro_fn):
    clients = []

    async def fake_establish(cls, device, name, **kw):
        client = FakeClient(pcs)
        clients.append(client)
        pcs.rx_seq = pcs.tx_seq = 0  # counters reset per connection
        return client

    pc.establish_connection = fake_establish
    result = asyncio.run(coro_fn())
    assert clients and all(not c.connected for c in clients), "connection left open"
    return result, clients


@pytest.mark.parametrize("mtu", [23, 64, 247, 517])
@pytest.mark.parametrize("ack", [False, True])
def test_poll_reads_everything(mtu, ack):
    pcs = FakePCS(mtu=mtu, ack_before_reply=ack)
    client = pc.PCSClient("PCS_" + DEVICE_ID)
    result, clients = run(pcs, lambda: client.async_read(FakeDevice(), device_info=True, sysmode=True))
    assert len(clients) == 1, "one connection per poll"
    assert [r["identifier"] for r in pcs.requests] == [
        "deviceInfo", "getStorageConfigurationInfo", "getDeviceLastDataLocal", "getsysmode"]
    assert all(r["method"] == "get" and r["deviceId"] == DEVICE_ID for r in pcs.requests)
    assert pcs.requests[1]["params"]["V"] == "2" and pcs.requests[0]["params"]["V"] == "01"
    assert result.last_data["SSOC"] == "98"
    assert result.device_info["dcmVer"] == "0.1.66.0"
    assert result.sysmode["area"] == "SE4"

    data = models.PCSData()
    data.apply_last_data(result.last_data)
    data.apply_sysmode(result.sysmode)
    data.apply_device_info(result.device_info)
    data.apply_config_info(result.config_info)
    assert data.battery_soc == 98
    assert data.battery_power == 885 and data.battery_charging_power == 0
    assert data.signed_battery_power == 885
    assert data.battery_flow_state == "Discharging"
    assert data.load_power == 989 and data.grid_import_power == 104 and data.grid_power == 104
    assert data.grid_exported_energy == pytest.approx(0.134232)
    assert data.grid_status == "On-grid" and data.min_soc == 10
    assert data.system_mode == "6" and data.backup_soc == 10 and data.peak_power == 1500
    assert data.selling_first is False and data.price_area == "SE4"
    assert data.buzzer == 0 and data.buzzer_alarm is False
    assert data.pcs_firmware_version_1 == "1.1.8.12" and data.pcs_firmware_version_3 == "1.0.14.9"
    assert data.comm_firmware_version == "0.1.66.0"
    assert data.wifi_rssi == -61 and data.cloud_connected is True
    assert data.battery_capacity == 20  # from getStorageConfigurationInfo; deviceInfo says 0
    assert data.total_produced == pytest.approx(8114.21875)


def test_small_mtu_fragments_requests():
    pcs = FakePCS(mtu=23)
    client = pc.PCSClient("PCS_" + DEVICE_ID)
    run(pcs, lambda: client.async_read(FakeDevice(), sysmode=False))
    assert pcs.frames_in > 10  # request split into many 20-byte frames


def test_sysmode_update_merges_and_reads_back():
    pcs = FakePCS()
    client = pc.PCSClient("PCS_" + DEVICE_ID)

    def mutate(p):
        p["mode"] = "1"
        p["backupSOC"] = "30"

    result, _ = run(pcs, lambda: client.async_update_sysmode(FakeDevice(), mutate))
    idents = [r["identifier"] for r in pcs.requests]
    assert idents == ["getsysmode", "setsysmode", "getsysmode"]
    sent = pcs.requests[1]
    assert sent["method"] == "set"
    assert sent["params"]["mode"] == "1" and sent["params"]["area"] == "SE4"  # other fields kept
    assert result["mode"] == "1" and result["backupSOC"] == "30"


def test_set_identifiers_are_stripped():
    pcs = FakePCS()
    client = pc.PCSClient("PCS_" + DEVICE_ID)
    run(pcs, lambda: client.async_command(FakeDevice(), "set/buzzerMode", {"status": "1"}))
    req = pcs.requests[0]
    assert req["identifier"] == "buzzerMode" and req["method"] == "set" and req["params"]["status"] == "1"


def test_device_id_mismatch_raises():
    pcs = FakePCS()
    client = pc.PCSClient("PCS_WRONG")
    with pytest.raises(pc.PCSDeviceError) as err:
        run(pcs, lambda: client.async_read(FakeDevice()))
    assert err.value.code == 202


def test_timeout_raises_and_disconnects(monkeypatch):
    monkeypatch.setattr(pc, "REQUEST_TIMEOUT_SECONDS", 0.3)
    pcs = FakePCS(silent={"getDeviceLastDataLocal"})
    client = pc.PCSClient("PCS_" + DEVICE_ID)

    async def go():
        async with client.connect(FakeDevice()) as conn:
            await conn.call("getDeviceLastDataLocal", timeout=0.3)

    with pytest.raises(pc.PCSError):
        run(pcs, go)


def test_getsysmode_failure_keeps_live_data():
    pcs = FakePCS(silent={"getsysmode"})
    client = pc.PCSClient("PCS_" + DEVICE_ID)

    async def go():
        orig = pc.PCSConnection.call

        async def short(self, identifier, params=None, timeout=pc.REQUEST_TIMEOUT_SECONDS):
            return await orig(self, identifier, params, timeout=0.3)

        pc.PCSConnection.call = short
        try:
            return await client.async_read(FakeDevice(), sysmode=True)
        finally:
            pc.PCSConnection.call = orig

    result, _ = run(pcs, go)
    assert result.sysmode is None and result.last_data["SSOC"] == "98"


def test_config_info_falls_back_to_storage_basic_info(monkeypatch):
    monkeypatch.setattr(pc, "OPTIONAL_REQUEST_TIMEOUT_SECONDS", 0.3)
    pcs = FakePCS(config_info={})  # answers neither config request with data
    orig = pcs._answer

    def answer(req):
        if req["identifier"] == "storageBasicInfo":
            return pcs._send({"identifier": "storageBasicInfo", "code": 200, "data": {"C": "10.24", "M": "ELT-12"}})
        return orig(req)

    pcs._answer = answer
    client = pc.PCSClient("PCS_" + DEVICE_ID)
    result, _ = run(pcs, lambda: client.async_read(FakeDevice(), device_info=True))
    assert result.config_info == {"C": "10.24", "M": "ELT-12"}
    data = models.PCSData()
    data.apply_config_info(result.config_info)
    assert data.battery_capacity == 10.24
    # The answering identifier is remembered; the next read asks only that one.
    pcs.requests.clear()
    run(pcs, lambda: client.async_read(FakeDevice(), device_info=True))
    assert [r["identifier"] for r in pcs.requests][:2] == ["deviceInfo", "storageBasicInfo"]


def test_no_config_info_keeps_poll_working(monkeypatch):
    monkeypatch.setattr(pc, "OPTIONAL_REQUEST_TIMEOUT_SECONDS", 0.2)
    pcs = FakePCS(config_info={})
    client = pc.PCSClient("PCS_" + DEVICE_ID)
    result, _ = run(pcs, lambda: client.async_read(FakeDevice(), device_info=True))
    assert result.config_info is None and result.last_data["SSOC"] == "98"
