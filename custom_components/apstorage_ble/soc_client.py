"""APstorage SoC query via Blufi encrypted custom payload.

This module uses the PCS protocol to query
the APstorage battery State of Charge (SoC) over BLE using:
  1. Blufi DH key exchange for session key derivation
  2. AES/CFB encryption for Blufi frame payloads
  3. Custom data command (type=1, subtype=19) carrying AES/CBC encrypted JSON
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from dataclasses import dataclass
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

_LOGGER = logging.getLogger(__name__)

# Static AES key and IV used by the protocol
AES_KEY_STR = "E7MiPPrs9v6i3DY3"
AES_IV_STR = "8914934610490056"

# Blufi DH parameters (standard Blufi spec)
BLUFI_DH_P_HEX = (
    "cf5cf5c38419a724957ff5dd323b9c45c3cdd261eb740f69aa94b8bb1a5c9640"
    "9153bd76b24222d03274e4725a5406092e9e82e9135c643cae98132b0d95f7d6"
    "5347c68afc1e677da90e51bbab5f5cf429c291b4ba39c6b2dc5e8c7231e46aa7"
    "728e87664532cdf547be20c9a3fa8342be6e34371a27c06f7dc0edddd2f86373"
)
BLUFI_DH_G = 2

# BLE characteristic UUIDs for custom protocol
WRITE_CHAR = "0000ff07-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "0000ff06-0000-1000-8000-00805f9b34fb"
DEVICE_NAME_CHAR = "00002a00-0000-1000-8000-00805f9b34fb"

# The PCS expects Blufi frames fragmented for the default BLE payload size.
# Keep this aligned with the known-good standalone script defaults.
BLUFI_MTU = 20

# Timeouts
CONNECT_TIMEOUT_SECONDS = 90
RESPONSE_TIMEOUT_SECONDS = 30

try:
    from Crypto.Cipher import AES
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


def _make_cmd(frame_type: int, subtype: int) -> int:
    """Encode Blufi frame type and subtype into command byte."""
    return (frame_type & 0x03) | ((subtype & 0x3F) << 2)


def _u16_le(value: int) -> bytes:
    """Encode a 16-bit integer in little-endian."""
    return bytes((value & 0xFF, (value >> 8) & 0xFF))


def _crc16_app(seed: int, data: bytes) -> int:
    """CRC16 used by APstorage app flow (poly 0x1021, inverted init/final)."""
    crc = (~seed) & 0xFFFF
    for b in data:
        crc ^= (b & 0xFF) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return (~crc) & 0xFFFF


def _aes_cfb_encrypt(key: bytes, seq: int, payload: bytes) -> bytes:
    """Blufi payload encryption with IV seeded by sequence number."""
    iv = bytes([seq & 0xFF]) + (b"\x00" * 15)
    return AES.new(key, AES.MODE_CFB, iv=iv, segment_size=128).encrypt(payload)


def _aes_cfb_decrypt(key: bytes, seq: int, payload: bytes) -> bytes:
    """Blufi payload decryption with IV seeded by sequence number."""
    iv = bytes([seq & 0xFF]) + (b"\x00" * 15)
    return AES.new(key, AES.MODE_CFB, iv=iv, segment_size=128).decrypt(payload)


def _pad_key_16(key: str) -> bytes:
    """Pad or truncate key material to 16 bytes."""
    if len(key) < 16:
        key = key + ("0" * (16 - len(key)))
    return key.encode("utf-8")[:16]


def _normalize_storage_ids(storage_id: str) -> list[str]:
    """Generate common candidate forms for a storage ID."""
    candidates = [storage_id]
    compact = storage_id.replace(":", "")
    if compact and compact not in candidates:
        candidates.append(compact)
    upper = compact.upper()
    if upper and upper not in candidates:
        candidates.append(upper)
    lower = compact.lower()
    if lower and lower not in candidates:
        candidates.append(lower)
    return candidates


def _deep_find_soc(obj: Any) -> str | None:
    """Search nested dict/list payloads for a SoC-like key."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            k = key.lower()
            if k in {"ssoc", "soc", "battery_soc", "batterysoc", "bs"}:
                if value is not None:
                    return str(value)
            nested = _deep_find_soc(value)
            if nested is not None:
                return nested
    elif isinstance(obj, list):
        for value in obj:
            nested = _deep_find_soc(value)
            if nested is not None:
                return nested
    return None


@dataclass
class BlufiFrame:
    """Parsed Blufi frame."""

    frame_type: int
    subtype: int
    flags: int
    seq: int
    payload: bytes


class BlufiCodec:
    """Blufi MTU-based packet builder/parser."""

    def __init__(self, mtu: int = 20) -> None:
        self.mtu = max(20, mtu)
        self.write_seq = -1
        self.read_seq = -1
        self._rx_buf = bytearray()
        self._rx_expect_total: int | None = None
        self._rx_hdr: tuple[int, int, int] | None = None

    def _next_write_seq(self) -> int:
        self.write_seq = (self.write_seq + 1) & 0xFF
        return self.write_seq

    @staticmethod
    def _flags(encrypt: bool, checksum: bool, frag: bool) -> int:
        flags = 0
        if encrypt:
            flags |= 0x01
        if checksum:
            flags |= 0x02
        if frag:
            flags |= 0x10
        return flags

    def _build_single_packet(
        self,
        cmd: int,
        seq: int,
        payload: bytes,
        encrypt: bool,
        checksum: bool,
        frag: bool,
        aes_key: bytes | None,
    ) -> bytes:
        if encrypt:
            if not aes_key:
                raise RuntimeError("Missing AES key for encrypted packet")
            payload_wire = _aes_cfb_encrypt(aes_key, seq, payload)
        else:
            payload_wire = payload

        flags = self._flags(encrypt=encrypt, checksum=checksum, frag=frag)
        out = bytearray((cmd & 0xFF, flags & 0xFF, seq & 0xFF, len(payload_wire) & 0xFF))
        out.extend(payload_wire)

        if checksum:
            crc = _crc16_app(0, bytes((seq & 0xFF, len(payload_wire) & 0xFF)))
            if payload:
                crc = _crc16_app(crc, payload)
            out.extend(_u16_le(crc))

        return bytes(out)

    def build_packets(
        self,
        cmd: int,
        payload: bytes,
        *,
        encrypt: bool = False,
        checksum: bool = False,
        aes_key: bytes | None = None,
    ) -> list[bytes]:
        """Fragment payload into MTU-sized Blufi packets (EMA-compatible)."""
        max_payload = self.mtu - (8 if checksum else 6)
        if max_payload < 1:
            max_payload = 1

        packets: list[bytes] = []
        cursor = 0
        total_len = len(payload)

        if total_len == 0:
            seq = self._next_write_seq()
            packets.append(
                self._build_single_packet(
                    cmd=cmd,
                    seq=seq,
                    payload=b"",
                    encrypt=encrypt,
                    checksum=checksum,
                    frag=False,
                    aes_key=aes_key,
                )
            )
            return packets

        while cursor < total_len:
            chunk_end = min(total_len, cursor + max_payload)
            chunk = payload[cursor:chunk_end]
            remaining = total_len - chunk_end

            # Avoid leaving a tiny trailer frame (1-2 bytes).
            if 0 < remaining <= 2:
                take = min(max_payload - len(chunk), remaining)
                if take > 0:
                    chunk = payload[cursor:chunk_end + take]
                    chunk_end += take
                    remaining = total_len - chunk_end

            has_more = remaining > 0
            if has_more:
                wrapped = _u16_le(total_len - cursor) + chunk
            else:
                wrapped = chunk

            seq = self._next_write_seq()
            packets.append(
                self._build_single_packet(
                    cmd=cmd,
                    seq=seq,
                    payload=wrapped,
                    encrypt=encrypt,
                    checksum=checksum,
                    frag=has_more,
                    aes_key=aes_key,
                )
            )
            cursor = chunk_end

        return packets

    def parse_notify(
        self, raw: bytes, aes_key: bytes | None = None
    ) -> BlufiFrame | None:
        """Parse a Blufi notification frame."""
        if len(raw) < 4:
            return None

        type_subtype = raw[0]
        flags = raw[1]
        seq = raw[2]
        data_len = raw[3]
        encrypt = (flags & 0x01) != 0
        checksum = (flags & 0x02) != 0
        frag = (flags & 0x10) != 0

        need = 4 + data_len + (2 if checksum else 0)
        if len(raw) < need:
            return None

        payload_wire = raw[4 : 4 + data_len]
        if encrypt:
            if not aes_key:
                raise RuntimeError("Encrypted notify received but AES key is not set")
            payload = _aes_cfb_decrypt(aes_key, seq, payload_wire)
        else:
            payload = bytes(payload_wire)

        if checksum:
            got = raw[4 + data_len] | (raw[4 + data_len + 1] << 8)
            crc = _crc16_app(0, bytes((seq & 0xFF, data_len & 0xFF)))
            if payload:
                crc = _crc16_app(crc, payload)
            if got != crc:
                raise RuntimeError(
                    f"Checksum mismatch: got=0x{got:04x} expected=0x{crc:04x}"
                )

        self.read_seq = (self.read_seq + 1) & 0xFF
        if seq != self.read_seq:
            self.read_seq = seq

        frame_type = type_subtype & 0x03
        subtype = (type_subtype >> 2) & 0x3F

        if frag:
            if len(payload) < 2:
                raise RuntimeError("Fragmented payload too short for total length header")
            frag_total = payload[0] | (payload[1] << 8)
            data = payload[2:]
            if self._rx_hdr is None:
                self._rx_hdr = (frame_type, subtype, flags)
                self._rx_expect_total = frag_total
                self._rx_buf.clear()
            self._rx_buf.extend(data)
            return None

        if self._rx_hdr is not None:
            self._rx_buf.extend(payload)
            data = bytes(self._rx_buf)
            frame_type, subtype, first_flags = self._rx_hdr
            self._rx_hdr = None
            self._rx_expect_total = None
            self._rx_buf.clear()
            return BlufiFrame(
                frame_type=frame_type,
                subtype=subtype,
                flags=first_flags,
                seq=seq,
                payload=data,
            )

        return BlufiFrame(
            frame_type=frame_type,
            subtype=subtype,
            flags=flags,
            seq=seq,
            payload=payload,
        )


def _ema_encrypt_json(request_json: str) -> bytes:
    """Encrypt JSON with AES/CBC/NoPadding + zero-padding."""
    if not HAS_CRYPTO:
        raise RuntimeError("pycryptodome required")

    data = request_json.encode("utf-8")
    if len(data) % 16 != 0:
        data += b"\x00" * (16 - (len(data) % 16))

    key = _pad_key_16(AES_KEY_STR)
    iv = (AES_IV_STR.ljust(16, "\x00")).encode("utf-8")[:16]

    cipher = AES.new(key, AES.MODE_CBC, iv=iv)
    return cipher.encrypt(data)


def _ema_encrypt_json_hexascii(request_json: str) -> bytes:
    """Encrypt JSON and encode result as hex-ASCII (matching EMA app)."""
    ciphertext = _ema_encrypt_json(request_json)
    return ciphertext.hex().encode("ascii")


def _ema_decrypt_payload(payload: bytes) -> str:
    """Decrypt custom payload with AES/CBC/NoPadding."""
    if not HAS_CRYPTO:
        raise RuntimeError("pycryptodome required")

    key = _pad_key_16(AES_KEY_STR)
    iv = (AES_IV_STR.ljust(16, "\x00")).encode("utf-8")[:16]

    cipher = AES.new(key, AES.MODE_CBC, iv=iv)
    decrypted = cipher.decrypt(payload)
    return decrypted.rstrip(b"\x00").decode("utf-8", errors="replace").strip()


def _derive_storage_ids_from_name(device_name: str | None) -> list[str]:
    """Extract storage serial from device name (e.g., 'PCS_B050XXXXXXXX' -> 'B050XXXXXXXX')."""
    if not device_name:
        return []

    out: list[str] = []

    if "_" in device_name:
        suffix = device_name.split("_", 1)[1].strip()
        if suffix and suffix not in out:
            out.append(suffix)

    if device_name.startswith("PCS"):
        trimmed = device_name.removeprefix("PCS").lstrip("_").strip()
        if trimmed and trimmed not in out:
            out.append(trimmed)

    # Keep full advertised name as a fallback after canonical serial variants.
    if device_name not in out:
        out.append(device_name)

    normalized: list[str] = []
    for item in out:
        for candidate in _normalize_storage_ids(item):
            if candidate not in normalized:
                normalized.append(candidate)
    return normalized


class APstorageSocClient:
    """Query APstorage battery SoC via Blufi encrypted custom payload."""

    def __init__(self) -> None:
        self.session_key: bytes | None = None
        self._codec = BlufiCodec(mtu=BLUFI_MTU)
        self.parsed_frames: list[BlufiFrame] = []
        self._frame_cursor = 0

    async def async_query_soc(
        self,
        ble_device: BLEDevice,
        *,
        device_name_hint: str | None = None,
    ) -> int | None:
        """Connect to device, query SoC, return percentage or None on error."""
        if not HAS_CRYPTO:
            _LOGGER.error("pycryptodome required; install with: pip install pycryptodome")
            return None

        client: BleakClient | None = None
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    ble_device.address,
                    max_attempts=3,
                    use_services_cache=True,
                )
                return await self._query_soc_once(
                    client,
                    ble_device,
                    device_name_hint=device_name_hint,
                )
        except asyncio.TimeoutError:
            _LOGGER.warning("SoC query timeout for %s", ble_device.address)
            return None
        except BleakError as err:
            _LOGGER.warning("BLE error during SoC query: %s", err)
            return None
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Unexpected error during SoC query: %s", err, exc_info=True)
            return None
        finally:
            if client and client.is_connected:
                await client.disconnect()

    async def _query_soc_once(
        self,
        client: BleakClient,
        ble_device: BLEDevice,
        *,
        device_name_hint: str | None = None,
    ) -> int | None:
        """Execute full SoC query sequence."""
        # 1. Read device name
        try:
            name_raw = await client.read_gatt_char(DEVICE_NAME_CHAR)
            device_name = bytes(name_raw).decode("utf-8", errors="ignore").strip("\x00\r\n ")
        except Exception:  # noqa: BLE001
            device_name = ""

        if not device_name:
            device_name = device_name_hint or ""

        if not device_name:
            device_name = ble_device.name or ""

        storage_ids = _derive_storage_ids_from_name(device_name)
        if not storage_ids:
            _LOGGER.warning("Could not extract storage ID from device name: %s", device_name)
            return None

        # 2. DH negotiation and session key derivation
        try:
            await self._establish_blufi_session(client)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to establish Blufi session: %s", err)
            return None

        # 3. Send SoC query request, trying common ID variants.
        for storage_id in storage_ids:
            try:
                soc_value = await self._send_soc_request(client, storage_id, system_id="")
                if soc_value is not None:
                    return int(soc_value)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("SoC query attempt failed for storage_id=%s: %s", storage_id, err)

        _LOGGER.debug("SoC not found for any storage_id candidate: %s", storage_ids)
        return None

    async def _establish_blufi_session(self, client: BleakClient) -> None:
        """Perform Blufi DH and security setup."""
        self._codec = BlufiCodec(mtu=BLUFI_MTU)

        # Generate DH keypair
        p = int(BLUFI_DH_P_HEX, 16)
        g = BLUFI_DH_G
        priv = secrets.randbelow(p - 3) + 2
        pub = pow(g, priv, p)
        p_bytes = bytes.fromhex(BLUFI_DH_P_HEX)

        g_hex = format(g, "x")
        if len(g_hex) % 2:
            g_hex = "0" + g_hex
        g_bytes = bytes.fromhex(g_hex)

        pub_hex = format(pub, "x").zfill(256)
        pub_bytes = bytes.fromhex(pub_hex)

        # DH handshake
        cmd_nego = _make_cmd(1, 0)
        nego_payload_0_len = len(p_bytes) + len(g_bytes) + len(pub_bytes) + 6
        nego_payload_0 = bytes((0, (nego_payload_0_len >> 8) & 0xFF, nego_payload_0_len & 0xFF))
        nego_payload_1 = (
            bytes((1, (len(p_bytes) >> 8) & 0xFF, len(p_bytes) & 0xFF))
            + p_bytes
            + bytes((len(g_bytes) >> 8, len(g_bytes) & 0xFF))
            + g_bytes
            + bytes((len(pub_bytes) >> 8, len(pub_bytes) & 0xFF))
            + pub_bytes
        )

        packets_0 = self._codec.build_packets(cmd_nego, nego_payload_0, encrypt=False, checksum=False)
        packets_1 = self._codec.build_packets(cmd_nego, nego_payload_1, encrypt=False, checksum=False)

        self.parsed_frames = []
        self._frame_cursor = 0
        await client.start_notify(NOTIFY_CHAR, self._on_notify)

        try:
            for pkt in packets_0 + packets_1:
                await client.write_gatt_char(WRITE_CHAR, pkt, response=True)
                await asyncio.sleep(0.01)

            # Wait for device public key response
            frame = await self._wait_frame(1, 0, RESPONSE_TIMEOUT_SECONDS)
            dev_pub = int(frame.payload.hex(), 16)
            shared = pow(dev_pub, priv, p)
            shared_hex = format(shared, "x")
            if len(shared_hex) % 2:
                shared_hex = "0" + shared_hex

            self.session_key = hashlib.md5(bytes.fromhex(shared_hex)).digest()

            # Set security mode (checksum + encrypt)
            cmd_sec = _make_cmd(0, 1)
            sec_packets = self._codec.build_packets(cmd_sec, bytes([0x03]), encrypt=False, checksum=True, aes_key=self.session_key)
            for pkt in sec_packets:
                await client.write_gatt_char(WRITE_CHAR, pkt, response=True)
                await asyncio.sleep(0.01)

        finally:
            await client.stop_notify(NOTIFY_CHAR)

    async def _send_soc_request(self, client: BleakClient, storage_id: str, system_id: str = "") -> str | None:
        """Send encrypted SoC query and parse response."""
        request = {
            "company": "apsystems",
            "companyKey": "AmS4SV9oy3gk",
            "productKey": "PCS",
            "version": "1.0",
            "id": storage_id,
            "deviceId": storage_id,
            "type": "property",
            "eid": "2972245456",
            "method": "get",
            "identifier": "getDeviceLastDataLocal",
            "params": {
                "T": "APS",
                "V": "01",
                "userId": "",
                "EID": storage_id,
                "systemId": system_id,
                "storageId": storage_id,
            },
        }

        request_json = json.dumps(request, separators=(",", ":"))
        payload = _ema_encrypt_json_hexascii(request_json)

        cmd_custom = _make_cmd(1, 19)
        packets = self._codec.build_packets(cmd_custom, payload, encrypt=True, checksum=True, aes_key=self.session_key)

        self.parsed_frames = []
        self._frame_cursor = 0
        await client.start_notify(NOTIFY_CHAR, self._on_notify_impl)

        try:
            for pkt in packets:
                await client.write_gatt_char(WRITE_CHAR, pkt, response=True)
                await asyncio.sleep(0.01)

            # Wait for custom data response
            frame = await self._wait_frame(1, 19, RESPONSE_TIMEOUT_SECONDS)
            decrypted = _ema_decrypt_payload(frame.payload)
            parsed = json.loads(decrypted)

            # Extract SoC value
            soc_str = _deep_find_soc(parsed)
            if soc_str is not None:
                return soc_str

            return None

        finally:
            await client.stop_notify(NOTIFY_CHAR)

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        """Notification callback used during DH/security setup."""
        self._on_notify_impl(_sender, data)

    def _on_notify_impl(self, _sender: Any, data: bytearray) -> None:
        """Internal notification accumulation."""
        raw = bytes(data)
        try:
            frame = self._codec.parse_notify(raw, aes_key=self.session_key)
            if frame:
                self.parsed_frames.append(frame)
        except Exception:  # noqa: BLE001
            pass

    async def _wait_frame(
        self, frame_type: int, subtype: int, timeout_seconds: float
    ) -> BlufiFrame:
        """Wait for a specific frame type/subtype."""
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout_seconds:
            while self._frame_cursor < len(self.parsed_frames):
                frame = self.parsed_frames[self._frame_cursor]
                self._frame_cursor += 1
                if frame.frame_type == frame_type and frame.subtype == subtype:
                    return frame

            await asyncio.sleep(0.05)

        raise TimeoutError(f"No frame type={frame_type} subtype={subtype} received")
