"""The APstorage BLE integration."""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_POLL_INTERVAL_SECONDS,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    POLL_INTERVAL_MAX_SECONDS,
    POLL_INTERVAL_MIN_SECONDS,
    POLL_INTERVAL_SECONDS,
)
from .coordinator import APstorageCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up APstorage BLE from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    name: str = entry.title
    poll_interval = int(entry.options.get(CONF_POLL_INTERVAL_SECONDS, POLL_INTERVAL_SECONDS))
    poll_interval = max(POLL_INTERVAL_MIN_SECONDS, min(POLL_INTERVAL_MAX_SECONDS, poll_interval))

    # Verify that HA can see the device (or a proxy for it) before proceeding.
    # This prevents ConfigEntryNotReady loops when the device is temporarily
    # out of range — HA will retry setup automatically once the device
    # re-appears via the Bluetooth integration.
    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise ConfigEntryNotReady(
            f"Cannot find BLE device {address!r}. "
            "Make sure the PCS is powered on and in range of an ESPHome "
            "Bluetooth proxy or a local Bluetooth adapter."
        )

    coordinator = APstorageCoordinator(
        hass=hass,
        logger=_LOGGER,
        address=address,
        name=name,
        poll_interval_seconds=poll_interval,
    )
    await coordinator.async_initialize()

    # Ensure a device is registered even before entities are added so the
    # Battery State of Charge sensor is always attached to a concrete device.
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, address)},
        connections={(dr.CONNECTION_BLUETOOTH, address)},
        manufacturer=MANUFACTURER,
        model=MODEL,
        name=name,
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start listening for Bluetooth advertisements *after* the platform has had
    # a chance to subscribe, so no updates are missed.
    entry.async_on_unload(coordinator.async_start())

    # Fallback periodic poll so sensors continue updating even when
    # advertisement events are sparse (common with some proxies/adapters).
    @callback
    def _periodic_poll(_now) -> None:
        hass.async_create_task(coordinator.async_periodic_poll())

    entry.async_on_unload(
        async_track_time_interval(
            hass,
            _periodic_poll,
            timedelta(seconds=poll_interval),
        )
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
