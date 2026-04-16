"""Data models for the APstorage ELT-12 PCS."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PCSData:
    """Live data received from the APstorage ELT-12 PCS.

    All numeric values use SI units (V, A, W, Hz, °C, %).
    A value of None means the field has not yet been decoded from the device
    (either not yet received or the parser does not handle it yet).

    Sign conventions (APstorage):
      battery_current  > 0  →  discharging
                       < 0  →  charging
      grid_power       > 0  →  importing from grid
                       < 0  →  exporting to grid
    """

    # --- Battery ---
    battery_soc: float | None = None          # %  (0–100)
    battery_voltage: float | None = None      # V
    battery_current: float | None = None      # A
    battery_power: float | None = None        # W  (discharge/raw P0)
    battery_charging_power: float | None = None  # W  (charging magnitude, P1)
    battery_temperature: float | None = None  # °C
    battery_charged_energy: float | None = None      # kWh (total charged)
    battery_discharged_energy: float | None = None   # kWh (total discharged)
    pv_energy_produced: float | None = None           # kWh (DE2)

    # --- Grid ---
    grid_voltage: float | None = None         # V
    grid_current: float | None = None         # A
    grid_power: float | None = None           # W
    grid_frequency: float | None = None       # Hz

    # --- PV / Solar ---
    pv_voltage: float | None = None           # V
    pv_current: float | None = None           # A
    pv_power: float | None = None             # W

    # --- Load / Output ---
    load_voltage: float | None = None         # V
    load_current: float | None = None         # A
    load_power: float | None = None           # W

    # --- System ---
    inverter_temperature: float | None = None  # °C
    system_mode: str | None = None             # mode code: 0..6
    backup_soc: float | None = None            # % reserve SOC threshold
    selling_first: bool | None = None          # 0/1 flag for selling-first behavior
    valley_charge: bool | None = None          # 0/1 flag for valley-charge behavior
    peak_power: int | None = None              # W peak-shaving setpoint (mode 5)
    system_state: str | None = None            # free-form state string
    battery_flow_state: str | None = None      # charging / discharging / holding
    buzzer: int | None = None                  # 0=Silent, 1=Normal
    co2_reduction: float | None = None            # kg
    total_produced: float | None = None           # kWh (T2)
    total_consumed: float | None = None           # kWh (T3)
    total_consumed_daily: float | None = None     # kWh (DE3)
    pcs_firmware_version: str | None = None       # current PCS firmware version (raw)
    pcs_latest_firmware_version: str | None = None  # latest available PCS firmware version (raw)
    pcs_software_version: str | None = None       # reported PCS software version
    pcs_hardware_version: str | None = None       # reported PCS hardware version
    battery_alarm: str | None = None              # battery/storage alarm summary
    pcs_alarm: str | None = None                  # PCS/ESS alarm summary
    alarm_summary: str | None = None              # combined alarm summary

    def _split_version_parts(self, value: str | None) -> tuple[str | None, str | None, str | None]:
        """Split underscore-delimited version strings into up to three parts."""
        if value is None:
            return (None, None, None)

        parts = [part.strip() for part in str(value).split("_") if part.strip()]
        while len(parts) < 3:
            parts.append(None)
        return (parts[0], parts[1], parts[2])

    @property
    def pcs_firmware_version_1(self) -> str | None:
        return self._split_version_parts(self.pcs_firmware_version)[0]

    @property
    def pcs_firmware_version_2(self) -> str | None:
        return self._split_version_parts(self.pcs_firmware_version)[1]

    @property
    def pcs_firmware_version_3(self) -> str | None:
        return self._split_version_parts(self.pcs_firmware_version)[2]

    @property
    def pcs_latest_firmware_version_1(self) -> str | None:
        return self._split_version_parts(self.pcs_latest_firmware_version)[0]

    @property
    def pcs_latest_firmware_version_2(self) -> str | None:
        return self._split_version_parts(self.pcs_latest_firmware_version)[1]

    @property
    def pcs_latest_firmware_version_3(self) -> str | None:
        return self._split_version_parts(self.pcs_latest_firmware_version)[2]

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
