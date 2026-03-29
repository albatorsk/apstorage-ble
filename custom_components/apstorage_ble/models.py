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
    battery_power: float | None = None        # W  (derived or direct)
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
    system_state: str | None = None            # free-form state string
    battery_flow_state: str | None = None      # charging / discharging / holding
    buzzer: int | None = None                  # 0=Silent, 1=Normal
    co2_reduction: float | None = None            # kg
    total_produced: float | None = None           # kWh (T2)
    total_consumed: float | None = None           # kWh (T3)
    total_consumed_daily: float | None = None     # kWh (DE3)
