"""DataUpdateCoordinator for the APstorage BLE integration.

Uses HA's ActiveBluetoothDataUpdateCoordinator so that:
  - Bluetooth advertisements from the PCS are tracked to know when the device
    is reachable (including via the ESPHome Bluetooth proxy).
  - A GATT poll is triggered at most once per POLL_INTERVAL_SECONDS.
  - The device is marked as unavailable automatically when advertisements stop.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import CoreState, HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, POLL_INTERVAL_SECONDS
from .models import PCSData
from .soc_client import APstorageSocClient

_LOGGER = logging.getLogger(__name__)


class APstorageCoordinator(ActiveBluetoothDataUpdateCoordinator[PCSData | None]):
    """Coordinator that polls the APstorage ELT-12 via BLE on advertisement."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        address: str,
        name: str,
    ) -> None:
        """Initialise the coordinator.

        Args:
            hass:    The HA instance.
            logger:  Logger for this coordinator.
            address: BLE MAC address of the PCS (upper-case, colon-separated).
            name:    Human-readable name of the device.
        """
        super().__init__(
            hass=hass,
            logger=logger,
            address=address,
            needs_poll_method=self._needs_poll,
            poll_method=self._async_poll,
            mode=bluetooth.BluetoothScanningMode.ACTIVE,
            connectable=True,
        )
        self._address = address
        self._name = name
        self._soc_client = APstorageSocClient()
        self._poll_lock = asyncio.Lock()
        self._daily_charged_kwh = 0.0
        self._daily_discharged_kwh = 0.0
        self._daily_date: str | None = None
        self._baseline_total_charged: float | None = None
        self._baseline_total_discharged: float | None = None
        self._last_total_charged: float | None = None
        self._last_total_discharged: float | None = None
        self._swap_energy_totals = False
        store_key = f"{DOMAIN}_{address.replace(':', '').lower()}_energy_daily"
        self._energy_store: Store[dict[str, Any]] = Store(hass, 1, store_key)
        self._store_loaded = False
        self._last_energy_ts: float | None = None
        self._last_battery_soc: float | None = None
        self._last_poll: float | None = None
        # Most-recent successfully parsed data; also exposed as coordinator.data
        self.data: PCSData | None = None

    async def async_initialize(self) -> None:
        """Load persisted daily energy state."""
        if self._store_loaded:
            return

        stored = await self._energy_store.async_load()
        if isinstance(stored, dict):
            self._daily_date = str(stored.get("date") or "") or None
            self._daily_charged_kwh = float(stored.get("charged", 0.0) or 0.0)
            self._daily_discharged_kwh = float(stored.get("discharged", 0.0) or 0.0)
            btc = stored.get("baseline_total_charged")
            btd = stored.get("baseline_total_discharged")
            self._baseline_total_charged = float(btc) if btc is not None else None
            self._baseline_total_discharged = float(btd) if btd is not None else None
            ltc = stored.get("last_total_charged")
            ltd = stored.get("last_total_discharged")
            self._last_total_charged = float(ltc) if ltc is not None else None
            self._last_total_discharged = float(ltd) if ltd is not None else None
            self._swap_energy_totals = bool(stored.get("swap_energy_totals", False))

        self._store_loaded = True
        self._rollover_daily_if_needed(force=True)

    @property
    def daily_energy_last_reset(self) -> str | None:
        """Return the date (local) when daily energy counters were last reset."""
        return self._daily_date

    async def _async_save_daily_state(self) -> None:
        """Persist daily counters so restart does not reset to zero."""
        await self._energy_store.async_save(
            {
                "last_total_charged": self._last_total_charged,
                "last_total_discharged": self._last_total_discharged,
                "swap_energy_totals": self._swap_energy_totals,
                "date": self._daily_date,
                "charged": self._daily_charged_kwh,
                "discharged": self._daily_discharged_kwh,
                "baseline_total_charged": self._baseline_total_charged,
                "baseline_total_discharged": self._baseline_total_discharged,
            }
        )

    def _rollover_daily_if_needed(self, *, force: bool = False) -> bool:
        """Reset daily counters when local date changes."""
        today = dt_util.now().date().isoformat()
        if self._daily_date == today:
            return False

        # Startup initialization path: if there is no prior date, set today's
        # marker without resetting already-restored same-day values.
        if self._daily_date is None and force:
            self._daily_date = today
            return True

        if self._daily_date != today:
            _LOGGER.debug("[%s] Daily energy rollover: %s -> %s", self._name, self._daily_date, today)

        self._daily_date = today
        self._daily_charged_kwh = 0.0
        self._daily_discharged_kwh = 0.0
        self._baseline_total_charged = None
        self._baseline_total_discharged = None
        self._last_total_charged = None
        self._last_total_discharged = None
        self._swap_energy_totals = False
        self._last_energy_ts = None
        return True

    def _apply_direct_daily_totals(self, metrics) -> tuple[bool, bool, bool]:
        """Use device-reported cumulative totals to calculate today's values.

        Returns a tuple of:
            (used_any_direct, used_direct_charged, used_direct_discharged)
        """
        charged_raw = metrics.battery_charged_energy
        discharged_raw = metrics.battery_discharged_energy

        charged_total = float(charged_raw) if charged_raw is not None else None
        discharged_total = float(discharged_raw) if discharged_raw is not None else None

        used_direct_charged = charged_total is not None
        used_direct_discharged = discharged_total is not None

        if not used_direct_charged and not used_direct_discharged:
            return False, False, False

        if self._swap_energy_totals and used_direct_charged and used_direct_discharged:
            charged_total, discharged_total = discharged_total, charged_total

        if (
            used_direct_charged
            and used_direct_discharged
            and self._last_total_charged is not None
            and self._last_total_discharged is not None
            and charged_total is not None
            and discharged_total is not None
        ):
            delta_charged = charged_total - self._last_total_charged
            delta_discharged = discharged_total - self._last_total_discharged
            direction_sign = self._resolve_battery_direction_sign(metrics)

            # Some firmware variants appear to report these two counters swapped.
            if (
                not self._swap_energy_totals
                and direction_sign is not None
                and (
                    (direction_sign < 0 and delta_charged > 0.001 and delta_discharged <= 0.0)
                    or (direction_sign > 0 and delta_discharged > 0.001 and delta_charged <= 0.0)
                )
            ):
                _LOGGER.warning(
                    "[%s] Detected swapped battery energy totals from device; applying automatic correction",
                    self._name,
                )
                self._swap_energy_totals = True
                charged_total, discharged_total = discharged_total, charged_total

        if used_direct_charged and charged_total is not None:
            self._last_total_charged = charged_total
            if self._baseline_total_charged is None or charged_total < self._baseline_total_charged:
                self._baseline_total_charged = charged_total
            self._daily_charged_kwh = max(0.0, charged_total - self._baseline_total_charged)

        if used_direct_discharged and discharged_total is not None:
            self._last_total_discharged = discharged_total
            if self._baseline_total_discharged is None or discharged_total < self._baseline_total_discharged:
                self._baseline_total_discharged = discharged_total
            self._daily_discharged_kwh = max(0.0, discharged_total - self._baseline_total_discharged)

        return True, used_direct_charged, used_direct_discharged

    def _integrate_daily_from_power(
        self,
        metrics,
        *,
        integrate_charged: bool = True,
        integrate_discharged: bool = True,
    ) -> bool:
        """Fallback daily counters by integrating battery power over time."""
        now_ts = time.monotonic()
        changed = False

        if self._last_energy_ts is not None and metrics.battery_power is not None:
            dt_hours = (now_ts - self._last_energy_ts) / 3600.0
            if 0 < dt_hours < (POLL_INTERVAL_SECONDS * 4 / 3600.0):
                delta_kwh = abs(float(metrics.battery_power)) * dt_hours / 1000.0
                direction_sign = self._resolve_battery_direction_sign(metrics)
                if direction_sign is None:
                    _LOGGER.debug(
                        "[%s] Skipping daily integration this cycle: unknown battery flow direction",
                        self._name,
                    )
                elif direction_sign >= 0:
                    if not integrate_charged:
                        self._last_energy_ts = now_ts
                        return changed
                    self._daily_charged_kwh += delta_kwh
                    changed = delta_kwh > 0
                else:
                    if not integrate_discharged:
                        self._last_energy_ts = now_ts
                        return changed
                    self._daily_discharged_kwh += delta_kwh
                    changed = delta_kwh > 0

        self._last_energy_ts = now_ts
        return changed

    def _resolve_battery_direction_sign(self, metrics) -> float | None:
        """Resolve battery flow direction sign.

        Returns:
            +1 for charging, -1 for discharging, None when unknown.
        """
        current_sign: float | None = None
        if metrics.battery_current is not None and abs(float(metrics.battery_current)) >= 0.05:
            # APstorage convention: positive current indicates discharging.
            current_sign = -1.0 if float(metrics.battery_current) >= 0 else 1.0

        power_sign: float | None = None
        if metrics.battery_power is not None and abs(float(metrics.battery_power)) >= 5.0:
            # APstorage convention: positive power indicates discharging.
            power_sign = -1.0 if float(metrics.battery_power) >= 0 else 1.0

        state_sign: float | None = None
        if getattr(metrics, "battery_flow_state", None) is not None:
            flow_text = str(metrics.battery_flow_state).lower()
            if flow_text.startswith("discharg"):
                state_sign = -1.0
            elif flow_text.startswith("charg"):
                state_sign = 1.0

        if metrics.system_state is not None:
            state_text = str(metrics.system_state).lower()
            if any(token in state_text for token in ("discharge", "discharging", "battery discharge", "battery_discharge")):
                state_sign = -1.0
            elif any(token in state_text for token in ("charge", "charging", "battery charge", "battery_charge")):
                state_sign = 1.0

        soc_sign: float | None = None
        if metrics.battery_soc is not None:
            current_soc = float(metrics.battery_soc)
            if self._last_battery_soc is not None:
                delta_soc = current_soc - self._last_battery_soc
                if abs(delta_soc) >= 0.02:
                    soc_sign = 1.0 if delta_soc > 0 else -1.0
            self._last_battery_soc = current_soc

        # SoC trend best reflects actual energy movement over time.
        if soc_sign is not None:
            flow_sign = current_sign if current_sign is not None else power_sign
            if flow_sign is not None and flow_sign != soc_sign:
                _LOGGER.debug(
                    "[%s] Battery flow sign mismatch (flow=%s, soc=%s); using SoC trend",
                    self._name,
                    flow_sign,
                    soc_sign,
                )
            return soc_sign

        # Next prefer explicit system state when it indicates charge/discharge.
        if state_sign is not None:
            return state_sign

        # If current and power disagree and we have no tie-breaker,
        # prefer power sign so daily counters continue to advance.
        if current_sign is not None and power_sign is not None and current_sign != power_sign:
            _LOGGER.debug(
                "[%s] Battery current/power sign conflict (current=%s, power=%s); using power sign",
                self._name,
                current_sign,
                power_sign,
            )
            return power_sign

        if current_sign is not None:
            return current_sign
        if power_sign is not None:
            return power_sign
        return None

    def _resolve_battery_flow_state(self, metrics) -> str | None:
        """Resolve user-facing battery flow state from live telemetry.

        Priority:
          1. Instantaneous battery power/current with APstorage sign convention
          2. Parsed text state fallback from query metrics
        """
        if metrics.battery_power is not None and abs(float(metrics.battery_power)) >= 5.0:
            return "Discharging" if float(metrics.battery_power) >= 0 else "Charging"

        if metrics.battery_current is not None and abs(float(metrics.battery_current)) >= 0.05:
            return "Discharging" if float(metrics.battery_current) >= 0 else "Charging"

        if metrics.battery_power is not None or metrics.battery_current is not None:
            return "Holding"

        if getattr(metrics, "battery_flow_state", None) is not None:
            flow_text = str(metrics.battery_flow_state).strip().lower()
            if flow_text.startswith("discharg"):
                return "Discharging"
            if flow_text.startswith("charg"):
                return "Charging"
            if flow_text.startswith("hold") or flow_text.startswith("stand"):
                return "Holding"

        return None

    # ------------------------------------------------------------------
    # ActiveBluetoothDataUpdateCoordinator callbacks
    # ------------------------------------------------------------------

    @callback
    def _needs_poll(
        self,
        service_info: BluetoothServiceInfoBleak,
        seconds_since_last_poll: float | None,
    ) -> bool:
        """Return True when GATT polling should be triggered.

        Conditions:
          1. HA is fully running (avoid polls during startup).
          2. Enough time has elapsed since the last poll.
          3. We have a connectable BLE device to use (may route through proxy).
        """
        if self.hass.state != CoreState.running:
            return False

        if (
            seconds_since_last_poll is not None
            and seconds_since_last_poll < POLL_INTERVAL_SECONDS
        ):
            return False

        # Confirm a connectable device (local adapter or ESPHome proxy) is
        # available for the PCS's MAC address.
        return bool(
            bluetooth.async_ble_device_from_address(
                self.hass,
                service_info.device.address,
                connectable=True,
            )
        )

    async def _async_poll(self) -> None:
        """Connect to the device via GATT and update coordinator data."""
        async with self._poll_lock:
            if not self._store_loaded:
                await self.async_initialize()

            service_info: BluetoothServiceInfoBleak | None = self._last_service_info

            # Prefer the connectable device from the most recent advertisement;
            # fall back to HA's current best connectable device by configured MAC.
            if service_info is not None and service_info.connectable:
                ble_device = service_info.device
            elif service_info is not None:
                ble_device = bluetooth.async_ble_device_from_address(
                    self.hass,
                    service_info.device.address,
                    connectable=True,
                )
            else:
                ble_device = bluetooth.async_ble_device_from_address(
                    self.hass,
                    self._address,
                    connectable=True,
                )

            if ble_device is None:
                _LOGGER.warning(
                    "[%s] No connectable BLE device found — skipping poll", self._name
                )
                return

            # Start from the previous snapshot so transient query failures
            # do not force all entities to Unknown.
            previous = self.data
            self.data = PCSData(**vars(previous)) if previous is not None else PCSData()
            _LOGGER.debug("[%s] Starting metrics poll for %s", self._name, ble_device.address)

            metrics = await self._soc_client.async_query_metrics(
                ble_device,
                device_name_hint=self._name,
            )
            if metrics is None:
                _LOGGER.info("[%s] SoC query returned no metrics", self._name)
            else:
                store_dirty = self._rollover_daily_if_needed()
                _LOGGER.debug(
                    "[%s] Received metrics: soc=%s, state=%s, flow=%s",
                    self._name,
                    metrics.battery_soc,
                    metrics.system_state,
                    metrics.battery_flow_state,
                )
                if metrics.battery_soc is not None:
                    self.data.battery_soc = float(metrics.battery_soc)
                    _LOGGER.debug("[%s] Battery SoC: %.1f%%", self._name, self.data.battery_soc)
                if metrics.battery_voltage is not None:
                    self.data.battery_voltage = float(metrics.battery_voltage)
                if metrics.battery_current is not None:
                    self.data.battery_current = float(metrics.battery_current)
                if metrics.battery_power is not None:
                    self.data.battery_power = float(metrics.battery_power)
                    _LOGGER.debug("[%s] Battery Power: %.1f W", self._name, self.data.battery_power)
                if metrics.battery_temperature is not None:
                    self.data.battery_temperature = float(metrics.battery_temperature)
                if metrics.system_state is not None:
                    self.data.system_state = metrics.system_state
                    _LOGGER.debug("[%s] System state: %s", self._name, metrics.system_state)
                resolved_flow_state = self._resolve_battery_flow_state(metrics)
                if resolved_flow_state is not None:
                    self.data.battery_flow_state = resolved_flow_state
                if metrics.grid_voltage is not None:
                    self.data.grid_voltage = float(metrics.grid_voltage)
                if metrics.grid_current is not None:
                    self.data.grid_current = float(metrics.grid_current)
                if metrics.grid_power is not None:
                    self.data.grid_power = float(metrics.grid_power)
                if metrics.grid_frequency is not None:
                    self.data.grid_frequency = float(metrics.grid_frequency)
                if metrics.pv_voltage is not None:
                    self.data.pv_voltage = float(metrics.pv_voltage)
                if metrics.pv_current is not None:
                    self.data.pv_current = float(metrics.pv_current)
                if metrics.pv_power is not None:
                    self.data.pv_power = float(metrics.pv_power)
                if metrics.load_voltage is not None:
                    self.data.load_voltage = float(metrics.load_voltage)
                if metrics.load_current is not None:
                    self.data.load_current = float(metrics.load_current)
                if metrics.load_power is not None:
                    self.data.load_power = float(metrics.load_power)
                if metrics.inverter_temperature is not None:
                    self.data.inverter_temperature = float(metrics.inverter_temperature)

                # Daily energy counters: prefer direct cumulative totals when
                # available, otherwise integrate battery power over time.
                used_direct, used_direct_charged, used_direct_discharged = self._apply_direct_daily_totals(metrics)
                if used_direct:
                    store_dirty = True

                if self._integrate_daily_from_power(
                    metrics,
                    integrate_charged=not used_direct_charged,
                    integrate_discharged=not used_direct_discharged,
                ):
                    store_dirty = True

                self.data.battery_charged_energy = self._daily_charged_kwh
                self.data.battery_discharged_energy = self._daily_discharged_kwh

                if store_dirty:
                    await self._async_save_daily_state()

            # Push the update to all subscribed entities.
            self.async_update_listeners()

    async def async_periodic_poll(self) -> None:
        """Run a fallback poll independent of advertisement event timing."""
        await self._async_poll()

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle BLE advertisement events.

        The base class calls this on every advertisement.  We can optionally
        extract data from the advertisement payload here in the future if the
        PCS encodes any useful data in its manufacturer-specific data.
        """
        _LOGGER.debug(
            "[%s] Advertisement received (RSSI %d dBm)",
            self._name,
            service_info.rssi,
        )
        # Pass through to base class which triggers _needs_poll / _async_poll.
        super()._async_handle_bluetooth_event(service_info, change)

    @callback
    def _async_handle_unavailable(
        self, service_info: BluetoothServiceInfoBleak
    ) -> None:
        """Handle the device going out of range / unavailable."""
        _LOGGER.info("[%s] Device is now unavailable", self._name)
        super()._async_handle_unavailable(service_info)
