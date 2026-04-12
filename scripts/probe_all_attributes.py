#!/usr/bin/env python3
"""
Probe APstorage ELT-12 and print all available attributes and their values.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from bleak import BleakScanner

# Ensure the parent directory is in sys.path for local imports
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_soc_client_class() -> type:
    """Load APstorageSocClient without importing HA integration package init."""
    module_path = REPO_ROOT / "custom_components" / "apstorage_ble" / "soc_client.py"
    spec = importlib.util.spec_from_file_location("apstorage_soc_client", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.APstorageSocClient


APstorageSocClient = _load_soc_client_class()

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
    device = await BleakScanner.find_device_by_address(mac, timeout=15.0)
    if device is None:
        raise RuntimeError(f"Device not found during BLE scan: {mac}")

    client = APstorageSocClient()
    metrics = await client.async_query_metrics(device)
    if metrics is None:
        print("No metrics received")
        return

    print("\n--- All attributes and values ---")
    print_attributes(metrics)

if __name__ == "__main__":
    asyncio.run(main())
