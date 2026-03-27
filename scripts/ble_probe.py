#!/usr/bin/env python3
"""BLE probing utility for APstorage ELT-12.

Examples:
  python3 scripts/ble_probe.py info --mac AA:BB:CC:DD:EE:FF
  python3 scripts/ble_probe.py monitor --mac AA:BB:CC:DD:EE:FF
    python3 scripts/ble_probe.py oneshot --mac AA:BB:CC:DD:EE:FF --cmd 01 --oneshot-wait 0.7
  python3 scripts/ble_probe.py probe --mac AA:BB:CC:DD:EE:FF --cmd 01 --cmd 02 --cmd ff
    python3 scripts/ble_probe.py probe-read --mac AA:BB:CC:DD:EE:FF --cmd 7e0100 --cmd 01
  python3 scripts/ble_probe.py sweep --mac AA:BB:CC:DD:EE:FF --start 0 --end 255
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import inspect
import secrets
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

try:
    AES = importlib.import_module("Crypto.Cipher.AES")
except Exception:  # noqa: BLE001
    try:
        AES = importlib.import_module("Cryptodome.Cipher.AES")
    except Exception:  # noqa: BLE001
        AES = None

DEFAULT_MAC = "AA:BB:CC:DD:EE:FF"
DEFAULT_NOTIFY_UUID = "0000ff06-0000-1000-8000-00805f9b34fb"
DEFAULT_WRITE_UUID = "0000ff07-0000-1000-8000-00805f9b34fb"

BLUFI_DH_P_HEX = (
    "cf5cf5c38419a724957ff5dd323b9c45c3cdd261eb740f69aa94b8bb1a5c9640"
    "9153bd76b24222d03274e4725a5406092e9e82e9135c643cae98132b0d95f7d6"
    "5347c68afc1e677da90e51bbab5f5cf429c291b4ba39c6b2dc5e8c7231e46aa7"
    "728e87664532cdf547be20c9a3fa8342be6e34371a27c06f7dc0edddd2f86373"
)
BLUFI_DH_G = 2


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _to_bytes(hex_value: str) -> bytes:
    hex_value = hex_value.strip().lower().replace("0x", "")
    if len(hex_value) % 2 != 0:
        raise ValueError(f"Hex value must have even number of chars: {hex_value}")
    return bytes.fromhex(hex_value)


def _u16_le(value: int) -> bytes:
    return bytes((value & 0xFF, (value >> 8) & 0xFF))


def _crc16_app(seed: int, data: bytes) -> int:
    """CRC16 used by EMA app (poly 0x1021, init/final as bitwise-not)."""
    crc = (~seed) & 0xFFFF
    for b in data:
        crc ^= (b & 0xFF) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return (~crc) & 0xFFFF


def _make_cmd(cmd_type: int, subtype: int) -> int:
    return (cmd_type & 0x03) | ((subtype & 0x3F) << 2)


def _aes_cfb_encrypt(key: bytes, seq: int, payload: bytes) -> bytes:
    if AES is None:
        raise RuntimeError("PyCryptodome is required for secure mode: pip install pycryptodome")
    iv = bytes([seq & 0xFF]) + (b"\x00" * 15)
    return AES.new(key, AES.MODE_CFB, iv=iv, segment_size=128).encrypt(payload)


def _aes_cfb_decrypt(key: bytes, seq: int, payload: bytes) -> bytes:
    if AES is None:
        raise RuntimeError("PyCryptodome is required for secure mode: pip install pycryptodome")
    iv = bytes([seq & 0xFF]) + (b"\x00" * 15)
    return AES.new(key, AES.MODE_CFB, iv=iv, segment_size=128).decrypt(payload)


@dataclass
class BlufiFrame:
    frame_type: int
    subtype: int
    flags: int
    seq: int
    payload: bytes


class BlufiCodec:
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

    def _flags(self, encrypt: bool, checksum: bool, frag: bool) -> int:
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
        encrypt: bool = False,
        checksum: bool = False,
        aes_key: bytes | None = None,
    ) -> list[bytes]:
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

            # Match app behavior: avoid leaving only 1-2 bytes for next frame.
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

    def parse_notify(self, raw: bytes, aes_key: bytes | None = None) -> BlufiFrame | None:
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
                    f"Checksum mismatch: got=0x{got:04x} expected=0x{crc:04x} raw={raw.hex()}"
                )

        self.read_seq = (self.read_seq + 1) & 0xFF
        if seq != self.read_seq:
            print(f"{_now()}  !! WARN seq mismatch read={seq} expected={self.read_seq}")
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
            if self._rx_expect_total is not None and len(data) != self._rx_expect_total:
                print(
                    f"{_now()}  !! WARN reassembled len={len(data)} expected={self._rx_expect_total}"
                )
            self._rx_hdr = None
            self._rx_expect_total = None
            self._rx_buf.clear()
            return BlufiFrame(frame_type=frame_type, subtype=subtype, flags=first_flags, seq=seq, payload=data)

        return BlufiFrame(frame_type=frame_type, subtype=subtype, flags=flags, seq=seq, payload=payload)


class ProbeSession:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.notifications: list[bytes] = []
        self.parsed_frames: list[BlufiFrame] = []
        self.blufi = BlufiCodec(mtu=args.blufi_mtu)
        self.secure_key: bytes | None = None

    def _try_parse_blufi(self, payload: bytes) -> None:
        try:
            frame = self.blufi.parse_notify(payload, aes_key=self.secure_key)
        except Exception as exc:  # noqa: BLE001
            print(f"{_now()}  !! BLUFI parse error: {exc}")
            return

        if frame is None:
            return

        self.parsed_frames.append(frame)

        print(
            f"{_now()}  .. BLUFI type={frame.frame_type} subtype={frame.subtype} "
            f"seq={frame.seq} len={len(frame.payload)} payload={frame.payload.hex()}"
        )

    def on_notify(self, sender: Any, data: bytearray) -> None:
        payload = bytes(data)
        self.notifications.append(payload)
        print(f"{_now()}  << NOTIFY handle={sender} len={len(payload):3d}  {payload.hex()}")
        self._try_parse_blufi(payload)

    async def _write_blufi(
        self,
        client: BleakClient,
        cmd: int,
        payload: bytes,
        encrypt: bool,
        checksum: bool,
    ) -> None:
        packets = self.blufi.build_packets(
            cmd=cmd,
            payload=payload,
            encrypt=encrypt,
            checksum=checksum,
            aes_key=self.secure_key,
        )
        for packet in packets:
            print(f"{_now()}  >> BLUFI WRITE len={len(packet):3d}  {packet.hex()}")
            await client.write_gatt_char(
                self.args.write_uuid,
                packet,
                response=not self.args.no_response,
            )
            await asyncio.sleep(self.args.delay)

    async def _wait_for_frame(self, frame_type: int, subtype: int, timeout: float) -> BlufiFrame:
        deadline = asyncio.get_event_loop().time() + timeout
        seen = 0
        while True:
            now = asyncio.get_event_loop().time()
            if now >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for BLUFI frame type={frame_type} subtype={subtype}"
                )
            if len(self.parsed_frames) == seen:
                await asyncio.sleep(0.05)
                continue

            frame = self.parsed_frames[seen]
            seen += 1
            if frame.frame_type == frame_type and frame.subtype == subtype:
                return frame

    async def run_blufi_secure_scan(self) -> None:
        p = int(BLUFI_DH_P_HEX, 16)
        g = BLUFI_DH_G
        p_bytes = bytes.fromhex(BLUFI_DH_P_HEX)
        g_hex = format(g, "x")
        if len(g_hex) % 2:
            g_hex = "0" + g_hex
        g_bytes = bytes.fromhex(g_hex)

        for attempt in range(1, self.args.secure_retries + 1):
            client: BleakClient | None = None
            try:
                self.notifications.clear()
                self.parsed_frames.clear()
                self.secure_key = None
                self.blufi = BlufiCodec(mtu=self.args.blufi_mtu)

                priv = secrets.randbelow(p - 3) + 2
                pub = pow(g, priv, p)
                pub_hex = format(pub, "x").zfill(256)
                pub_bytes = bytes.fromhex(pub_hex)

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

                print(f"Connecting to {self.args.mac}... (attempt {attempt}/{self.args.secure_retries})")
                client = await self._open_probe_client()

                cmd_negotiate = _make_cmd(1, 0)
                await self._write_blufi(client, cmd=cmd_negotiate, payload=nego_payload_0, encrypt=False, checksum=False)
                await asyncio.sleep(0.1)
                await self._write_blufi(client, cmd=cmd_negotiate, payload=nego_payload_1, encrypt=False, checksum=False)

                dev_key_frame = await self._wait_for_frame(frame_type=1, subtype=0, timeout=self.args.secure_timeout)
                dev_pub = int(dev_key_frame.payload.hex(), 16)
                shared = pow(dev_pub, priv, p)
                shared_hex = format(shared, "x")
                if len(shared_hex) % 2:
                    shared_hex = "0" + shared_hex
                self.secure_key = hashlib.md5(bytes.fromhex(shared_hex)).digest()
                print(f"{_now()}  .. derived AES key: {self.secure_key.hex()}")

                cmd_set_security = _make_cmd(0, 1)
                await self._write_blufi(
                    client,
                    cmd=cmd_set_security,
                    payload=bytes((0x03,)),
                    encrypt=False,
                    checksum=True,
                )
                await asyncio.sleep(0.2)

                cmd_scan = _make_cmd(0, 9)
                await self._write_blufi(client, cmd=cmd_scan, payload=b"", encrypt=True, checksum=True)
                print(f"{_now()}  .. secure scan request sent, waiting {self.args.tail_wait}s...")
                await asyncio.sleep(self.args.tail_wait)
                print(f"Done. Received {len(self.notifications)} notifications.")
                return
            except (BleakError, TimeoutError, RuntimeError) as exc:
                print(f"{_now()}  !! secure scan attempt {attempt} failed: {exc}")
                if attempt >= self.args.secure_retries:
                    raise
                await asyncio.sleep(0.6)
            finally:
                await self._close_probe_client(client)

    async def _open_probe_client(self) -> BleakClient:
        """Open a client connection and subscribe notifications."""
        client = BleakClient(self.args.mac, timeout=self.args.connect_timeout)
        await client.connect()
        get_services = getattr(client, "get_services", None)
        if callable(get_services):
            maybe_awaitable = get_services()
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        else:
            _ = client.services
        await client.start_notify(self.args.notify_uuid, self.on_notify)
        print(f"Subscribed to {self.args.notify_uuid}")
        return client

    async def _close_probe_client(self, client: BleakClient | None) -> None:
        """Best-effort client cleanup."""
        if client is None:
            return
        try:
            if client.is_connected:
                try:
                    await client.stop_notify(self.args.notify_uuid)
                except Exception:  # noqa: BLE001
                    pass
                await client.disconnect()
        except Exception:  # noqa: BLE001
            pass

    async def run_info(self) -> None:
        print(f"Connecting to {self.args.mac}...")
        async with BleakClient(self.args.mac, timeout=self.args.connect_timeout) as client:
            print(f"Connected: {client.is_connected}")
            for service in client.services:
                print(f"\nSERVICE: {service.uuid}")
                if service.description:
                    print(f"         {service.description}")
                for char in service.characteristics:
                    props = ", ".join(char.properties)
                    print(f"  CHAR:  {char.uuid}")
                    print(f"         props: [{props}]")
                    if "read" in char.properties:
                        try:
                            val = bytes(await client.read_gatt_char(char.uuid))
                            print(f"         value: {val.hex()} | {list(val)}")
                        except Exception as exc:  # noqa: BLE001
                            print(f"         read error: {exc}")

    async def run_monitor(self) -> None:
        print(f"Connecting to {self.args.mac} and subscribing to {self.args.notify_uuid}...")
        async with BleakClient(self.args.mac, timeout=self.args.connect_timeout) as client:
            await client.start_notify(self.args.notify_uuid, self.on_notify)
            print("Subscribed. Waiting for notifications (Ctrl+C to stop)...")
            await asyncio.sleep(self.args.monitor_seconds)
            await client.stop_notify(self.args.notify_uuid)
            print(f"Stopped. Received {len(self.notifications)} notifications.")

    async def run_probe(self) -> None:
        commands = [_to_bytes(cmd) for cmd in self.args.cmd]
        print(f"Connecting to {self.args.mac}...")
        client: BleakClient | None = None
        try:
            client = await self._open_probe_client()

            for cmd in commands:
                for attempt in range(2):
                    try:
                        if client is None or not client.is_connected:
                            client = await self._open_probe_client()

                        print(f"{_now()}  >> WRITE len={len(cmd):3d}  {cmd.hex()}")
                        await client.write_gatt_char(
                            self.args.write_uuid,
                            cmd,
                            response=not self.args.no_response,
                        )
                        await asyncio.sleep(self.args.delay)
                        break
                    except Exception as exc:  # noqa: BLE001
                        print(
                            f"{_now()}  !! WRITE error: {exc} "
                            f"(attempt {attempt + 1}/2)"
                        )
                        await self._close_probe_client(client)
                        client = None
                        if attempt == 0:
                            # One reconnect/retry is usually enough after a
                            # command that asks the device to restart BLE.
                            await asyncio.sleep(0.5)
                            print(f"{_now()}  .. reconnecting and retrying {cmd.hex()}")
                            continue
                        raise

            if self.args.tail_wait > 0:
                print(f"Waiting {self.args.tail_wait}s for late notifications...")
                await asyncio.sleep(self.args.tail_wait)
        finally:
            await self._close_probe_client(client)

        print(f"Done. Received {len(self.notifications)} notifications.")

    async def run_probe_read(self) -> None:
        """Send commands and read all readable characteristics after each write.

        Some devices keep notifications as ACKs while exposing actual data via
        explicit characteristic reads after a mode/select command.
        """
        commands = [_to_bytes(cmd) for cmd in self.args.cmd]
        print(f"Connecting to {self.args.mac}...")
        async with BleakClient(self.args.mac, timeout=self.args.connect_timeout) as client:
            await client.start_notify(self.args.notify_uuid, self.on_notify)
            print(f"Subscribed to {self.args.notify_uuid}")

            readable: list[str] = []
            for service in client.services:
                for char in service.characteristics:
                    if "read" in char.properties:
                        readable.append(char.uuid)

            print(f"Readable characteristics: {len(readable)}")
            for uuid in readable:
                print(f"  - {uuid}")

            for cmd in commands:
                before = len(self.notifications)
                print(f"{_now()}  >> WRITE len={len(cmd):3d}  {cmd.hex()}")
                await client.write_gatt_char(
                    self.args.write_uuid,
                    cmd,
                    response=not self.args.no_response,
                )

                await asyncio.sleep(self.args.delay)

                # Show notifications produced by this command.
                new_notifies = self.notifications[before:]
                if new_notifies:
                    print(f"{_now()}  << NOTIFY_COUNT {len(new_notifies)}")
                else:
                    print(f"{_now()}  << NOTIFY_COUNT 0")

                # Read all readable characteristics right after the command.
                for uuid in readable:
                    try:
                        value = bytes(await client.read_gatt_char(uuid))
                        print(f"{_now()}  << READ {uuid} len={len(value):3d}  {value.hex()}")
                    except Exception as exc:  # noqa: BLE001
                        print(f"{_now()}  << READ {uuid} error: {exc}")

                # Optional extra dwell to catch delayed notifications.
                if self.args.read_tail_wait > 0:
                    await asyncio.sleep(self.args.read_tail_wait)

            if self.args.tail_wait > 0:
                print(f"Waiting {self.args.tail_wait}s for late notifications...")
                await asyncio.sleep(self.args.tail_wait)

            await client.stop_notify(self.args.notify_uuid)
            print(f"Done. Received {len(self.notifications)} notifications.")

    async def run_oneshot(self) -> None:
        """Connect, send one command, wait shortly for notify, disconnect."""
        cmd = _to_bytes(self.args.cmd[0])
        self.notifications.clear()

        print(f"Connecting to {self.args.mac}...")
        async with BleakClient(self.args.mac, timeout=self.args.connect_timeout) as client:
            await client.start_notify(self.args.notify_uuid, self.on_notify)
            print(f"Subscribed to {self.args.notify_uuid}")

            print(f"{_now()}  >> WRITE len={len(cmd):3d}  {cmd.hex()}")
            await client.write_gatt_char(
                self.args.write_uuid,
                cmd,
                response=not self.args.no_response,
            )

            await asyncio.sleep(self.args.oneshot_wait)
            await client.stop_notify(self.args.notify_uuid)

        print(
            "Oneshot done. "
            f"Command={cmd.hex()} notifications={len(self.notifications)}"
        )

    async def run_sweep(self) -> None:
        start = self.args.start
        end = self.args.end
        if start < 0 or end > 255 or start > end:
            raise ValueError("Sweep range must satisfy 0 <= start <= end <= 255")

        out_path = Path(self.args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)

        print(f"Connecting to {self.args.mac}...")
        async with BleakClient(self.args.mac, timeout=self.args.connect_timeout) as client:
            await client.start_notify(self.args.notify_uuid, self.on_notify)
            print(f"Subscribed to {self.args.notify_uuid}")

            with out_path.open("w", encoding="utf-8") as f:
                f.write("time,cmd_hex,notify_count,notify_hex_list\n")
                for value in range(start, end + 1):
                    before = len(self.notifications)
                    cmd = bytes([value])
                    print(f"{_now()}  >> WRITE  {cmd.hex()}")
                    await client.write_gatt_char(
                        self.args.write_uuid,
                        cmd,
                        response=not self.args.no_response,
                    )
                    await asyncio.sleep(self.args.delay)
                    chunk = self.notifications[before:]
                    chunk_hex = "|".join(n.hex() for n in chunk)
                    f.write(f"{_now()},{cmd.hex()},{len(chunk)},{chunk_hex}\n")

                if self.args.tail_wait > 0:
                    await asyncio.sleep(self.args.tail_wait)

            await client.stop_notify(self.args.notify_uuid)
            print(f"Sweep complete: {start:#04x}..{end:#04x}")
            print(f"Output saved: {out_path}")


async def async_main(args: argparse.Namespace) -> None:
    if args.scan:
        print("Scanning for 8 seconds...")
        devices = await BleakScanner.discover(timeout=8.0)
        for d in devices:
            print(f"{d.address:20s}  {d.name}")

    session = ProbeSession(args)

    if args.mode == "info":
        await session.run_info()
    elif args.mode == "monitor":
        await session.run_monitor()
    elif args.mode == "oneshot":
        if len(args.cmd) != 1:
            raise ValueError("oneshot mode requires exactly one --cmd")
        await session.run_oneshot()
    elif args.mode == "probe":
        if not args.cmd:
            raise ValueError("probe mode requires at least one --cmd")
        await session.run_probe()
    elif args.mode == "probe-read":
        if not args.cmd:
            raise ValueError("probe-read mode requires at least one --cmd")
        await session.run_probe_read()
    elif args.mode == "sweep":
        await session.run_sweep()
    elif args.mode == "blufi-secure-scan":
        await session.run_blufi_secure_scan()
    else:
        raise ValueError(f"Unsupported mode: {args.mode}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Bleak probing utility for APstorage ELT-12")
    p.add_argument(
        "mode",
        choices=[
            "info",
            "monitor",
            "oneshot",
            "probe",
            "probe-read",
            "sweep",
            "blufi-secure-scan",
        ],
    )
    p.add_argument("--mac", default=DEFAULT_MAC, help="Target BLE MAC")
    p.add_argument("--notify-uuid", default=DEFAULT_NOTIFY_UUID, help="Notify characteristic UUID")
    p.add_argument("--write-uuid", default=DEFAULT_WRITE_UUID, help="Write characteristic UUID")
    p.add_argument("--connect-timeout", type=float, default=15.0)
    p.add_argument("--delay", type=float, default=0.5, help="Delay after each write")
    p.add_argument("--tail-wait", type=float, default=3.0, help="Wait after writes for late notifications")
    p.add_argument("--scan", action="store_true", help="Perform a scanner pass before connecting")
    p.add_argument("--no-response", action="store_true", help="Use write without response")

    p.add_argument("--cmd", action="append", default=[], help="Hex command bytes (probe mode), e.g. 01 or aa55ff")
    p.add_argument("--monitor-seconds", type=float, default=120.0)
    p.add_argument("--oneshot-wait", type=float, default=0.7, help="Seconds to wait for a notification in oneshot mode")
    p.add_argument("--read-tail-wait", type=float, default=0.0, help="Extra wait after reads per command in probe-read mode")

    p.add_argument("--start", type=int, default=0, help="Sweep start byte (0-255)")
    p.add_argument("--end", type=int, default=255, help="Sweep end byte (0-255)")
    p.add_argument("--out", default="probe_logs/sweep.csv", help="Sweep output CSV")
    p.add_argument("--blufi-mtu", type=int, default=20, help="BLUFI packet MTU used for app-like fragmentation")
    p.add_argument("--secure-timeout", type=float, default=8.0, help="Timeout waiting for secure negotiation responses")
    p.add_argument("--secure-retries", type=int, default=3, help="How many full reconnect+handshake retries to attempt")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
