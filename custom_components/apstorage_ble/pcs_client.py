"""Bluetooth LE client for the APsystems APstorage PCS.

Protocol (see APSTORAGE_BLE_SPEC.md, verified on an ELT-12):
  GATT     service 0xFFEC, write 0xFF07, notify 0xFF06
  framing  Espressif BluFi custom-data frames (type 0x4D), no BluFi security
  payload  JSON, AES-128-CBC with static key/IV and zero padding;
           phone->device as hex text, device->phone as raw ciphertext

Each operation opens one connection, sends its requests one at a time and
disconnects, so the PCS is free for the phone app between polls.
"""
from __future__ import annotations

import asyncio
import binascii
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import logging
import time
from typing import Any

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

WRITE_CHAR = "0000ff07-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "0000ff06-0000-1000-8000-00805f9b34fb"
NAME_PREFIX = "PCS_"

AES_KEY = b"E7MiPPrs9v6i3DY3"
AES_IV = b"8914934610490056"

TYPE_CUSTOM_DATA = 0x4D  # DATA frame, subtype 19
TYPE_ERROR = 0x49  # DATA frame, subtype 18
FC_ENCRYPTED = 0x01
FC_CHECKSUM = 0x02
FC_FRAGMENT = 0x10
MAX_FRAME = 259  # keeps the 1-byte payload length <= 255
FRAGMENT_DELAY_SECONDS = 0.01

REQUEST_TIMEOUT_SECONDS = 15.0
# Optional reads that older or newer firmware may not answer.
OPTIONAL_REQUEST_TIMEOUT_SECONDS = 10.0
# Storage configuration (capacity, model): older firmware, then newer firmware.
CONFIG_INFO_IDENTIFIERS = ("getStorageConfigurationInfo", "storageBasicInfo")
CONNECT_ATTEMPTS = 3

# Fixed envelope values the app sends over BLE (§5.1).
_ENVELOPE = {
    "company": "apsystems",
    "companyKey": "AmS4SV9oy3gk",
    "productKey": "PCS",
    "version": "1.0",
    "id": "2972245456",
    "eid": "2972245456",
    "type": "property",
}
_PARAMS = {"T": "APS", "V": "01", "userId": "", "EID": "2972245456"}


class PCSError(Exception):
    """Communication with the PCS failed."""


class PCSDeviceError(PCSError):
    """The PCS answered with an error code."""

    def __init__(self, identifier: str, code: Any, message: str) -> None:
        super().__init__(f"{identifier}: device error {code} {message}".strip())
        self.code = code
        self.message = message


# ------------------------------------------------------------------ crypto


def encrypt_request(obj: dict[str, Any]) -> bytes:
    """JSON -> zero-padded AES-CBC -> lowercase hex ASCII (what the app sends)."""
    raw = json.dumps(obj, separators=(",", ":")).encode()
    raw += b"\0" * (-len(raw) % 16)
    enc = Cipher(algorithms.AES(AES_KEY), modes.CBC(AES_IV)).encryptor()
    return (enc.update(raw) + enc.finalize()).hex().encode()


def decrypt_reply(payload: bytes) -> dict[str, Any]:
    """Raw ciphertext (or hex text, tolerated) -> dict."""
    data = bytes(payload)
    try:  # tolerate a device that answers in hex like the phone does
        text = data.decode("ascii")
        if len(text) % 32 == 0 and all(c in "0123456789abcdefABCDEF" for c in text):
            data = binascii.unhexlify(text)
    except UnicodeDecodeError:
        pass
    if len(data) % 16:
        raise PCSError(f"ciphertext length {len(data)} is not a multiple of 16")
    dec = Cipher(algorithms.AES(AES_KEY), modes.CBC(AES_IV)).decryptor()
    plain = (dec.update(data) + dec.finalize()).decode("utf-8", errors="replace")
    plain = plain.strip("\x00 \r\n\t")
    try:
        reply = json.loads(plain[plain.find("{") : plain.rfind("}") + 1])
    except ValueError as err:
        raise PCSError(f"reply is not JSON: {plain[:80]!r}") from err
    if not isinstance(reply, dict):
        raise PCSError("reply is not a JSON object")
    return reply


# ------------------------------------------------------------------ BluFi framing


def build_frames(
    frame_type: int, payload: bytes, seq_start: int, mtu: int | None
) -> tuple[list[bytes], int]:
    """Split payload into BluFi frames (no encryption, no checksum, no ack)."""
    max_frame = min(mtu - 4, MAX_FRAME) if mtu and mtu > 24 else 20
    chunk_size = max_frame - 6
    frames: list[bytes] = []
    seq, i = seq_start, 0
    while True:
        part = payload[i : i + chunk_size]
        i += len(part)
        rest = len(payload) - i
        if 0 < rest <= 2:  # same tail rule as the app
            part += payload[i:]
            i, rest = len(payload), 0
        if rest:
            body = (len(part) + rest).to_bytes(2, "little") + part
            fc = FC_FRAGMENT
        else:
            body, fc = part, 0
        frames.append(bytes([frame_type, fc, seq & 0xFF, len(body)]) + body)
        seq += 1
        if not rest:
            return frames, seq


class FrameAssembler:
    """Reassemble device->phone BluFi notifications into complete messages."""

    def __init__(self) -> None:
        self.expected_seq = 0
        self.buf = bytearray()

    def feed(self, frame: bytes) -> tuple[int, bytes] | None:
        """Return (type, payload) when a message completes, else None."""
        if len(frame) < 4:
            raise PCSError("short frame")
        ftype, fc, seq, length = frame[0], frame[1], frame[2], frame[3]
        if seq != self.expected_seq & 0xFF:
            # Resynchronise on the device's counter; drop any half message.
            expected = self.expected_seq & 0xFF
            self.expected_seq = seq + 1
            self.buf = bytearray()
            raise PCSError(f"sequence {seq}, expected {expected}")
        self.expected_seq += 1
        if fc & FC_ENCRYPTED:
            raise PCSError("BluFi-encrypted frame; this client does not negotiate BluFi security")
        body = bytes(frame[4 : 4 + length])
        if fc & FC_FRAGMENT:
            body = body[2:]
        self.buf += body
        if fc & FC_FRAGMENT:
            return None
        msg, self.buf = bytes(self.buf), bytearray()
        return ftype, msg


# ------------------------------------------------------------------ requests


def device_id_from_name(name: str | None) -> str:
    """'PCS_B05000000000' -> 'B05000000000'."""
    if name and name.upper().startswith(NAME_PREFIX):
        return name[len(NAME_PREFIX) :].strip()
    return ""


def make_request(identifier: str, device_id: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the request envelope; the method follows the identifier's prefix (§5.1)."""
    method = "set" if identifier.startswith(("set", "pcsUpdate")) else "get"
    wire = identifier[4:] if identifier.startswith(("get/", "set/")) else identifier
    request_params = dict(params or {})
    request_params.update(_PARAMS)
    if wire == "getStorageConfigurationInfo":
        request_params["V"] = "2"  # the one BLE request the app sends with V "2" (§5.1)
    # Same key order as tools/apstorage_ble_soc.py, which is verified on hardware.
    return {
        "company": _ENVELOPE["company"],
        "companyKey": _ENVELOPE["companyKey"],
        "productKey": _ENVELOPE["productKey"],
        "version": _ENVELOPE["version"],
        "id": _ENVELOPE["id"],
        "eid": _ENVELOPE["eid"],
        "deviceId": device_id,
        "type": _ENVELOPE["type"],
        "method": method,
        "identifier": wire,
        "params": request_params,
    }


def reply_data(reply: dict[str, Any]) -> Any:
    """Return the payload of a reply (`messagedata` first, then `data`)."""
    data = reply.get("messagedata")
    if data in (None, ""):
        data = reply.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            pass
    return data


def reply_is_success(reply: dict[str, Any]) -> bool:
    """200 is success; legacy replies use result "0" or code 1."""
    if str(reply.get("code")) in ("200", "1"):
        return True
    return str(reply.get("result")) == "0"


class PCSConnection:
    """One open GATT connection: send a request, wait for its reply."""

    def __init__(self, client: BleakClientWithServiceCache, device_id: str) -> None:
        self.client = client
        self.device_id = device_id
        self.tx_seq = 0
        self.rx = FrameAssembler()
        self.queue: asyncio.Queue[tuple[int | str, bytes]] = asyncio.Queue()
        self._write_with_response = True

    @property
    def mtu(self) -> int:
        """Negotiated ATT MTU (23 when unknown)."""
        return getattr(self.client, "mtu_size", 23) or 23

    def on_notify(self, _sender: Any, data: bytearray) -> None:
        """Feed one notification into the reassembler."""
        try:
            msg = self.rx.feed(bytes(data))
        except PCSError as err:
            _LOGGER.debug("Dropped BLE frame %s: %s", bytes(data).hex(), err)
            self.queue.put_nowait(("error", str(err).encode()))
            return
        if msg:
            self.queue.put_nowait(msg)

    async def _write(self, frame: bytes) -> None:
        try:
            await self.client.write_gatt_char(WRITE_CHAR, frame, response=self._write_with_response)
        except BleakError:
            if not self._write_with_response:
                raise
            # Some proxies only support write-without-response.
            _LOGGER.debug("Write with response failed; retrying without response")
            self._write_with_response = False
            await self.client.write_gatt_char(WRITE_CHAR, frame, response=False)

    async def call(
        self,
        identifier: str,
        params: dict[str, Any] | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        """Send one request and return the decrypted reply."""
        request = make_request(identifier, self.device_id, params)
        wire = request["identifier"]
        # Replies to an earlier, timed-out request must not answer this one.
        while not self.queue.empty():
            self.queue.get_nowait()
        frames, self.tx_seq = build_frames(
            TYPE_CUSTOM_DATA, encrypt_request(request), self.tx_seq, self.mtu
        )
        _LOGGER.debug("-> %s (%d frames)", wire, len(frames))
        for index, frame in enumerate(frames):
            await self._write(frame)
            if index + 1 < len(frames):
                await asyncio.sleep(FRAGMENT_DELAY_SECONDS)

        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise PCSError(f"no reply to {wire} within {timeout:g}s")
            try:
                ftype, msg = await asyncio.wait_for(self.queue.get(), left)
            except asyncio.TimeoutError:
                raise PCSError(f"no reply to {wire} within {timeout:g}s") from None
            if ftype == "error":
                continue  # a corrupt frame; the reply may still follow
            if ftype == TYPE_ERROR:
                raise PCSError(f"device reported BluFi error {msg[0] if msg else '?'}")
            if ftype != TYPE_CUSTOM_DATA:
                continue  # ACKs or other BluFi frames
            reply = decrypt_reply(msg)
            _LOGGER.debug("<- %s", reply)
            if "identifier" not in reply and not reply_is_success(reply):
                # Error replies carry no identifier, e.g. {"code":202,"message":"DEVICE ID MISMATCH"}.
                raise PCSDeviceError(wire, reply.get("code"), str(reply.get("message") or ""))
            reply_identifier = str(reply.get("identifier", ""))
            if not reply_identifier or reply_identifier.lower() == wire.lower():
                return reply

    async def get(self, identifier: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send a request and return its `data` dict, raising on an error code."""
        reply = await self.call(identifier, params)
        if not reply_is_success(reply):
            raise PCSDeviceError(identifier, reply.get("code"), str(reply.get("message") or ""))
        data = reply_data(reply)
        if isinstance(data, list):
            data = next((item for item in data if isinstance(item, dict)), {})
        return data if isinstance(data, dict) else {}


@dataclass
class PCSReadResult:
    """Replies collected during one poll."""

    device_id: str
    last_data: dict[str, Any]
    device_info: dict[str, Any] | None = None
    config_info: dict[str, Any] | None = None
    firmware: dict[str, Any] | None = None  # pcsVersion: current/latest firmware
    update_check_sent: bool = False
    sysmode: dict[str, Any] | None = None
    mtu: int | None = None


class PCSClient:
    """Connects to one PCS and runs read and write operations on it."""

    def __init__(self, name: str | None = None) -> None:
        self._name = name
        # Learned from deviceInfo (`eid`); the advertised name is the fallback.
        self.device_id: str = device_id_from_name(name)
        self.last_mtu: int | None = None
        # The config-info identifier this PCS answered, once known.
        self._config_identifier: str | None = None

    def set_name(self, name: str | None) -> None:
        """Update the advertised name, e.g. once the device has been seen."""
        self._name = name
        if not self.device_id:
            self.device_id = device_id_from_name(name)

    @asynccontextmanager
    async def connect(self, ble_device: BLEDevice) -> AsyncIterator[PCSConnection]:
        """Open a connection with notifications enabled; always disconnects."""
        if not self.device_id:
            self.device_id = device_id_from_name(ble_device.name)
        if not self.device_id:
            raise PCSError("unknown device ID: the advertised name should be PCS_<id>")
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                self._name or ble_device.name or ble_device.address,
                max_attempts=CONNECT_ATTEMPTS,
            )
        except (BleakError, asyncio.TimeoutError) as err:
            raise PCSError(f"connect failed: {err}") from err
        try:
            backend = getattr(client, "_backend", None)
            if hasattr(backend, "_acquire_mtu"):  # BlueZ: learn the negotiated MTU
                try:
                    await backend._acquire_mtu()  # noqa: SLF001
                except Exception:  # noqa: BLE001
                    pass
            conn = PCSConnection(client, self.device_id)
            self.last_mtu = conn.mtu
            await client.start_notify(NOTIFY_CHAR, conn.on_notify)
            yield conn
        except BleakError as err:
            raise PCSError(f"BLE error: {err}") from err
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    def _learn_device_id(self, conn: PCSConnection, info: dict[str, Any]) -> None:
        device_id = str(info.get("eid") or info.get("EID") or info.get("deviceId") or "").strip()
        if device_id:
            self.device_id = conn.device_id = device_id

    async def async_read(
        self,
        ble_device: BLEDevice,
        *,
        device_info: bool = False,
        sysmode: bool = True,
        firmware: bool = False,
        request_update_check: bool = False,
    ) -> PCSReadResult:
        """Read live data, plus deviceInfo, getsysmode and pcsVersion when asked, in one connection.

        `request_update_check` sends set/checkPcsVersion: the PCS then asks the APsystems
        cloud for newer firmware itself (it needs Wi-Fi); pcsVersion shows the result after
        about 25 s. Firmware is never installed from here (pcsUpdate is not sent).
        """
        async with self.connect(ble_device) as conn:
            info: dict[str, Any] | None = None
            config: dict[str, Any] | None = None
            if device_info:
                info = await conn.get("deviceInfo")
                self._learn_device_id(conn, info)
                config = await self._read_config_info(conn)
            last_data = await conn.get("getDeviceLastDataLocal")
            mode_data: dict[str, Any] | None = None
            if sysmode:
                try:
                    mode_data = await conn.get("getsysmode")
                except PCSError as err:
                    # Live data is what matters; keep it even if this read fails.
                    _LOGGER.debug("getsysmode failed: %s", err)
            version: dict[str, Any] | None = None
            if firmware:
                version = await self._read_optional(conn, "pcsVersion")
            check_sent = False
            if request_update_check:
                check_sent = await self._send_optional(conn, "set/checkPcsVersion")
            return PCSReadResult(
                device_id=conn.device_id,
                last_data=last_data,
                device_info=info,
                config_info=config,
                firmware=version,
                update_check_sent=check_sent,
                sysmode=mode_data,
                mtu=conn.mtu,
            )

    async def _read_optional(self, conn: PCSConnection, identifier: str) -> dict[str, Any] | None:
        """A read that may fail without failing the poll."""
        try:
            reply = await conn.call(identifier, timeout=OPTIONAL_REQUEST_TIMEOUT_SECONDS)
        except PCSError as err:
            _LOGGER.debug("%s failed: %s", identifier, err)
            return None
        if not reply_is_success(reply):
            _LOGGER.debug("%s: code %s %s", identifier, reply.get("code"), reply.get("message"))
            return None
        data = reply_data(reply)
        if isinstance(data, list):
            data = next((item for item in data if isinstance(item, dict)), None)
        return data if isinstance(data, dict) else None

    async def _send_optional(self, conn: PCSConnection, identifier: str) -> bool:
        """A set command whose failure doesn't fail the poll; True if the PCS accepted it."""
        try:
            reply = await conn.call(identifier, timeout=OPTIONAL_REQUEST_TIMEOUT_SECONDS)
        except PCSError as err:
            _LOGGER.debug("%s failed: %s", identifier, err)
            return False
        ok = reply_is_success(reply)
        if not ok:
            _LOGGER.debug("%s: code %s %s", identifier, reply.get("code"), reply.get("message"))
        return ok

    async def _read_config_info(self, conn: PCSConnection) -> dict[str, Any] | None:
        """Storage configuration (capacity, model); None if the PCS answers neither request."""
        identifiers = (self._config_identifier,) if self._config_identifier else CONFIG_INFO_IDENTIFIERS
        for identifier in identifiers:
            data = await self._read_optional(conn, identifier)
            if data is not None:
                self._config_identifier = identifier
                return data
        return None

    async def async_get(self, ble_device: BLEDevice, identifier: str) -> dict[str, Any]:
        """Run one read request and return its data."""
        async with self.connect(ble_device) as conn:
            return await conn.get(identifier)

    async def async_command(
        self,
        ble_device: BLEDevice,
        identifier: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one set command; raises PCSDeviceError unless the PCS confirms it."""
        async with self.connect(ble_device) as conn:
            reply = await conn.call(identifier, params)
        if not reply_is_success(reply):
            raise PCSDeviceError(identifier, reply.get("code"), str(reply.get("message") or ""))
        return reply

    async def async_update_sysmode(
        self,
        ble_device: BLEDevice,
        mutate: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        """getsysmode -> mutate -> setsysmode -> getsysmode; returns the read-back settings.

        `mutate` changes the settings in place and may raise to refuse the change.
        """
        async with self.connect(ble_device) as conn:
            current = await conn.get("getsysmode")
            payload = {key: value for key, value in current.items() if value is not None}
            mutate(payload)
            reply = await conn.call("setsysmode", payload)
            if not reply_is_success(reply):
                raise PCSDeviceError("setsysmode", reply.get("code"), str(reply.get("message") or ""))
            return await conn.get("getsysmode")
