#!/usr/bin/env python3
"""
Probe APstorage ELT-12 via BLE, send a poll request, subscribe to notifications, and decode all available attributes using the open-source parser logic.
This script does NOT require Home Assistant and uses only Bleak.
"""
import asyncio
from bleak import BleakClient, BleakScanner
import sys
import struct

# Field ID mapping based on Home Assistant integration and protocol analysis
FIELD_ID_MAP = {
    1: "bssid",
    2: "ssid",
    # Add more mappings as discovered
    # Example: 0x10: "battery_soc", 0x11: "battery_voltage", ...
}

RESPONSE_TIMEOUT_SECONDS = 30

async def probe_elt12(mac: str):
    print(f"Connecting to {mac}...")
    async with BleakClient(mac) as client:
        print(f"Connected: {client.is_connected}")
        print("Discovering services and characteristics...")
        notify_char = None
        write_char = None
        for service in client.services:
            for char in service.characteristics:
                if "notify" in char.properties:
                    notify_char = char.uuid
                if "write" in char.properties:
                    write_char = char.uuid
        if not write_char or not notify_char:
            print("No writable or notify characteristic found.")
            return
        rx_buf = bytearray()
        rx_event = asyncio.Event()
        def notification_handler(_sender, data):
            rx_buf.extend(data)
            if len(rx_buf) >= 4:
                flags = int(rx_buf[1])
                data_len = int(rx_buf[3])
                has_checksum = (flags & 0x02) != 0
                expected_total = 4 + data_len + (2 if has_checksum else 0)
                if len(rx_buf) >= expected_total:
                    rx_event.set()
        await client.start_notify(notify_char, notification_handler)
        print(f"Sending poll request to {write_char}...")
        await client.write_gatt_char(write_char, bytes([0x14, 0x00, 0x00, 0x00]), response=True)
        try:
            await asyncio.wait_for(rx_event.wait(), timeout=RESPONSE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            print(f"No notification received within {RESPONSE_TIMEOUT_SECONDS} seconds.")
            await client.stop_notify(notify_char)
            return
        await client.stop_notify(notify_char)
        print(f"Received {len(rx_buf)} bytes: {rx_buf.hex()}")
        if len(rx_buf) < 4:
            print("Frame too short.")
            return
        flags = int(rx_buf[1])
        data_len = int(rx_buf[3])
        has_checksum = (flags & 0x02) != 0
        expected_total = 4 + data_len + (2 if has_checksum else 0)
        if len(rx_buf) < expected_total:
            print("Incomplete frame.")
            return
        payload = rx_buf[4:4+data_len]
        print("Decoded payload:")
        idx = 0
        while idx + 2 <= len(payload):
            field_id = payload[idx]
            field_len = payload[idx + 1]
            idx += 2
            if idx + field_len > len(payload):
                break
            value = payload[idx: idx + field_len]
            idx += field_len
            name = FIELD_ID_MAP.get(field_id, f"field_{field_id:02x}")
            as_int = int.from_bytes(value, 'little', signed=False) if field_len <= 4 else value.hex()
            print(f"{name} (0x{field_id:02x}): {value.hex()} (as int: {as_int})")

async def main():
    if len(sys.argv) < 2:
        print("Usage: python3 probe_ble_elt12_all.py <MAC_ADDRESS>")
        print("Or:    python3 probe_ble_elt12_all.py scan")
        sys.exit(1)
    if sys.argv[1] == "scan":
        print("Scanning for BLE devices (8 seconds)...")
        devices = await BleakScanner.discover(timeout=8.0)
        for d in devices:
            print(f"{d.address:20s}  {d.name}")
        return
    mac = sys.argv[1]
    await probe_elt12(mac)

if __name__ == "__main__":
    asyncio.run(main())
