"""DataUpdateCoordinator for the APstorage BLE integration.

Uses HA's ActiveBluetoothDataUpdateCoordinator so that:
  - Bluetooth advertisements from the PCS are tracked to know when the device
    is reachable (including via the ESPHome Bluetooth proxy).
  - A GATT poll is triggered at most once per POLL_INTERVAL_SECONDS.
  - The device is marked as unavailable automatically when advertisements stop.
"""
from __future__ import annotations

import logging

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import CoreState, HomeAssistant, callback

from .ble_client import APstorageBLEClient
from .const import POLL_INTERVAL_SECONDS
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
        self._name = name
        self._ble_client = APstorageBLEClient(name)
        self._soc_client = APstorageSocClient()
        self._last_poll: float | None = None
        # Most-recent successfully parsed data; also exposed as coordinator.data
        self.data: PCSData | None = None

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
        service_info: BluetoothServiceInfoBleak = self._last_service_info
        # Prefer the connectable device from the service_info if available;
        # otherwise obtain the best connectable device HA knows about.
        if service_info.connectable:
            ble_device = service_info.device
        else:
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass,
                service_info.device.address,
                connectable=True,
            )

        if ble_device is None:
            _LOGGER.warning(
                "[%s] No connectable BLE device found — skipping poll", self._name
            )
            return

        # Keep a mutable data object so we can still expose SoC even if the
        # generic status parser has no match yet.
        result = await self._ble_client.async_fetch_data(ble_device)
        self.data = result if result is not None else PCSData()

        if result is None:
            _LOGGER.debug("[%s] Poll returned no generic data frame", self._name)

        # Query SoC via custom Blufi protocol independently from generic poll.
        soc = await self._soc_client.async_query_soc(
            ble_device,
            device_name_hint=service_info.name,
        )
        if soc is not None:
            self.data.battery_soc = float(soc)
            _LOGGER.debug("[%s] Battery SoC: %d%%", self._name, soc)
        else:
            _LOGGER.debug("[%s] SoC query returned no value", self._name)

        # Push the update to all subscribed entities.
        self.async_update_listeners()

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
