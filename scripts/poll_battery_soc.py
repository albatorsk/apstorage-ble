#!/usr/bin/env python3
"""Continuously poll APstorage battery SoC over BLE.

Designed for Ubuntu/Arch terminal use. Maintains a *persistent* BLE connection
so the expensive Blufi DH key-exchange only happens once per connect, not once
per poll.  This mirrors how the EMA app works and avoids the Blufi server-side
state-machine confusion that causes repeated failures when connecting and
disconnecting on every poll.

Examples:
  python3 scripts/poll_battery_soc.py --mac 48:CA:43:EB:C3:F9
  python3 scripts/poll_battery_soc.py --mac 48:CA:43:EB:C3:F9 --interval 5 --count 20
  python3 scripts/poll_battery_soc.py --mac 48:CA:43:EB:C3:F9 --interval 5 --count 20 --debug
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import sys
from datetime import datetime
from pathlib import Path
from types import ModuleType

from bleak import BleakClient, BleakScanner
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_soc_module() -> ModuleType:
    """Load the soc_client module directly from the integration source file."""
    module_path = REPO_ROOT / "custom_components" / "apstorage_ble" / "soc_client.py"
    spec = importlib.util.spec_from_file_location("apstorage_soc_client", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module from: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_soc_module = _load_soc_module()

APstorageSocClient = _soc_module.APstorageSocClient
_safe_disconnect = _soc_module._safe_disconnect
_derive_storage_id_candidates = _soc_module._derive_storage_id_candidates
_extract_metrics = _soc_module._extract_metrics
DEVICE_NAME_CHAR = _soc_module.DEVICE_NAME_CHAR


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Continuously poll APstorage battery SoC via persistent BLE connection"
    )
    parser.add_argument("--mac", required=True, help="BLE MAC address")
    parser.add_argument(
        "--device-name-hint",
        default=None,
        help="Optional name hint, e.g. PCS_B050XXXXXXXX",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Seconds between polls (default: 5)",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=0,
        help="Number of polls (0 = run forever)",
    )
    parser.add_argument(
        "--scan-timeout",
        type=float,
        default=15.0,
        help="BLE scan timeout in seconds",
    )
    parser.add_argument(
        "--query-timeout",
        type=float,
        default=25.0,
        help="Max seconds to wait for a single SoC query",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    return parser


async def _find_device(mac: str, scan_timeout: float):
    device = await BleakScanner.find_device_by_address(mac, timeout=scan_timeout)
    if device is None:
        raise RuntimeError(f"Device not found during BLE scan: {mac}")
    return device


async def _connect_and_setup(
    device,
    client_obj: APstorageSocClient,
    device_name_hint: str | None,
) -> tuple[BleakClient, str]:
    """Connect, discover services, run Blufi DH exchange, return (ble_client, storage_id)."""
    ble_client = await establish_connection(
        BleakClientWithServiceCache,
        device,
        device.address,
        max_attempts=3,
        use_services_cache=False,
    )

    try:
        name_raw = await ble_client.read_gatt_char(DEVICE_NAME_CHAR)
        device_name = bytes(name_raw).decode("utf-8", errors="ignore").strip("\x00\r\n ")
    except Exception:  # noqa: BLE001
        device_name = ""

    storage_ids = _derive_storage_id_candidates(
        None, device_name, device_name_hint, device.name
    )
    if not storage_ids:
        await _safe_disconnect(ble_client)
        raise RuntimeError(
            f"Could not derive storage ID from device name {device_name!r} "
            f"(hint: {device_name_hint!r}, ble_name: {device.name!r})"
        )
    storage_id = storage_ids[0]

    # Blufi DH key-exchange — establishes session_key + starts notify subscription.
    client_obj._reset_blufi_session_state()
    await client_obj._establish_blufi_session(ble_client)

    return ble_client, storage_id


async def async_main(args: argparse.Namespace) -> int:
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    if args.interval < 1.0:
        raise RuntimeError("--interval must be >= 1.0 second")

    print(
        f"Starting SoC polling for {args.mac} "
        f"(interval={args.interval}s, scan_timeout={args.scan_timeout}s, "
        f"query_timeout={args.query_timeout}s, persistent_connection=true)"
    )
    print("Scanning for BLE device...")
    device = await _find_device(args.mac, args.scan_timeout)
    print(f"Found device: {device.address} ({device.name or 'unknown'})")

    client_obj = APstorageSocClient()
    ble_client: BleakClient | None = None
    storage_id: str | None = None

    poll_num = 0
    try:
        while True:
            poll_num += 1
            ts = datetime.now().isoformat(timespec="seconds")

            # (Re)connect if we don't have an active connection.
            if ble_client is None or not ble_client.is_connected:
                try:
                    print(f"{ts} poll={poll_num} state=connecting")
                    ble_client, storage_id = await _connect_and_setup(
                        device, client_obj, args.device_name_hint
                    )
                    print(f"{ts} poll={poll_num} state=connected storage_id={storage_id}")
                except Exception as exc:  # noqa: BLE001
                    print(f"{ts} poll={poll_num} error=connect_failed: {exc}")
                    if ble_client is not None:
                        await _safe_disconnect(ble_client)
                        ble_client = None
                    # Rescan so the next attempt uses a fresh advertisement.
                    try:
                        print(f"{ts} poll={poll_num} state=rescanning")
                        device = await _find_device(args.mac, args.scan_timeout)
                    except Exception as scan_exc:  # noqa: BLE001
                        print(f"{ts} poll={poll_num} rescan_error={scan_exc}")
                    if args.count > 0 and poll_num >= args.count:
                        return 0
                    await asyncio.sleep(args.interval)
                    continue

            # Query using the existing session — no DH exchange needed.
            try:
                async with asyncio.timeout(args.query_timeout):
                    client_obj.parsed_frames = []
                    client_obj._frame_cursor = 0
                    parsed = await client_obj._send_soc_request(ble_client, storage_id)

                if parsed is not None:
                    metrics = _extract_metrics(parsed)
                    if metrics.battery_soc is not None:
                        print(f"{ts} poll={poll_num} soc={int(metrics.battery_soc)}%")
                    else:
                        print(f"{ts} poll={poll_num} soc=unknown (response had no soc field)")
                else:
                    print(f"{ts} poll={poll_num} soc=no_response")

            except TimeoutError:
                print(f"{ts} poll={poll_num} error=query timeout after {args.query_timeout}s")
                # Drop the connection; reconnect next cycle.
                await _safe_disconnect(ble_client)
                ble_client = None

            except Exception as exc:  # noqa: BLE001
                print(f"{ts} poll={poll_num} error={type(exc).__name__}: {exc}")
                # If the BLE link dropped, force a reconnect next cycle.
                if ble_client is not None and not ble_client.is_connected:
                    ble_client = None

            if args.count > 0 and poll_num >= args.count:
                return 0

            await asyncio.sleep(args.interval)

    finally:
        if ble_client is not None and ble_client.is_connected:
            await _safe_disconnect(ble_client)

    return 0


def main() -> None:
    args = build_parser().parse_args()
    code = asyncio.run(async_main(args))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
