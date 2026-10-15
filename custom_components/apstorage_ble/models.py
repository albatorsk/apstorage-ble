"""Data models for the APstorage PCS."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PCSData:
    """Values read from the APstorage PCS.

    Units: W, kWh, %, kg. None means the PCS did not report the value.
    """

    # --- Battery (getDeviceLastDataLocal) ---
    battery_soc: float | None = None             # % (SSOC)
    battery_power: float | None = None           # W discharging (P0)
    battery_charging_power: float | None = None  # W charging (P1)
    battery_capacity: float | None = None        # kWh (Capacity, or deviceInfo capacity)
    battery_charged_energy: float | None = None      # kWh today (DE1)
    battery_discharged_energy: float | None = None   # kWh today (DE0)
    battery_flow_state: str | None = None        # Charging / Discharging / Holding
    min_soc: float | None = None                 # % (ABD.SM or getsysmode socMin)

    # --- PV / load / grid ---
    pv_power: float | None = None                # W (P2)
    pv_energy_produced: float | None = None      # kWh today (DE2)
    load_power: float | None = None              # W (P3)
    total_consumed_daily: float | None = None    # kWh today (DE3)
    grid_export_power: float | None = None       # W (P4)
    grid_import_power: float | None = None       # W (P5)
    grid_power: float | None = None              # W, > 0 import, < 0 export
    grid_exported_energy: float | None = None    # kWh today (DE4)
    grid_imported_energy: float | None = None    # kWh today (DE5)
    grid_status: str | None = None               # On-grid / Off-grid (ABD.GS)
    grid_frequency: float | None = None          # Hz (ABD.GF)
    generator_power: float | None = None         # W (P6)
    generator_status: str | None = None          # raw ABD.GES

    # --- Lifetime ---
    total_produced: float | None = None          # kWh (T2)
    total_consumed: float | None = None          # kWh (T3)
    co2_reduction: float | None = None           # kg (CO2)

    # --- Operating mode (getsysmode; MODE from live data as fallback) ---
    system_mode: str | None = None               # mode code "0".."6"
    system_state: str | None = None              # legacy mode label; unused by this firmware
    storage_status: str | None = None            # raw status/STA from live data
    backup_soc: float | None = None              # %
    selling_first: bool | None = None
    valley_charge: bool | None = None
    peak_power: int | None = None                # W, peak-shaving limit (mode 5)
    price_area: str | None = None                # e.g. SE4
    sysmode: dict[str, Any] | None = None        # raw getsysmode data

    # --- Buzzer ---
    buzzer: int | None = None                    # 0 = Silent, 1 = Normal (BUZ)
    buzzer_alarm: bool | None = None             # any of BUZAlarm1..3 set

    # --- Device (deviceInfo) ---
    device_id: str | None = None
    device_type: str | None = None               # devType
    pcs_firmware_version: str | None = None      # moduleVer1_moduleVer2_moduleVer3
    comm_firmware_version: str | None = None     # dcmVer
    wifi_ssid: str | None = None
    wifi_rssi: float | None = None               # dBm
    wifi_ip: str | None = None
    cloud_connected: bool | None = None          # connetStatus (sic)
    device_time: str | None = None               # deviceTime from the live-data reply

    _cached_fw_version_source: str | None = field(default=None, init=False, repr=False)
    _cached_fw_version_parts: tuple[str | None, str | None, str | None] = field(
        default=(None, None, None), init=False, repr=False
    )

    @staticmethod
    def _split_version_parts(value: str | None) -> tuple[str | None, str | None, str | None]:
        """Split underscore-delimited version strings into up to three parts."""
        if value is None:
            return (None, None, None)
        parts: list[str | None] = [part.strip() for part in str(value).split("_") if part.strip()]
        while len(parts) < 3:
            parts.append(None)
        return (parts[0], parts[1], parts[2])

    def _firmware_parts(self) -> tuple[str | None, str | None, str | None]:
        """Return cached split parts for pcs_firmware_version."""
        if self.pcs_firmware_version != self._cached_fw_version_source:
            self._cached_fw_version_source = self.pcs_firmware_version
            self._cached_fw_version_parts = self._split_version_parts(self.pcs_firmware_version)
        return self._cached_fw_version_parts

    @property
    def pcs_firmware_version_1(self) -> str | None:
        return self._firmware_parts()[0]

    @property
    def pcs_firmware_version_2(self) -> str | None:
        return self._firmware_parts()[1]

    @property
    def pcs_firmware_version_3(self) -> str | None:
        return self._firmware_parts()[2]

    @property
    def signed_battery_power(self) -> float | None:
        """Return net signed battery power in watts.

        Positive values mean the battery is discharging.
        Negative values mean the battery is charging.
        """
        if self.battery_power is None and self.battery_charging_power is None:
            return None

        discharging = float(self.battery_power or 0.0)
        charging = float(self.battery_charging_power or 0.0)

        if discharging < 0:
            return discharging

        return discharging - charging

    # ------------------------------------------------------------ parsing

    def apply_last_data(self, data: dict[str, Any], device_time: Any = None) -> None:
        """Update from a getDeviceLastDataLocal `data` dict (compact or legacy keys, §9)."""
        def pick(*keys: str) -> Any:
            for key in keys:
                value = data.get(key)
                if value not in (None, "", "null"):
                    return value
            return None

        self.battery_soc = _num(pick("SSOC", "SOC"))
        self.battery_power = _num(pick("P0", "dischargePower", "dischargePwoer"))
        self.battery_charging_power = _num(pick("P1", "chargePower"))
        self.pv_power = _num(pick("P2", "PVPower"))
        self.load_power = _num(pick("P3", "OperatingLoad"))
        self.grid_export_power = _num(pick("P4", "ToGridPower"))
        self.grid_import_power = _num(pick("P5", "FromGridPower"))
        self.generator_power = _num(pick("P6"))
        if self.grid_import_power is not None or self.grid_export_power is not None:
            self.grid_power = (self.grid_import_power or 0.0) - (self.grid_export_power or 0.0)
        else:
            self.grid_power = None

        capacity = _num(pick("Capacity"))
        if capacity is not None:
            self.battery_capacity = capacity

        # Today's energy, DE0..DE5; older firmware lists it in SD[FL].DE.
        daily = {f"DE{i}": _num(pick(f"DE{i}")) for i in range(6)}
        for item in data.get("SD") or []:
            if isinstance(item, dict) and str(item.get("FL", "")).isdigit():
                daily.setdefault(f"DE{item['FL']}", None)
                if daily[f"DE{item['FL']}"] is None:
                    daily[f"DE{item['FL']}"] = _num(item.get("DE"))
        self.battery_discharged_energy = daily.get("DE0")
        self.battery_charged_energy = daily.get("DE1")
        self.pv_energy_produced = daily.get("DE2")
        self.total_consumed_daily = daily.get("DE3")
        self.grid_exported_energy = daily.get("DE4")
        self.grid_imported_energy = daily.get("DE5")

        self.total_produced = _num(pick("T2"))
        self.total_consumed = _num(pick("T3"))
        self.co2_reduction = _num(pick("CO2"))

        mode = pick("MODE", "MOD")
        if mode is not None:
            self.system_mode = str(mode)
        status = pick("status", "STA")
        self.storage_status = None if status is None else str(status)

        buzzer = _num(pick("BUZ"))
        self.buzzer = None if buzzer is None else int(buzzer)
        alarms = [pick(f"BUZAlarm{i}") for i in (1, 2, 3)]
        if any(value is not None for value in alarms):
            self.buzzer_alarm = any(str(value) not in ("0", "None") for value in alarms if value is not None)

        frequency = pick("GF", "gridFrequency", "GridFrequency")
        pcs = data.get("ABD")
        if isinstance(pcs, list):
            pcs = next((item for item in pcs if isinstance(item, dict)), None)
        if isinstance(pcs, dict):
            if pcs.get("GF") not in (None, ""):
                frequency = pcs["GF"]
            grid = pcs.get("GS")
            if grid not in (None, ""):
                self.grid_status = "On-grid" if str(grid) == "0" else "Off-grid"
            generator = pcs.get("GES")
            self.generator_status = None if generator in (None, "") else str(generator)
            min_soc = _num(pcs.get("SM"))
            if min_soc is not None:
                self.min_soc = min_soc

        self.grid_frequency = _hz(frequency)

        if self.battery_charging_power and self.battery_charging_power > 0:
            self.battery_flow_state = "Charging"
        elif self.battery_power and self.battery_power > 0:
            self.battery_flow_state = "Discharging"
        elif self.battery_power is not None or self.battery_charging_power is not None:
            self.battery_flow_state = "Holding"
        else:
            self.battery_flow_state = None

        if device_time not in (None, ""):
            self.device_time = str(device_time)

    def apply_sysmode(self, data: dict[str, Any]) -> None:
        """Update from a getsysmode `data` dict (§8.2)."""
        self.sysmode = dict(data)
        if data.get("mode") not in (None, ""):
            self.system_mode = str(data["mode"])
        self.backup_soc = _num(data.get("backupSOC"))
        if data.get("sellingFirst") not in (None, ""):
            self.selling_first = str(data["sellingFirst"]) == "1"
        if data.get("valleycharge") not in (None, ""):
            self.valley_charge = str(data["valleycharge"]) == "1"
        peak_power = _num(data.get("peakPower"))
        self.peak_power = None if peak_power is None else int(peak_power)
        min_soc = _num(data.get("socMin"))
        if min_soc is not None:
            self.min_soc = min_soc
        area = data.get("area") or data.get("are")
        self.price_area = str(area) if area else None

    def apply_device_info(self, data: dict[str, Any]) -> None:
        """Update from a deviceInfo `data` dict (§6.2)."""
        device_id = data.get("eid") or data.get("EID") or data.get("deviceId")
        if device_id:
            self.device_id = str(device_id)
        device_type = data.get("devType") or data.get("deviceType")
        self.device_type = None if device_type in (None, "") else str(device_type)
        modules = [str(data[key]).strip() for key in ("moduleVer1", "moduleVer2", "moduleVer3") if data.get(key)]
        if modules:
            self.pcs_firmware_version = "_".join(modules)
        if data.get("dcmVer"):
            self.comm_firmware_version = str(data["dcmVer"])
        capacity = _num(data.get("capacity"))
        if capacity is not None and self.battery_capacity is None:
            self.battery_capacity = capacity
        self.wifi_ssid = data.get("ssid") or None
        self.wifi_rssi = _num(data.get("rssi"))
        self.wifi_ip = data.get("dcmIP") or None
        cloud = data.get("connetStatus", data.get("connectStatus"))
        self.cloud_connected = None if cloud in (None, "") else str(cloud) == "1"


def _num(value: Any) -> float | None:
    """Parse a numeric field; the PCS sends numbers as strings."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _hz(value: Any) -> float | None:
    """Grid frequency in Hz; some firmware sends it in 0.1 or 0.01 Hz."""
    number = _num(value)
    if number is None:
        return None
    for divisor in (1, 10, 100):
        if 40.0 <= number / divisor <= 70.0:
            return round(number / divisor, 3)
    return None  # 0 or out of range, e.g. off-grid
