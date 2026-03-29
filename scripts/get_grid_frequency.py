#!/usr/bin/env python3
"""Query APstorage grid frequency over BLE using the integration client.

This is a local troubleshooting helper script. It reuses the same BLE protocol
implementation as the Home Assistant integration.

Usage:
  /home/per/vscode/apstorage-ble/.venv/bin/python scripts/get_grid_frequency.py \
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

SWEDEN_FREQ_MIN_HZ = 49.0
SWEDEN_FREQ_MAX_HZ = 51.0

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
    parser = argparse.ArgumentParser(description="Read grid frequency from APstorage over BLE")
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
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser


async def query_once(
    mac: str,
    name_hint: str | None,
    scan_timeout: float,
) -> tuple[float | None, object | None]:
    device = await BleakScanner.find_device_by_address(mac, timeout=scan_timeout)
    if device is None:
        raise RuntimeError(f"Device not found during BLE scan: {mac}")

    client = APstorageSocClient()
    metrics = await client.async_query_metrics(device, device_name_hint=name_hint)
    if metrics is None:
        return None, None

    if metrics.grid_frequency is None:
        return None, metrics

    return float(metrics.grid_frequency), metrics


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
            freq, metrics = await query_once(args.mac, name_hint, args.scan_timeout)
            if freq is None:
                # In this device family, some operating modes do not expose
                # grid frequency in the local-data response.
                if metrics is None:
                    print("Grid frequency unavailable (expected near 50 Hz in Sweden)")
                else:
                    grid_power = getattr(metrics, "grid_power", None)
                    system_state = getattr(metrics, "system_state", None)
                    if grid_power is not None:
                        print(
                            "Grid frequency unavailable from device telemetry "
                            f"(grid_power={float(grid_power):.0f} W, system_state={system_state}; "
                            "expected near 50 Hz in Sweden)"
                        )
                    else:
                        print(
                            "Grid frequency unavailable from device telemetry "
                            f"(system_state={system_state}; expected near 50 Hz in Sweden)"
                        )
                if attempt < args.retries:
                    await asyncio.sleep(args.retry_delay)
                    continue
                return 2

            if not (SWEDEN_FREQ_MIN_HZ <= freq <= SWEDEN_FREQ_MAX_HZ):
                print(
                    "Grid frequency value looks implausible for Sweden: "
                    f"{freq:.2f} Hz (expected about 50 Hz)"
                )
                if attempt < args.retries:
                    await asyncio.sleep(args.retry_delay)
                    continue
                return 3

            print(f"Grid frequency: {freq:.2f} Hz")
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
