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
        store_key = f"{DOMAIN}_{address.replace(':', '').lower()}_energy_daily"
        self._energy_store: Store[dict[str, Any]] = Store(hass, 1, store_key)
        self._store_loaded = False
        self._last_energy_ts: float | None = None
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
        self._last_energy_ts = None
        return True

    def _apply_direct_daily_totals(self, metrics) -> bool:
        """Use device-reported cumulative totals to calculate today's values."""
        used_direct = False

        if metrics.battery_charged_energy is not None:
            total = float(metrics.battery_charged_energy)
            if self._baseline_total_charged is None or total < self._baseline_total_charged:
                self._baseline_total_charged = total
            self._daily_charged_kwh = max(0.0, total - self._baseline_total_charged)
            used_direct = True

        if metrics.battery_discharged_energy is not None:
            total = float(metrics.battery_discharged_energy)
            if self._baseline_total_discharged is None or total < self._baseline_total_discharged:
                self._baseline_total_discharged = total
            self._daily_discharged_kwh = max(0.0, total - self._baseline_total_discharged)
            used_direct = True

        return used_direct

    def _integrate_daily_from_power(self, metrics) -> bool:
        """Fallback daily counters by integrating battery power over time."""
        now_ts = time.monotonic()
        changed = False

        if self._last_energy_ts is not None and metrics.battery_power is not None:
            dt_hours = (now_ts - self._last_energy_ts) / 3600.0
            if 0 < dt_hours < (POLL_INTERVAL_SECONDS * 4 / 3600.0):
                delta_kwh = abs(float(metrics.battery_power)) * dt_hours / 1000.0
                direction = metrics.battery_current
                if direction is None:
                    direction = metrics.battery_power
                if direction >= 0:
                    self._daily_charged_kwh += delta_kwh
                else:
                    self._daily_discharged_kwh += delta_kwh
                changed = delta_kwh > 0

        self._last_energy_ts = now_ts
        return changed

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
                _LOGGER.debug("[%s] Received metrics: soc=%s, state=%s", self._name, metrics.battery_soc, metrics.system_state)
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
                used_direct = self._apply_direct_daily_totals(metrics)
                if used_direct:
                    store_dirty = True
                else:
                    if self._integrate_daily_from_power(metrics):
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
