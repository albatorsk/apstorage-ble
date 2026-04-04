#!/usr/bin/env python3
"""
Probe APstorage ELT-12 and print all available attributes and their values.
"""
import asyncio
import sys
from pathlib import Path

# Ensure the parent directory is in sys.path for local imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.apstorage_ble import ble_client

def print_attributes(obj, prefix=""):
    for attr in dir(obj):
        if attr.startswith("_"):
            continue
        try:
            value = getattr(obj, attr)
            print(f"{prefix}{attr}: {value}")
        except Exception as e:
            print(f"{prefix}{attr}: <error: {e}>")

async def main():
    # Replace with your device MAC address
    mac = "AA:BB:CC:DD:EE:FF"
    # You may need to adjust this to match your BLE client usage
    client = ble_client.BleClient(mac)
    await client.connect()
    try:
        metrics = await client.async_query_metrics(mac)
        print("\n--- All attributes and values ---")
        print_attributes(metrics)
    finally:
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
