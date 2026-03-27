#!/usr/bin/env python3
"""Query APstorage battery SoC over BLE using the integration client.

This is a local helper script for troubleshooting. It reuses the same SoC
protocol implementation as the Home Assistant integration.

Usage:
  /home/per/vscode/apstorage-ble/.venv/bin/python scripts/get_battery_soc.py \
    --mac AA:BB:CC:DD:EE:FF
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import sys
from pathlib import Path

from bleak import BleakScanner

# Ensure repository root is importable when run from scripts/.
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read Battery SoC from APstorage over BLE")
    parser.add_argument("--mac", required=True, help="BLE MAC address")
    parser.add_argument(
        "--storage-id",
        default=None,
        help="Compatibility option from older script variants.",
    )
    parser.add_argument(
        "--device-name-hint",
        default=None,
        help="Optional name hint, e.g. PCS_B050XXXXXXXX",
    )
    parser.add_argument("--scan-timeout", type=float, default=15.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=1.0)
    parser.add_argument(
        "--packet-delay",
        type=float,
        default=0.01,
        help="Compatibility option; accepted for old command lines.",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser


async def query_once(mac: str, name_hint: str | None, scan_timeout: float) -> int | None:
    device = await BleakScanner.find_device_by_address(mac, timeout=scan_timeout)
    if device is None:
        raise RuntimeError(f"Device not found during BLE scan: {mac}")

    client = APstorageSocClient()
    return await client.async_query_soc(device, device_name_hint=name_hint)


async def async_main(args: argparse.Namespace) -> int:
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    # Backward compatibility: if only storage-id is provided, pass it through
    # as device_name_hint so soc_client can derive candidate IDs from it.
    if args.device_name_hint:
        name_hint = args.device_name_hint
    else:
        name_hint = args.storage_id

    for attempt in range(1, args.retries + 1):
        try:
            soc = await query_once(args.mac, name_hint, args.scan_timeout)
            if soc is None:
                print("SoC query returned no value")
                if attempt < args.retries:
                    await asyncio.sleep(args.retry_delay)
                    continue
                return 2

            print(f"Battery SoC: {soc}%")
            return 0
        except Exception as exc:  # noqa: BLE001
            print(f"Attempt {attempt}/{args.retries} failed: {exc}")
            if attempt >= args.retries:
                return 1
            await asyncio.sleep(args.retry_delay)

    return 1


def main() -> None:
    args = build_parser().parse_args()
    code = asyncio.run(async_main(args))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
