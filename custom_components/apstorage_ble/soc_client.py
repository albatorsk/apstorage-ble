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
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError

_LOGGER = logging.getLogger(__name__)

# Static AES key and IV used by the protocol
AES_KEY_STR = "E7MiPPrs9v6i3DY3"
AES_IV_STR = "8914934610490056"

# Blufi DH parameters (standard Blufi spec)
BLUFI_DH_P_HEX = (
    "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD129024E088A67CC74"
    "020BBEA63B139B22514A08798E3404DDEF9519B3CD3A431B302B0A6DF25F1437"
    "4FE1356D6D51C245E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED"
    "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE65381FFFFFFFFFFFFFFFF"
)
BLUFI_DH_G = 2

# BLE characteristic UUIDs for custom protocol
WRITE_CHAR = "0000ff07-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "0000ff06-0000-1000-8000-00805f9b34fb"
DEVICE_NAME_CHAR = "00002a00-0000-1000-8000-00805f9b34fb"

# Timeouts
CONNECT_TIMEOUT_SECONDS = 10
RESPONSE_TIMEOUT_SECONDS = 30

try:
    from Crypto.Cipher import AES
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


def _make_cmd(frame_type: int, subtype: int) -> int:
    """Encode Blufi frame type and subtype into command byte."""
    return (frame_type & 0x03) | ((subtype & 0x3F) << 2)


class BlufiCodec:
    """Blufi MTU-based packet builder/parser."""

    def __init__(self, mtu: int = 256) -> None:
        self.mtu = mtu

    def build_packets(
        self,
        cmd: int,
        payload: bytes,
        *,
        encrypt: bool = False,
        checksum: bool = False,
        aes_key: bytes | None = None,
    ) -> list[bytes]:
        """Fragment payload into MTU-bounded Blufi packets."""
        packets = []
        seq = 0
        remaining = payload
        more_flag = 0x10  # Fragmentation flag bit 4

        while remaining:
            chunk_size = self.mtu - 4 - (2 if checksum else 0)
            chunk = remaining[:chunk_size]
            remaining = remaining[chunk_size:]

            # Last fragment clears fragmentation flag
            flags = (0x01 if encrypt else 0x00) | (0x02 if checksum else 0x00)
            if remaining:
                flags |= more_flag

            frame = bytes([cmd, flags, seq, len(chunk)]) + chunk

            # Checksum if needed
            if checksum:
                crc = self._crc16(frame)
                frame += crc.to_bytes(2, "little")

            # Encrypt if needed
            if encrypt and aes_key:
                frame = self._encrypt_frame(frame, aes_key)

            packets.append(frame)
            seq += 1

        return packets

    def parse_notify(
        self, raw: bytes, aes_key: bytes | None = None
    ) -> dict[str, Any] | None:
        """Parse a Blufi notification frame."""
        if len(raw) < 4:
            return None

        cmd = raw[0]
        flags = raw[1]
        seq = raw[2]
        data_len = raw[3]

        is_encrypted = bool(flags & 0x01)
        has_checksum = bool(flags & 0x02)
        is_fragmented = bool(flags & 0x10)

        payload_start = 4
        payload_end = payload_start + data_len
        payload = raw[payload_start:payload_end]

        if is_encrypted and aes_key:
            payload = self._decrypt_frame(payload, aes_key)

        return {
            "cmd": cmd,
            "flags": flags,
            "seq": seq,
            "encrypted": is_encrypted,
            "checksum": has_checksum,
            "fragmented": is_fragmented,
            "payload": payload,
        }

    @staticmethod
    def _crc16(data: bytes) -> int:
        """CRC16-CCITT."""
        crc = 0x0000
        for byte in data:
            crc ^= byte << 8
            for _ in range(8):
                crc <<= 1
                if crc & 0x10000:
                    crc ^= 0x1021
                crc &= 0xFFFF
        return crc

    @staticmethod
    def _encrypt_frame(frame: bytes, key: bytes) -> bytes:
        """AES/CFB/128 encryption (Blufi standard)."""
        iv = bytes(16)
        cipher = AES.new(key, AES.MODE_CFB, iv=iv, segment_size=128)
        return cipher.encrypt(frame)

    @staticmethod
    def _decrypt_frame(frame: bytes, key: bytes) -> bytes:
        """AES/CFB/128 decryption."""
        iv = bytes(16)
        cipher = AES.new(key, AES.MODE_CFB, iv=iv, segment_size=128)
        return cipher.decrypt(frame)


def _ema_encrypt_json(request_json: str) -> bytes:
    """Encrypt JSON with AES/CBC/NoPadding + zero-padding."""
    if not HAS_CRYPTO:
        raise RuntimeError("pycryptodome required")

    data = request_json.encode("utf-8")
    # Zero padding to 16-byte boundary
    pad_len = (16 - (len(data) % 16)) % 16 or 16
    data = data + bytes(pad_len)

    key = (AES_KEY_STR.ljust(32, "\x00")).encode("utf-8")[:32]
    iv = (AES_IV_STR.ljust(16, "\x00")).encode("utf-8")[:16]

    cipher = AES.new(key, AES.MODE_CBC, iv=iv)
    return cipher.encrypt(data)


def _ema_encrypt_json_hexascii(request_json: str) -> bytes:
    """Encrypt JSON and encode result as hex-ASCII (matching EMA app)."""
    ciphertext = _ema_encrypt_json(request_json)
    return hex(int.from_bytes(ciphertext, "big"))[2:].encode("ascii")


def _ema_decrypt_payload(payload: bytes) -> str:
    """Decrypt custom payload with AES/CBC/NoPadding."""
    if not HAS_CRYPTO:
        raise RuntimeError("pycryptodome required")

    key = (AES_KEY_STR.ljust(32, "\x00")).encode("utf-8")[:32]
    iv = (AES_IV_STR.ljust(16, "\x00")).encode("utf-8")[:16]

    cipher = AES.new(key, AES.MODE_CBC, iv=iv)
    decrypted = cipher.decrypt(payload)
    return decrypted.rstrip(b"\x00").decode("utf-8", errors="ignore")


def _derive_storage_ids_from_name(device_name: str | None) -> list[str]:
    """Extract storage serial from device name (e.g., 'PCS_B050XXXXXXXX' -> 'B050XXXXXXXX')."""
    if not device_name:
        return []

    out: list[str] = [device_name]

    if "_" in device_name:
        suffix = device_name.split("_", 1)[1].strip()
        if suffix and suffix not in out:
            out.append(suffix)

    if device_name.startswith("PCS"):
        trimmed = device_name.removeprefix("PCS").lstrip("_").strip()
        if trimmed and trimmed not in out:
            out.append(trimmed)

    return out


class APstorageSocClient:
    """Query APstorage battery SoC via Blufi encrypted custom payload."""

    def __init__(self) -> None:
        self.session_key: bytes | None = None
        self.parsed_frames: list[dict[str, Any]] = []
        self._frame_cursor = 0

    async def async_query_soc(self, ble_device: BLEDevice) -> int | None:
        """Connect to device, query SoC, return percentage or None on error."""
        if not HAS_CRYPTO:
            _LOGGER.error("pycryptodome required; install with: pip install pycryptodome")
            return None

        try:
            async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
                async with BleakClient(ble_device) as client:
                    return await self._query_soc_once(client)
        except asyncio.TimeoutError:
            _LOGGER.warning("SoC query timeout for %s", ble_device.address)
            return None
        except BleakError as err:
            _LOGGER.warning("BLE error during SoC query: %s", err)
            return None
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Unexpected error during SoC query: %s", err, exc_info=True)
            return None

    async def _query_soc_once(self, client: BleakClient) -> int | None:
        """Execute full SoC query sequence."""
        # 1. Read device name
        try:
            name_raw = await client.read_gatt_char(DEVICE_NAME_CHAR)
            device_name = bytes(name_raw).decode("utf-8", errors="ignore").strip("\x00\r\n ")
        except Exception:  # noqa: BLE001
            device_name = ""

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

        # 3. Send SoC query request
        try:
            soc_value = await self._send_soc_request(client, storage_ids[0])
            return int(soc_value) if soc_value else None
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to query SoC: %s", err)
            return None

    async def _establish_blufi_session(self, client: BleakClient) -> None:
        """Perform Blufi DH and security setup."""
        codec = BlufiCodec(mtu=256)

        # Generate DH keypair
        p = int(BLUFI_DH_P_HEX, 16)
        g = BLUFI_DH_G
        priv = secrets.randbelow(p)
        pub = pow(g, priv, p)
        pub_hex = format(pub, "x")
        if len(pub_hex) % 2:
            pub_hex = "0" + pub_hex

        # DH handshake
        cmd_nego = _make_cmd(1, 0)
        packets = codec.build_packets(cmd_nego, bytes.fromhex(pub_hex), encrypt=False, checksum=False)

        self.parsed_frames = []
        await client.start_notify(NOTIFY_CHAR, self._on_notify)

        try:
            for pkt in packets:
                await client.write_gatt_char(WRITE_CHAR, pkt, response=True)
                await asyncio.sleep(0.01)

            # Wait for device public key response
            frame = await self._wait_frame(1, 0, RESPONSE_TIMEOUT_SECONDS)
            dev_pub = int(frame["payload"].hex(), 16)
            shared = pow(dev_pub, priv, p)
            shared_hex = format(shared, "x")
            if len(shared_hex) % 2:
                shared_hex = "0" + shared_hex

            self.session_key = hashlib.md5(bytes.fromhex(shared_hex)).digest()

            # Set security mode (checksum + encrypt)
            cmd_sec = _make_cmd(0, 1)
            sec_packets = codec.build_packets(cmd_sec, bytes([0x03]), encrypt=False, checksum=True, aes_key=self.session_key)
            for pkt in sec_packets:
                await client.write_gatt_char(WRITE_CHAR, pkt, response=True)
                await asyncio.sleep(0.01)

        finally:
            await client.stop_notify(NOTIFY_CHAR)

    async def _send_soc_request(self, client: BleakClient, storage_id: str) -> str | None:
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
                "systemId": "",
                "storageId": storage_id,
            },
        }

        request_json = json.dumps(request, separators=(",", ":"))
        payload = _ema_encrypt_json_hexascii(request_json)

        codec = BlufiCodec(mtu=256)
        cmd_custom = _make_cmd(1, 19)
        packets = codec.build_packets(cmd_custom, payload, encrypt=True, checksum=True, aes_key=self.session_key)

        self.parsed_frames = []
        await client.start_notify(NOTIFY_CHAR, self._on_notify_impl)

        try:
            for pkt in packets:
                await client.write_gatt_char(WRITE_CHAR, pkt, response=True)
                await asyncio.sleep(0.01)

            # Wait for custom data response
            frame = await self._wait_frame(1, 19, RESPONSE_TIMEOUT_SECONDS)
            decrypted = _ema_decrypt_payload(frame["payload"])
            parsed = json.loads(decrypted)

            # Extract SoC value
            if isinstance(parsed, dict):
                data = parsed.get("data", {})
                if isinstance(data, dict):
                    soc_str = data.get("SSOC")
                    if soc_str:
                        return str(soc_str)

            return None

        finally:
            await client.stop_notify(NOTIFY_CHAR)

    def _on_notify_impl(self, _sender: Any, data: bytearray) -> None:
        """Internal notification accumulation."""
        raw = bytes(data)
        try:
            codec = BlufiCodec()
            frame = codec.parse_notify(raw, aes_key=self.session_key)
            if frame:
                self.parsed_frames.append(frame)
        except Exception:  # noqa: BLE001
            pass

    async def _wait_frame(
        self, frame_type: int, subtype: int, timeout_seconds: float
    ) -> dict[str, Any]:
        """Wait for a specific frame type/subtype."""
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout_seconds:
            for frame in self.parsed_frames:
                cmd = frame["cmd"]
                actual_type = cmd & 0x03
                actual_subtype = (cmd & 0xFC) >> 2
                if actual_type == frame_type and actual_subtype == subtype:
                    self.parsed_frames.remove(frame)
                    return frame

            await asyncio.sleep(0.05)

        raise TimeoutError(f"No frame type={frame_type} subtype={subtype} received")
