#!/usr/bin/env python3
"""
Probe APstorage ELT-12 via BLE and print all available GATT services, characteristics, and their values.
This script does NOT require Home Assistant and uses only Bleak.
"""
import asyncio
from bleak import BleakClient, BleakScanner
import sys

async def probe_elt12(mac: str):
    print(f"Connecting to {mac}...")
    async with BleakClient(mac) as client:
        print(f"Connected: {client.is_connected}")
        print("Discovering services and characteristics...")
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

async def main():
    if len(sys.argv) < 2:
        print("Usage: python3 probe_ble_elt12.py <MAC_ADDRESS>")
        print("Or:    python3 probe_ble_elt12.py scan")
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
