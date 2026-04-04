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
import inspect
import json
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

_LOGGER = logging.getLogger(__name__)

# Static AES key and IV used by the protocol
AES_KEY_STR = "E7MiPPrs9v6i3DY3"
AES_IV_STR = "8914934610490056"

# Blufi DH parameters (standard Blufi spec)
BLUFI_DH_P_HEX = (
    "cf5cf5c38419a724957ff5dd323b9c45c3cdd261eb740f69aa94b8bb1a5c9640"
    "9153bd76b24222d03274e4725a5406092e9e82e9135c643cae98132b0d95f7d6"
    "5347c68afc1e677da90e51bbab5f5cf429c291b4ba39c6b2dc5e8c7231e46aa7"
    "728e87664532cdf547be20c9a3fa8342be6e34371a27c06f7dc0edddd2f86373"
)
BLUFI_DH_G = 2

# BLE characteristic UUIDs for custom protocol
WRITE_CHAR = "0000ff07-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR = "0000ff06-0000-1000-8000-00805f9b34fb"
DEVICE_NAME_CHAR = "00002a00-0000-1000-8000-00805f9b34fb"

# The PCS expects Blufi frames fragmented for the default BLE payload size.
# Keep this aligned with the known-good standalone script defaults.
BLUFI_MTU = 20

# Timeouts
CONNECT_TIMEOUT_SECONDS = 90
RESPONSE_TIMEOUT_SECONDS = 30

try:
    from Crypto.Cipher import AES
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


def _make_cmd(frame_type: int, subtype: int) -> int:
    """Encode Blufi frame type and subtype into command byte."""
    return (frame_type & 0x03) | ((subtype & 0x3F) << 2)


def _u16_le(value: int) -> bytes:
    """Encode a 16-bit integer in little-endian."""
    return bytes((value & 0xFF, (value >> 8) & 0xFF))


def _crc16_app(seed: int, data: bytes) -> int:
    """CRC16 used by APstorage app flow (poly 0x1021, inverted init/final)."""
    crc = (~seed) & 0xFFFF
    for b in data:
        crc ^= (b & 0xFF) << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return (~crc) & 0xFFFF


def _aes_cfb_encrypt(key: bytes, seq: int, payload: bytes) -> bytes:
    """Blufi payload encryption with IV seeded by sequence number."""
    iv = bytes([seq & 0xFF]) + (b"\x00" * 15)
    return AES.new(key, AES.MODE_CFB, iv=iv, segment_size=128).encrypt(payload)


def _aes_cfb_decrypt(key: bytes, seq: int, payload: bytes) -> bytes:
    """Blufi payload decryption with IV seeded by sequence number."""
    iv = bytes([seq & 0xFF]) + (b"\x00" * 15)
    return AES.new(key, AES.MODE_CFB, iv=iv, segment_size=128).decrypt(payload)


def _pad_key_16(key: str) -> bytes:
    """Pad or truncate key material to 16 bytes."""
    if len(key) < 16:
        key = key + ("0" * (16 - len(key)))
    return key.encode("utf-8")[:16]


def _normalize_storage_ids(storage_id: str) -> list[str]:
    """Generate common candidate forms for a storage ID."""
    candidates = [storage_id]
    compact = storage_id.replace(":", "")
    if compact and compact not in candidates:
        candidates.append(compact)
    upper = compact.upper()
    if upper and upper not in candidates:
        candidates.append(upper)
    lower = compact.lower()
    if lower and lower not in candidates:
        candidates.append(lower)
    return candidates


def _deep_find_soc(obj: Any) -> str | None:
    """Search nested dict/list payloads for a SoC-like key."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            k = key.lower()
            if k in {"ssoc", "soc", "battery_soc", "batterysoc", "bs"}:
                if value is not None:
                    return str(value)
            nested = _deep_find_soc(value)
            if nested is not None:
                return nested
    elif isinstance(obj, list):
        for value in obj:
            nested = _deep_find_soc(value)
            if nested is not None:
                return nested
    return None


def _deep_find_key(obj: Any, keys: set[str]) -> Any | None:
    """Find first value in nested payload where key matches one of keys."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in keys and value is not None:
                return value
            nested = _deep_find_key(value, keys)
            if nested is not None:
                return nested
    elif isinstance(obj, list):
        for value in obj:
            nested = _deep_find_key(value, keys)
            if nested is not None:
                return nested
    return None


def _deep_find_grid_frequency_key(obj: Any) -> Any | None:
    """Find frequency-like values under grid/AC-related keys.

    Some firmwares use key names not covered by explicit mappings.
    This fallback searches keys containing freq/hz with grid/AC hints.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            kl = key.lower()
            freq_hint = (
                "freq" in kl
                or "hz" in kl
                or re.fullmatch(r"f(?:_?(?:ac|grid|g|\d+))?", kl) is not None
            )
            grid_hint = (
                "grid" in kl
                or "ac" in kl
                or kl.startswith("gf")
                or kl.startswith("f")
            )
            if freq_hint and grid_hint and value is not None:
                return value

            nested = _deep_find_grid_frequency_key(value)
            if nested is not None:
                return nested
    elif isinstance(obj, list):
        for value in obj:
            nested = _deep_find_grid_frequency_key(value)
            if nested is not None:
                return nested
    return None


def _deep_collect_numeric_items(
    obj: Any,
    *,
    path_prefix: str = "",
) -> list[tuple[str, float]]:
    """Collect numeric leaf values from nested dict/list payloads.

    Returns tuples of (path, value) where path is a dotted key path.
    """
    items: list[tuple[str, float]] = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            key_str = str(key)
            path = f"{path_prefix}.{key_str}" if path_prefix else key_str
            items.extend(_deep_collect_numeric_items(value, path_prefix=path))
        return items

    if isinstance(obj, list):
        for idx, value in enumerate(obj):
            path = f"{path_prefix}[{idx}]" if path_prefix else f"[{idx}]"
            items.extend(_deep_collect_numeric_items(value, path_prefix=path))
        return items

    number = _to_float(obj)
    if number is not None:
        items.append((path_prefix, number))

    return items


def _infer_grid_frequency_from_numeric_fields(root: Any) -> float | None:
    """Infer grid frequency from numeric fields when key names are opaque.

    The decompiled app frequently represents frequency in tenths of Hz.
    This heuristic scans numeric leaf values and tests common divisors,
    selecting candidates that land close to 50 Hz (grid-standard prior).
    """
    numeric_items = _deep_collect_numeric_items(root)
    if not numeric_items:
        return None

    best_hz: float | None = None
    best_score = float("inf")
    best_path: str | None = None

    for path, value in numeric_items:
        kl = path.lower()
        # Skip likely non-grid-frequency fields to reduce false positives.
        if any(token in kl for token in ("soc", "temp", "co2", "power", "current", "voltage", "energy")):
            continue

        for div in (1.0, 10.0, 100.0, 1000.0):
            hz = value / div
            if 49.0 <= hz <= 51.0:
                score = abs(hz - 50.0)
                # Prefer keys hinting at frequency/grid/ac when scores tie.
                if any(token in kl for token in ("freq", "hz", "grid", "ac", "gf", "f")):
                    score -= 0.01
                if score < best_score:
                    best_score = score
                    best_hz = hz
                    best_path = path

    if best_hz is not None:
        _LOGGER.debug(
            "Inferred grid frequency %.2f Hz from numeric field '%s'",
            best_hz,
            best_path,
        )

    return best_hz


def _to_float(value: Any) -> float | None:
    """Best-effort conversion to float."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _last_nonzero_from_array(value: Any) -> float | None:
    """Extract the last non-zero numeric value from a list of strings/numbers.

    APstorage getDeviceLastDataLocal responses use array fields (e.g. SV0,
    SP0, SI0) containing historical readings as string values.  The last
    non-zero element represents the most recent valid measurement.
    """
    if not isinstance(value, list):
        return None
    result: float | None = None
    for item in value:
        num = _to_float(item)
        if num is not None and num != 0.0:
            result = num
    return result


def _to_celsius(value: Any) -> float | None:
    """Convert raw temperature-like values to Celsius.

    APstorage payloads for this device family appear to encode temperatures
    in hundredths of a degree (e.g. 328.1 -> 3.28 C). Some firmwares may
    already report plain Celsius.
    """
    temp = _to_float(value)
    if temp is None:
        return None
    if temp > 100:
        return temp / 100.0
    return temp


def _to_grid_frequency(value: Any) -> float | None:
    """Convert raw frequency-like values to Hz when they look valid."""
    freq = _to_float(value)
    if freq is None:
        return None

    # Firmware variants encode frequency in different scales.
    # Try common divisors and choose the first plausible Hz value,
    # preferring values closest to 50 Hz.
    candidates: list[float] = []
    for div in (1.0, 10.0, 100.0, 1000.0):
        hz = freq / div
        if 40.0 <= hz <= 70.0:
            candidates.append(hz)

    if candidates:
        return min(candidates, key=lambda x: abs(x - 50.0))

    # Some payloads report only one decimal already (e.g. 500 -> 50.0).
    if 400.0 <= freq <= 700.0:
        return freq / 10.0

    return None


def _to_grid_voltage(value: Any) -> float | None:
    """Convert and validate grid voltage-like values.

    Grid voltage should be within a plausible AC range; reject values that
    look like counters/energies (e.g. single-digit kWh-like values).
    """
    volts = _to_float(value)
    if volts is None:
        return None
    if 80.0 <= volts <= 300.0:
        return volts
    return None


def _to_battery_voltage(value: Any) -> float | None:
    """Convert and validate battery voltage-like values.

    APstorage low-voltage battery packs are expected roughly in the 20-65 V
    range depending on chemistry/state.
    """
    volts = _to_float(value)
    if volts is None:
        return None
    if 20.0 <= volts <= 65.0:
        return volts
    return None


def _to_battery_current(value: Any) -> float | None:
    """Convert and validate battery current-like values."""
    amps = _to_float(value)
    if amps is None:
        return None
    if -300.0 <= amps <= 300.0:
        return amps
    return None


def _to_grid_current(value: Any) -> float | None:
    """Convert and validate grid current-like values.

    Reject values outside plausible AC current range for this class of device.
    """
    amps = _to_float(value)
    if amps is None:
        return None
    if -200.0 <= amps <= 200.0:
        return amps
    return None


def _map_ess_status_to_battery_flow_state(value: Any) -> str:
    """Map app essStatus to battery flow state label.

    Decompiled app logic in SystemModeActivityByStorage maps:
      essStatus == "0" -> discharge
      essStatus == "1" -> charge
      otherwise         -> standby
    """
    if str(value).strip() == "0":
        return "Discharging"
    if str(value).strip() == "1":
        return "Charging"
    return "Holding"


def _derive_battery_flow_state(
    battery_current: float | None,
    battery_power: float | None,
    battery_charging_power: float | None = None,
) -> str | None:
    """Best-effort battery flow state when essStatus is absent."""
    p0 = float(battery_power) if battery_power is not None else None
    p1 = float(battery_charging_power) if battery_charging_power is not None else None

    if p0 is not None and p1 is not None:
        if p1 >= 5.0:
            return "Charging"
        if p0 >= 5.0:
            return "Discharging"
        return "Holding"

    if p1 is not None:
        return "Charging" if p1 >= 5.0 else "Holding"

    if p0 is not None and abs(p0) >= 5.0:
        return "Discharging" if p0 >= 0 else "Charging"

    if battery_current is not None and abs(float(battery_current)) >= 0.05:
        return "Discharging" if float(battery_current) >= 0 else "Charging"

    if p0 is not None or battery_current is not None:
        return "Holding"
    return None


def _normalize_pv_current(
    current: float | None,
    power: float | None,
    voltage: float | None,
) -> float | None:
    """Normalize PV current when raw value is inconsistent with P=V*I.

    Some firmware payloads appear to report PV current in alternate scales.
    Use power/voltage consistency to select a plausible current.
    """
    if current is None:
        return None
    if power is None or voltage in (None, 0.0):
        return current

    abs_current = abs(current)
    expected = abs(float(power)) / float(voltage)

    # If PV power is essentially zero, suppress clearly bogus high current.
    if abs(float(power)) < 5.0 and abs_current > 2.0:
        for div in (10.0, 100.0, 1000.0):
            candidate = abs_current / div
            if candidate <= 2.0:
                return candidate if current >= 0 else -candidate
        return 0.0

    # Choose the scale that best matches expected current from power/voltage.
    options = [abs_current, abs_current / 10.0, abs_current / 100.0, abs_current / 1000.0]
    best = min(options, key=lambda x: abs(x - expected))

    # Only apply correction when difference is meaningful.
    if abs(best - abs_current) > 0.5 and (expected == 0.0 or abs_current > expected * 3.0):
        return best if current >= 0 else -best

    return current


def _extract_metrics(parsed: Any) -> SocMetrics:
    """Extract all available metrics from parsed local-data response JSON."""
    metrics = SocMetrics()
    if not isinstance(parsed, dict):
        return metrics

    ess_flow_state: str | None = None

    roots: list[Any] = []
    data_root = parsed.get("data")
    if isinstance(data_root, (dict, list)):
        roots.append(data_root)
    roots.append(parsed)

    # Search for SoC
    for root in roots:
        soc_raw = _deep_find_key(
            root,
            {"bs", "soc", "ssoc", "battery_soc", "batterysoc"},
        )
        soc = _to_float(soc_raw)
        if soc is not None:
            metrics.battery_soc = soc
            break

    # Search for battery voltage.
    for root in roots:
        bv_raw = _deep_find_key(
            root,
            {"bv", "uvdc", "battery_voltage", "batteryvoltage", "bat_vol", "batvol"},
        )
        bv = _to_battery_voltage(bv_raw)
        if bv is not None:
            metrics.battery_voltage = bv
            break

    # Search for battery current
    for root in roots:
        bi_raw = _deep_find_key(
            root,
            {"bi", "battery_current", "batterycurrent", "bat_cur", "batcur", "idc"},
        )
        bi = _to_battery_current(bi_raw)
        if bi is not None:
            metrics.battery_current = bi
            break

    # Search for battery power (APstorage field: P0 appears to be battery power)
    for root in roots:
        bp_raw = _deep_find_key(
            root,
            {"bp", "battery_power", "batterypower", "bat_pow", "batpow", "pdc", "p0"},
        )
        bp = _to_float(bp_raw)
        if bp is not None:
            metrics.battery_power = bp
            break

    # Search for battery charging power (P1).
    for root in roots:
        bcp_raw = _deep_find_key(root, {"p1", "battery_charging_power"})
        bcp = _to_float(bcp_raw)
        if bcp is not None:
            metrics.battery_charging_power = bcp
            break

    # Search for battery temperature (APstorage field is typically T2).
    for root in roots:
        bt_raw = _deep_find_key(
            root,
            {"bt", "battery_temperature", "batterytemp", "battery_temp", "bat_temp", "tbat"},
        )
        bt = _to_celsius(bt_raw)
        if bt is not None:
            metrics.battery_temperature = bt
            break

    # Search for charged/discharged energy totals (kWh).
    # App data models use chargeTotal/dischargeTotal and sometimes
    # todayChargeEnergy/todayDischargeEnergy in home views.
    for root in roots:
        ce_raw = _deep_find_key(
            root,
            {
                "charge_total",
                "chargetotal",
                "charged_total",
                "chargedtotal",
                "charge_energy",
                "chargeenergy",
                "todaychargeenergy",
                "de1",
            },
        )
        ce = _to_float(ce_raw)
        if ce is not None:
            metrics.battery_charged_energy = ce
            break

    for root in roots:
        de_raw = _deep_find_key(
            root,
            {
                "discharge_total",
                "dischargetotal",
                "discharged_total",
                "dischargedtotal",
                "discharge_energy",
                "dischargeenergy",
                "todaydischargeenergy",
                "de0",
            },
        )
        de = _to_float(de_raw)
        if de is not None:
            metrics.battery_discharged_energy = de
            break

    # Search for buzzer setting (BUZ attribute).
    for root in roots:
        buz_raw = _deep_find_key(root, {"buz", "buzzer"})
        buz = _to_float(buz_raw)
        if buz is not None and buz in (0.0, 1.0):
            metrics.buzzer = int(buz)
            break

    # Search for system state
    state_keys = {
        "system_state",
        "systemstate",
        "run_state",
        "runstate",
        "work_mode",
        "workmode",
        "device_state",
        "devicestate",
        "state",
        "status",
        "mode",
    }
    for root in roots:
        state_raw = _deep_find_key(root, state_keys)
        if state_raw is not None:
            metrics.system_state = str(state_raw)
            break

    for root in roots:
        ess_status_raw = _deep_find_key(root, {"essstatus", "ess_status"})
        if ess_status_raw is not None:
            ess_flow_state = _map_ess_status_to_battery_flow_state(ess_status_raw)
            break

    if metrics.system_state is None:
        code = parsed.get("code")
        msg = parsed.get("msg") or parsed.get("message")
        if code is not None and msg is not None:
            metrics.system_state = f"{code} {msg}"
        elif code is not None:
            metrics.system_state = f"code {code}"

    # Search for grid power (APstorage field: P1 appears to be grid power)
    for root in roots:
        gp_raw = _deep_find_key(root, {"gp", "grid_power", "gridpow", "p5"})
        gp = _to_float(gp_raw)
        if gp is not None:
            metrics.grid_power = gp
            break

    # SP0 is grid/storage power as an array of historical readings.
    if metrics.grid_power is None:
        for root in roots:
            sp0_raw = _deep_find_key(root, {"sp0"})
            gp = _last_nonzero_from_array(sp0_raw)
            if gp is not None:
                metrics.grid_power = gp
                break

    # Search for grid voltage/current/frequency.
    for root in roots:
        gv_raw = _deep_find_key(
            root,
            {
                "gv",
                "grid_voltage",
                "gridvol",
                "gridv",
                "dv1",
                "uac",
                "vac",
            },
        )
        gv = _to_grid_voltage(gv_raw)
        if gv is not None:
            metrics.grid_voltage = gv
            break

    # SV0 is grid voltage as an array of historical readings.
    if metrics.grid_voltage is None:
        for root in roots:
            sv0_raw = _deep_find_key(root, {"sv0"})
            gv = _last_nonzero_from_array(sv0_raw)
            if gv is not None:
                gv = _to_grid_voltage(gv)
                if gv is not None:
                    metrics.grid_voltage = gv
                    break

    for root in roots:
        gc_raw = _deep_find_key(
            root,
            {
                "gc",
                "grid_current",
                "gridcur",
                "grida",
                "da1",
                "iac",
            },
        )
        gc = _to_grid_current(gc_raw)
        if gc is not None:
            metrics.grid_current = gc
            break

    # Do not use P5 for grid current. If not found, always derive from grid power / 230.


    # If grid current is still None, derive from grid power / 230
    if metrics.grid_current is None and metrics.grid_power is not None:
        metrics.grid_current = metrics.grid_power / 230.0

    for root in roots:
        gf_raw = _deep_find_key(
            root,
            {
                "gf",
                "grid_frequency",
                "grid_frequency_1",
                "gridfreq",
                "gridfreq1",
                "frequency",
                "frequency_1",
                "freq",
                "hz",
                "fgrid",
                "f_ac",
                "fg",
            },
        )
        gf = _to_grid_frequency(gf_raw)
        if gf is not None:
            metrics.grid_frequency = gf
            break

    if metrics.grid_frequency is None:
        for root in roots:
            gf_raw = _deep_find_grid_frequency_key(root)
            gf = _to_grid_frequency(gf_raw)
            if gf is not None:
                metrics.grid_frequency = gf
                break

    if metrics.grid_frequency is None:
        for root in roots:
            inferred = _infer_grid_frequency_from_numeric_fields(root)
            if inferred is not None:
                metrics.grid_frequency = inferred
                break

    if (
        metrics.grid_current is None
        and metrics.grid_power is not None
        and metrics.grid_voltage not in (None, 0.0)
    ):
        metrics.grid_current = metrics.grid_power / metrics.grid_voltage

    # If no explicit grid voltage is exposed in this payload variant,
    # use a nominal single-phase fallback so current can still be derived.
    if metrics.grid_voltage is None and metrics.grid_power is not None:
        metrics.grid_voltage = 230.0

    if (
        metrics.grid_current is None
        and metrics.grid_power is not None
        and metrics.grid_voltage not in (None, 0.0)
    ):
        metrics.grid_current = metrics.grid_power / metrics.grid_voltage

    if metrics.grid_current is None and metrics.grid_power is not None and abs(metrics.grid_power) < 5.0:
        metrics.grid_current = 0.0

    # Search for PV voltage/current.
    for root in roots:
        pv_v_raw = _deep_find_key(
            root,
            {
                "pvv",
                "pv_voltage",
                "pvvoltage",
                "pv_volt",
                "pvvol",
                "vpv",
            },
        )
        pv_v = _to_float(pv_v_raw)
        if pv_v is not None:
            metrics.pv_voltage = pv_v
            break

    for root in roots:
        pv_i_raw = _deep_find_key(
            root,
            {
                "pvi",
                "pv_current",
                "pvcurrent",
                "pvcur",
                "ipv",
            },
        )
        pv_i = _to_float(pv_i_raw)
        if pv_i is not None:
            metrics.pv_current = pv_i
            break

    # Use APstorage field P2 exclusively for PV Power.
    for root in roots:
        pp_raw = _deep_find_key(root, {"p2"})
        pp = _to_float(pp_raw)
        if pp is not None:
            metrics.pv_power = pp
            break

    if (
        metrics.pv_current is None
        and metrics.pv_power is not None
        and metrics.pv_voltage not in (None, 0.0)
    ):
        metrics.pv_current = metrics.pv_power / metrics.pv_voltage

    if (
        metrics.pv_voltage is None
        and metrics.pv_power is not None
        and metrics.pv_current not in (None, 0.0)
    ):
        metrics.pv_voltage = metrics.pv_power / metrics.pv_current

    metrics.pv_current = _normalize_pv_current(
        metrics.pv_current,
        metrics.pv_power,
        metrics.pv_voltage,
    )

    # Search for load voltage/current.
    # On this device family DE4/DE5 are the best current candidates.
    for root in roots:
        lv_raw = _deep_find_key(root, {"lv", "loadvol", "load_voltage", "de4"})
        lv = _to_float(lv_raw)
        if lv is not None:
            metrics.load_voltage = lv
            break

    for root in roots:
        li_raw = _deep_find_key(root, {"li", "loadcur", "load_current", "de5"})
        li = _to_float(li_raw)
        if li is not None:
            metrics.load_current = li
            break

    # Search for load power (APstorage field: P3 appears to be load power)
    for root in roots:
        lp_raw = _deep_find_key(root, {"lp", "loadpow", "load_power", "p3"})
        lp = _to_float(lp_raw)
        if lp is not None:
            metrics.load_power = lp
            break
    # Last-resort derived load current from load power and load voltage.
    if (
        metrics.load_current is None
        and metrics.load_power is not None
        and metrics.load_voltage not in (None, 0.0)
    ):
        metrics.load_current = metrics.load_power / metrics.load_voltage

    derived_flow_state = _derive_battery_flow_state(
        metrics.battery_current,
        metrics.battery_power,
        metrics.battery_charging_power,
    )

    # Match EMA app behavior: prefer essStatus when present.
    # Fallback to derived power/current direction only when essStatus is absent.
    if ess_flow_state is not None:
        if derived_flow_state in {"Charging", "Discharging"} and derived_flow_state != ess_flow_state:
            _LOGGER.debug(
                "Battery flow mismatch (essStatus=%s, derived=%s); using essStatus",
                ess_flow_state,
                derived_flow_state,
            )
        metrics.battery_flow_state = ess_flow_state
    else:
        metrics.battery_flow_state = derived_flow_state

    # Search for daily produced energy (DE2).
    for root in roots:
        de2_raw = _deep_find_key(root, {"de2", "daily_produced_energy", "pv_energy_produced"})
        de2 = _to_float(de2_raw)
        if de2 is not None:
            metrics.pv_energy_produced = de2
            break

    # Search for CO2 reduction.
    for root in roots:
        co2_raw = _deep_find_key(root, {"co2", "co2_reduction"})
        co2 = _to_float(co2_raw)
        if co2 is not None:
            metrics.co2_reduction = co2
            break

    # Search for total produced energy (T2) and total consumed energy (T3/DE3).
    for root in roots:
        t2_raw = _deep_find_key(root, {"t2", "total_produced"})
        t2 = _to_float(t2_raw)
        if t2 is not None:
            metrics.total_produced = t2
            break

    for root in roots:
        t3_raw = _deep_find_key(root, {"t3", "total_consumed"})
        t3 = _to_float(t3_raw)
        if t3 is not None:
            metrics.total_consumed = t3
            break

    for root in roots:
        de3_raw = _deep_find_key(root, {"de3", "total_consumed_daily"})
        de3 = _to_float(de3_raw)
        if de3 is not None:
            metrics.total_consumed_daily = de3
            break

    # Search for inverter temperature (APstorage field is typically T3).
    for root in roots:
        it_raw = _deep_find_key(root, {"it", "inverter_temperature", "invertertemp", "inverter_temp", "tinv"})
        it = _to_celsius(it_raw)
        if it is not None:
            metrics.inverter_temperature = it
            break

    # Log summary of extracted fields
    extracted_fields = []
    if metrics.battery_soc is not None:
        extracted_fields.append(f"soc={metrics.battery_soc}")
    if metrics.battery_voltage is not None:
        extracted_fields.append(f"bv={metrics.battery_voltage:.2f}")
    if metrics.battery_current is not None:
        extracted_fields.append(f"bi={metrics.battery_current:.2f}")
    if metrics.battery_power is not None:
        extracted_fields.append(f"bp={metrics.battery_power:.0f}")
    if metrics.battery_temperature is not None:
        extracted_fields.append(f"bt={metrics.battery_temperature:.1f}")
    if metrics.inverter_temperature is not None:
        extracted_fields.append(f"it={metrics.inverter_temperature:.1f}")
    if metrics.battery_charged_energy is not None:
        extracted_fields.append(f"ce={metrics.battery_charged_energy:.3f}")
    if metrics.battery_discharged_energy is not None:
        extracted_fields.append(f"de={metrics.battery_discharged_energy:.3f}")
    if metrics.pv_energy_produced is not None:
        extracted_fields.append(f"de2={metrics.pv_energy_produced:.3f}")
    if metrics.grid_power is not None:
        extracted_fields.append(f"gp={metrics.grid_power:.0f}")
    if metrics.grid_voltage is not None:
        extracted_fields.append(f"gv={metrics.grid_voltage:.2f}")
    if metrics.grid_current is not None:
        extracted_fields.append(f"gc={metrics.grid_current:.2f}")
    if metrics.grid_frequency is not None:
        extracted_fields.append(f"gf={metrics.grid_frequency:.2f}")
    if metrics.pv_power is not None:
        extracted_fields.append(f"pp={metrics.pv_power:.0f}")
    if metrics.pv_voltage is not None:
        extracted_fields.append(f"pvv={metrics.pv_voltage:.2f}")
    if metrics.pv_current is not None:
        extracted_fields.append(f"pvi={metrics.pv_current:.2f}")
    if metrics.load_voltage is not None:
        extracted_fields.append(f"lv={metrics.load_voltage:.2f}")
    if metrics.load_current is not None:
        extracted_fields.append(f"li={metrics.load_current:.2f}")
    if metrics.load_power is not None:
        extracted_fields.append(f"lp={metrics.load_power:.0f}")
    if metrics.system_state is not None:
        extracted_fields.append(f"state={metrics.system_state}")
    if metrics.battery_flow_state is not None:
        extracted_fields.append(f"flow={metrics.battery_flow_state}")
    if metrics.buzzer is not None:
        extracted_fields.append(f"buz={metrics.buzzer}")
    if metrics.co2_reduction is not None:
        extracted_fields.append(f"co2={metrics.co2_reduction:.2f}")
    if metrics.total_produced is not None:
        extracted_fields.append(f"t2={metrics.total_produced:.3f}")
    if metrics.total_consumed is not None:
        extracted_fields.append(f"t3={metrics.total_consumed:.3f}")
    if metrics.total_consumed_daily is not None:
        extracted_fields.append(f"de3={metrics.total_consumed_daily:.3f}")
    if extracted_fields:
        _LOGGER.debug("Extracted from local-data: %s", ", ".join(extracted_fields))

    return metrics


@dataclass
class BlufiFrame:
    """Parsed Blufi frame."""

    frame_type: int
    subtype: int
    flags: int
    seq: int
    payload: bytes


@dataclass
class SocMetrics:
    """Metrics extracted from a single local data response."""

    # Battery metrics
    battery_soc: float | None = None           # %  (0–100)
    battery_voltage: float | None = None       # V
    battery_current: float | None = None       # A
    battery_power: float | None = None         # W  (P0)
    battery_charging_power: float | None = None  # W  (P1)
    battery_temperature: float | None = None   # °C
    battery_charged_energy: float | None = None      # kWh (total charged)
    battery_discharged_energy: float | None = None   # kWh (total discharged)
    pv_energy_produced: float | None = None           # kWh (DE2)
    # System state
    system_state: str | None = None            # free-form state string
    battery_flow_state: str | None = None      # Charging / Discharging / Holding
    buzzer: int | None = None                  # 0=Silent, 1=Normal
    co2_reduction: float | None = None            # kg
    total_produced: float | None = None           # kWh (T2)
    total_consumed: float | None = None           # kWh (T3)
    total_consumed_daily: float | None = None     # kWh (DE3)
    # Grid metrics
    grid_voltage: float | None = None          # V
    grid_current: float | None = None          # A
    grid_power: float | None = None            # W
    grid_frequency: float | None = None        # Hz
    # PV metrics
    pv_voltage: float | None = None            # V
    pv_current: float | None = None            # A
    pv_power: float | None = None              # W
    # Load metrics
    load_voltage: float | None = None          # V
    load_current: float | None = None          # A
    load_power: float | None = None            # W
    # Inverter
    inverter_temperature: float | None = None  # °C


class BlufiCodec:
    """Blufi MTU-based packet builder/parser."""

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

    @staticmethod
    def _flags(encrypt: bool, checksum: bool, frag: bool) -> int:
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
        *,
        encrypt: bool = False,
        checksum: bool = False,
        aes_key: bytes | None = None,
    ) -> list[bytes]:
        """Fragment payload into MTU-sized Blufi packets (EMA-compatible)."""
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

            # Avoid leaving a tiny trailer frame (1-2 bytes).
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

    def parse_notify(
        self, raw: bytes, aes_key: bytes | None = None
    ) -> BlufiFrame | None:
        """Parse a Blufi notification frame."""
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
                    f"Checksum mismatch: got=0x{got:04x} expected=0x{crc:04x}"
                )

        self.read_seq = (self.read_seq + 1) & 0xFF
        if seq != self.read_seq:
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
            self._rx_hdr = None
            self._rx_expect_total = None
            self._rx_buf.clear()
            return BlufiFrame(
                frame_type=frame_type,
                subtype=subtype,
                flags=first_flags,
                seq=seq,
                payload=data,
            )

        return BlufiFrame(
            frame_type=frame_type,
            subtype=subtype,
            flags=flags,
            seq=seq,
            payload=payload,
        )


def _ema_encrypt_json(request_json: str) -> bytes:
    """Encrypt JSON with AES/CBC/NoPadding + zero-padding."""
    if not HAS_CRYPTO:
        raise RuntimeError("pycryptodome required")

    data = request_json.encode("utf-8")
    if len(data) % 16 != 0:
        data += b"\x00" * (16 - (len(data) % 16))

    key = _pad_key_16(AES_KEY_STR)
    iv = (AES_IV_STR.ljust(16, "\x00")).encode("utf-8")[:16]

    cipher = AES.new(key, AES.MODE_CBC, iv=iv)
    return cipher.encrypt(data)


def _ema_encrypt_json_hexascii(request_json: str) -> bytes:
    """Encrypt JSON and encode result as hex-ASCII (matching EMA app)."""
    ciphertext = _ema_encrypt_json(request_json)
    return ciphertext.hex().encode("ascii")


def _ema_decrypt_payload(payload: bytes) -> str:
    """Decrypt custom payload with AES/CBC/NoPadding."""
    if not HAS_CRYPTO:
        raise RuntimeError("pycryptodome required")

    key = _pad_key_16(AES_KEY_STR)
    iv = (AES_IV_STR.ljust(16, "\x00")).encode("utf-8")[:16]

    cipher = AES.new(key, AES.MODE_CBC, iv=iv)
    decrypted = cipher.decrypt(payload)
    return decrypted.rstrip(b"\x00").decode("utf-8", errors="replace").strip()


def _derive_storage_ids_from_name(device_name: str | None) -> list[str]:
    """Extract storage serial from device name (e.g., 'PCS_B050XXXXXXXX' -> 'B050XXXXXXXX')."""
    if not device_name:
        return []

    name = device_name.strip()
    out: list[str] = []

    # Preferred patterns seen on APstorage devices.
    # Examples: PCS_B05000001878, B05000001878.
    m_full = re.fullmatch(r"(?:PCS[_-]?)?(B\d{6,})", name, flags=re.IGNORECASE)
    if m_full:
        out.append(m_full.group(1))

    # Also accept embedded serials if present in a longer label.
    for serial in re.findall(r"B\d{6,}", name, flags=re.IGNORECASE):
        if serial not in out:
            out.append(serial)

    # Backward-compatible fallback for exact PCS_* names.
    if name.upper().startswith("PCS_"):
        suffix = name.split("_", 1)[1].strip()
        if suffix and suffix not in out:
            out.append(suffix)

    # Keep exact name last only if it already resembles a serial-like token.
    if re.fullmatch(r"[A-Za-z0-9_-]{6,}", name) and name not in out:
        out.append(name)

    normalized: list[str] = []
    for item in out:
        for candidate in _normalize_storage_ids(item):
            if candidate not in normalized:
                normalized.append(candidate)
    return normalized


class APstorageSocClient:
    """Query APstorage battery SoC via Blufi encrypted custom payload."""

    def __init__(self) -> None:
        self.session_key: bytes | None = None
        self._codec = BlufiCodec(mtu=BLUFI_MTU)
        self.parsed_frames: list[BlufiFrame] = []
        self._frame_cursor = 0
        self._preferred_storage_id: str | None = None

    async def _ensure_services_ready(self, client: BleakClient) -> None:
        """Ensure GATT service discovery has completed before I/O.

        Some backend/proxy combinations connect successfully but defer
        discovery until explicitly requested, which causes first read/write
        calls to fail with "Service Discovery has not been performed yet".
        """
        try:
            _ = client.services
            return
        except Exception:  # noqa: BLE001
            pass

        backend = getattr(client, "_backend", None)
        get_services = getattr(backend, "_get_services", None)
        if callable(get_services):
            params = inspect.signature(get_services).parameters
            kwargs: dict[str, object] = {}
            if "dangerous_use_bleak_cache" in params:
                kwargs["dangerous_use_bleak_cache"] = False
            await get_services(**kwargs)

        # Re-check and raise the backend error if services are still unavailable.
        _ = client.services

    async def async_query_metrics(
        self,
        ble_device: BLEDevice,
        *,
        device_name_hint: str | None = None,
    ) -> SocMetrics | None:
        """Connect to device and return extracted metrics or None on failure."""
        if not HAS_CRYPTO:
            _LOGGER.error("pycryptodome required; install with: pip install pycryptodome")
            return None

        client: BleakClient | None = None
        try:
            _LOGGER.debug("Connecting to BLE device %s (hint: %s)", ble_device.address, device_name_hint)
            async with asyncio.timeout(CONNECT_TIMEOUT_SECONDS):
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    ble_device.address,
                    max_attempts=3,
                    use_services_cache=True,
                )

                # Ensure service discovery is available before first GATT call.
                try:
                    await self._ensure_services_ready(client)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug(
                        "Initial service discovery failed for %s (%s); retrying without cache",
                        ble_device.address,
                        err,
                    )
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass

                    client = await establish_connection(
                        BleakClientWithServiceCache,
                        ble_device,
                        ble_device.address,
                        max_attempts=3,
                        use_services_cache=False,
                    )
                    await self._ensure_services_ready(client)

                _LOGGER.debug("Connected to %s, querying metrics", ble_device.address)
                result = await self._query_soc_once(
                    client,
                    ble_device,
                    device_name_hint=device_name_hint,
                )
                _LOGGER.debug("Query complete for %s: metrics=%s", ble_device.address, result is not None)
                return result
        except asyncio.TimeoutError:
            _LOGGER.warning("Connection timeout for %s after %ds", ble_device.address, CONNECT_TIMEOUT_SECONDS)
            return None
        except BleakError as err:
            _LOGGER.warning("BLE error for %s: %s", ble_device.address, err)
            return None
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Unexpected error querying %s: %s", ble_device.address, err, exc_info=True)
            return None
        finally:
            if client and client.is_connected:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass

    async def async_query_soc(
        self,
        ble_device: BLEDevice,
        *,
        device_name_hint: str | None = None,
    ) -> int | None:
        """Compatibility wrapper that returns SoC percent only."""
        metrics = await self.async_query_metrics(
            ble_device,
            device_name_hint=device_name_hint,
        )
        if metrics is None or metrics.battery_soc is None:
            return None
        return int(metrics.battery_soc)

    async def _query_soc_once(
        self,
        client: BleakClient,
        ble_device: BLEDevice,
        *,
        device_name_hint: str | None = None,
    ) -> SocMetrics | None:
        """Execute full local-data query sequence."""
        # 1. Read device name
        try:
            name_raw = await client.read_gatt_char(DEVICE_NAME_CHAR)
            device_name = bytes(name_raw).decode("utf-8", errors="ignore").strip("\x00\r\n ")
        except Exception:  # noqa: BLE001
            device_name = ""

        if not device_name:
            device_name = device_name_hint or ""

        if not device_name:
            device_name = ble_device.name or ""

        storage_ids: list[str] = []

        if self._preferred_storage_id:
            storage_ids.append(self._preferred_storage_id)

        for source in (device_name, device_name_hint, ble_device.name):
            for candidate in _derive_storage_ids_from_name(source):
                if candidate not in storage_ids:
                    storage_ids.append(candidate)

        if not storage_ids:
            _LOGGER.warning("Could not extract storage ID from device name: %s", device_name)
            return None

        # 2. DH negotiation and session key derivation
        try:
            await self._establish_blufi_session(client)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Failed to establish Blufi session: %s", err)
            return None

        # 3. Send local-data request, trying common ID variants.
        for storage_id in storage_ids:
            try:
                parsed = await self._send_soc_request(client, storage_id, system_id="")
                if parsed is None:
                    _LOGGER.debug("_send_soc_request returned None for storage_id=%s", storage_id)
                    continue

                if isinstance(parsed, dict):
                    code = parsed.get("code")
                    msg = str(parsed.get("msg") or parsed.get("message") or "")
                    if code == 202 and "device id mismatch" in msg.lower():
                        _LOGGER.debug(
                            "Ignoring DEVICE ID MISMATCH for storage_id=%s (trying next candidate)",
                            storage_id,
                        )
                        continue

                metrics = _extract_metrics(parsed)
                _LOGGER.debug(
                    "Extracted metrics for storage_id=%s: soc=%s, power=%s, state=%s",
                    storage_id,
                    metrics.battery_soc,
                    metrics.battery_power,
                    metrics.system_state,
                )
                # Return if we extracted any useful metric
                if any(value is not None for value in (
                    metrics.battery_soc,
                    metrics.battery_voltage,
                    metrics.battery_current,
                    metrics.battery_power,
                    metrics.battery_temperature,
                    metrics.battery_charged_energy,
                    metrics.battery_discharged_energy,
                    metrics.grid_voltage,
                    metrics.grid_current,
                    metrics.grid_power,
                    metrics.grid_frequency,
                    metrics.pv_voltage,
                    metrics.pv_current,
                    metrics.pv_power,
                    metrics.load_voltage,
                    metrics.load_current,
                    metrics.load_power,
                    metrics.inverter_temperature,
                )):
                    self._preferred_storage_id = storage_id
                    return metrics
                _LOGGER.warning("Extraction succeeded but metrics are empty for storage_id=%s", storage_id)
            except Exception as err:  # noqa: BLE001
                _LOGGER.exception("SoC query failed for storage_id=%s: %s", storage_id, err)

        _LOGGER.warning("No usable metrics found for storage_id candidates: %s", storage_ids)
        return None

    async def _establish_blufi_session(self, client: BleakClient) -> None:
        """Perform Blufi DH and security setup."""
        self._codec = BlufiCodec(mtu=BLUFI_MTU)

        # Generate DH keypair
        p = int(BLUFI_DH_P_HEX, 16)
        g = BLUFI_DH_G
        priv = secrets.randbelow(p - 3) + 2
        pub = pow(g, priv, p)
        p_bytes = bytes.fromhex(BLUFI_DH_P_HEX)

        g_hex = format(g, "x")
        if len(g_hex) % 2:
            g_hex = "0" + g_hex
        g_bytes = bytes.fromhex(g_hex)

        pub_hex = format(pub, "x").zfill(256)
        pub_bytes = bytes.fromhex(pub_hex)

        # DH handshake
        cmd_nego = _make_cmd(1, 0)
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

        packets_0 = self._codec.build_packets(cmd_nego, nego_payload_0, encrypt=False, checksum=False)
        packets_1 = self._codec.build_packets(cmd_nego, nego_payload_1, encrypt=False, checksum=False)

        self.parsed_frames = []
        self._frame_cursor = 0
        await client.start_notify(NOTIFY_CHAR, self._on_notify)

        try:
            for pkt in packets_0 + packets_1:
                await client.write_gatt_char(WRITE_CHAR, pkt, response=True)
                await asyncio.sleep(0.01)

            # Wait for device public key response
            frame = await self._wait_frame(1, 0, RESPONSE_TIMEOUT_SECONDS)
            dev_pub = int(frame.payload.hex(), 16)
            shared = pow(dev_pub, priv, p)
            shared_hex = format(shared, "x")
            if len(shared_hex) % 2:
                shared_hex = "0" + shared_hex

            self.session_key = hashlib.md5(bytes.fromhex(shared_hex)).digest()

            # Set security mode (checksum + encrypt)
            cmd_sec = _make_cmd(0, 1)
            sec_packets = self._codec.build_packets(cmd_sec, bytes([0x03]), encrypt=False, checksum=True, aes_key=self.session_key)
            for pkt in sec_packets:
                await client.write_gatt_char(WRITE_CHAR, pkt, response=True)
                await asyncio.sleep(0.01)

        finally:
            await client.stop_notify(NOTIFY_CHAR)

    async def _send_soc_request(
        self,
        client: BleakClient,
        storage_id: str,
        system_id: str = "",
    ) -> dict[str, Any] | None:
        """Send encrypted local-data query and return parsed JSON response."""
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
                "systemId": system_id,
                "storageId": storage_id,
            },
        }

        request_json = json.dumps(request, separators=(",", ":"))
        payload = _ema_encrypt_json_hexascii(request_json)

        cmd_custom = _make_cmd(1, 19)
        packets = self._codec.build_packets(cmd_custom, payload, encrypt=True, checksum=True, aes_key=self.session_key)

        self.parsed_frames = []
        self._frame_cursor = 0
        await client.start_notify(NOTIFY_CHAR, self._on_notify_impl)

        try:
            for pkt in packets:
                await client.write_gatt_char(WRITE_CHAR, pkt, response=True)
                await asyncio.sleep(0.01)

            # Wait for custom data response
            frame = await self._wait_frame(1, 19, RESPONSE_TIMEOUT_SECONDS)
            decrypted = _ema_decrypt_payload(frame.payload)
            try:
                parsed = json.loads(decrypted)
            except json.JSONDecodeError:
                _LOGGER.debug("SoC response was not valid JSON for storage_id=%s", storage_id)
                return None

            if isinstance(parsed, dict):
                _LOGGER.debug(
                    "Local-data response keys for storage_id=%s: %s",
                    storage_id,
                    list(parsed.keys()),
                )
                # Log the nested 'data' structure if present
                data_root = parsed.get("data")
                if isinstance(data_root, dict):
                    _LOGGER.debug("Response 'data' field: %s", data_root)
                elif isinstance(data_root, list):
                    _LOGGER.debug("Response 'data' field (list): %s", data_root)
                return parsed
            _LOGGER.debug("Local-data response was non-dict for storage_id=%s", storage_id)
            return None

        finally:
            await client.stop_notify(NOTIFY_CHAR)

    def _on_notify(self, _sender: Any, data: bytearray) -> None:
        """Notification callback used during DH/security setup."""
        self._on_notify_impl(_sender, data)

    def _on_notify_impl(self, _sender: Any, data: bytearray) -> None:
        """Internal notification accumulation."""
        raw = bytes(data)
        try:
            frame = self._codec.parse_notify(raw, aes_key=self.session_key)
            if frame:
                self.parsed_frames.append(frame)
        except Exception:  # noqa: BLE001
            pass

    async def _wait_frame(
        self, frame_type: int, subtype: int, timeout_seconds: float
    ) -> BlufiFrame:
        """Wait for a specific frame type/subtype."""
        start = asyncio.get_event_loop().time()
        while asyncio.get_event_loop().time() - start < timeout_seconds:
            while self._frame_cursor < len(self.parsed_frames):
                frame = self.parsed_frames[self._frame_cursor]
                self._frame_cursor += 1
                if frame.frame_type == frame_type and frame.subtype == subtype:
                    return frame

            await asyncio.sleep(0.05)

        raise TimeoutError(f"No frame type={frame_type} subtype={subtype} received")
