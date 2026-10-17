"""Firmware update entity: shows whether the PCS reports newer firmware.

The versions come from pcsVersion. The latest version is what the PCS found the
last time it checked the APsystems cloud (the integration asks it to check once
a day with set/checkPcsVersion). Installing is left to the EMA app: pcsUpdate is
never sent, so the entity has no Install button.
"""
from __future__ import annotations

from homeassistant.components.update import UpdateDeviceClass, UpdateEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BLE_NAME, DOMAIN, MANUFACTURER, get_model
from .coordinator import APstorageCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the firmware update entity."""
    coordinator: APstorageCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([APstorageFirmwareUpdate(coordinator, entry)])


class APstorageFirmwareUpdate(CoordinatorEntity[APstorageCoordinator], UpdateEntity):
    """PCS firmware: installed vs. latest available, as reported by the PCS."""

    _attr_has_entity_name = True
    _attr_name = "Firmware"
    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_title = "PCS firmware"
    _attr_release_summary = "Install firmware updates from the EMA app."

    def __init__(self, coordinator: APstorageCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        address: str = entry.data[CONF_ADDRESS]
        self._attr_unique_id = f"{address}-firmware"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(dr.CONNECTION_BLUETOOTH, address)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=get_model(entry.data.get(CONF_BLE_NAME)),
        )

    @property
    def available(self) -> bool:
        return self.coordinator.runtime_available

    @property
    def installed_version(self) -> str | None:
        data = self.coordinator.data
        return data.firmware_installed if data else None

    @property
    def latest_version(self) -> str | None:
        """The PCS reports no latest version when it hasn't found one; treat that as up to date."""
        data = self.coordinator.data
        if data is None or data.firmware_installed is None:
            return None
        return data.firmware_latest or data.firmware_installed
