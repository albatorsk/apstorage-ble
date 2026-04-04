#!/usr/bin/env python3
"""
Probe APstorage ELT-12 via BLE, subscribe to notifications, and print all available DE* attributes and their values.
This script does NOT require Home Assistant and uses only Bleak.
"""
import asyncio
from bleak import BleakClient, BleakScanner
import sys
import re

def parse_de_fields(payload: bytes):
    # Heuristic: look for DE* ASCII keys in the payload and print their values
    # This is a placeholder for a real parser. Adjust as needed for your device's protocol.
    text = payload.decode(errors="ignore")
    matches = re.findall(r"DE\d+[:=]([0-9.]+)", text)
    if matches:
        for i, val in enumerate(matches, 1):
            print(f"DE{i}: {val}")
    else:
        print(f"Raw payload: {payload.hex()}")

async def probe_elt12(mac: str):
    print(f"Connecting to {mac}...")
    async with BleakClient(mac) as client:
        print(f"Connected: {client.is_connected}")
        print("Discovering services and characteristics...")
        notify_chars = []
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
                        print(f"         value: {val.hex()} | {list(val)})")
                    except Exception as exc:
                        print(f"         read error: {exc}")
                if "notify" in char.properties:
                    notify_chars.append(char.uuid)
        if notify_chars:
            print("\nSubscribing to notifications...")
            def notification_handler(sender, data):
                print(f"\nNotification from {sender}: {data.hex()}")
                parse_de_fields(data)
            for uuid in notify_chars:
                await client.start_notify(uuid, notification_handler)
            print("Listening for notifications (10 seconds)...")
            await asyncio.sleep(10)
            for uuid in notify_chars:
                await client.stop_notify(uuid)
        else:
            print("No notify characteristics found.")

async def main():
    if len(sys.argv) < 2:
        print("Usage: python3 probe_ble_elt12_de.py <MAC_ADDRESS>")
        print("Or:    python3 probe_ble_elt12_de.py scan")
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
