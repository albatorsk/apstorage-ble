"""The APstorage BLE integration."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
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

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SELECT]

SERVICE_SET_SYSTEM_MODE = "set_system_mode"
ATTR_MODE = "mode"
ATTR_ENTRY_ID = "entry_id"
ATTR_ADDRESS = "address"

_MODE_LABEL_TO_CODE: dict[str, int] = {
    "peak-valley": 0,
    "self-consumption": 1,
    "manual control": 2,
    "mixed": 3,
    "backup battery": 4,
    "peak-shaving": 5,
    "intelligent": 6,
}

SERVICE_SET_SYSTEM_MODE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MODE): vol.Any(vol.Coerce(int), cv.string),
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(ATTR_ADDRESS): cv.string,
    }
)


def _parse_mode(value: Any) -> int:
    """Parse a mode value from int/code or human label."""
    if isinstance(value, int):
        mode = value
    else:
        text = str(value).strip()
        if text.isdigit():
            mode = int(text)
        else:
            label_key = text.lower()
            if label_key not in _MODE_LABEL_TO_CODE:
                raise HomeAssistantError(
                    f"Invalid mode {value!r}. Use 0-6 or a known label."
                )
            mode = _MODE_LABEL_TO_CODE[label_key]

    if mode < 0 or mode > 6:
        raise HomeAssistantError("mode must be in range 0..6")
    return mode


def _resolve_target_coordinator(
    hass: HomeAssistant,
    *,
    entry_id: str | None,
    address: str | None,
) -> APstorageCoordinator:
    """Resolve a single target coordinator for a service call."""
    coordinators: dict[str, APstorageCoordinator] = hass.data.get(DOMAIN, {})
    if not coordinators:
        raise HomeAssistantError("No APstorage BLE config entries are loaded")

    if entry_id is not None:
        coordinator = coordinators.get(entry_id)
        if coordinator is None:
            raise HomeAssistantError(f"Unknown entry_id: {entry_id}")
        return coordinator

    if address is not None:
        wanted = address.upper()
        for coordinator in coordinators.values():
            if coordinator._address.upper() == wanted:  # pylint: disable=protected-access
                return coordinator
        raise HomeAssistantError(f"No APstorage BLE entry found for address: {address}")

    if len(coordinators) == 1:
        return next(iter(coordinators.values()))

    raise HomeAssistantError(
        "Multiple APstorage BLE entries loaded; provide entry_id or address"
    )


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

    if not hass.services.has_service(DOMAIN, SERVICE_SET_SYSTEM_MODE):

        async def _async_handle_set_system_mode(call: ServiceCall) -> None:
            mode = _parse_mode(call.data[ATTR_MODE])
            target = _resolve_target_coordinator(
                hass,
                entry_id=call.data.get(ATTR_ENTRY_ID),
                address=call.data.get(ATTR_ADDRESS),
            )
            await target.async_set_system_mode(mode)

        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_SYSTEM_MODE,
            _async_handle_set_system_mode,
            schema=SERVICE_SET_SYSTEM_MODE_SCHEMA,
        )

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
