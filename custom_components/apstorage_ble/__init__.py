"""The APstorage BLE integration."""
from __future__ import annotations

import logging

from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import APstorageCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up APstorage BLE from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    name: str = entry.title

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
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start listening for Bluetooth advertisements *after* the platform has had
    # a chance to subscribe, so no updates are missed.
    entry.async_on_unload(coordinator.async_start())

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
