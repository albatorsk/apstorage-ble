"""Button platform for APstorage write actions."""
from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import APstorageCoordinator


CLEAR_BUZZER_BUTTON = ButtonEntityDescription(
    key="clear_buzzer",
    name="Clear Buzzer Alarm",
    icon="mdi:bell-off",
)

PCS_REBOOT_BUTTON = ButtonEntityDescription(
    key="pcs_reboot",
    name="Reboot PCS",
    icon="mdi:restart",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up APstorage action buttons."""
    coordinator: APstorageCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            APstorageClearBuzzerButton(coordinator, entry, CLEAR_BUZZER_BUTTON),
            APstoragePcsRebootButton(coordinator, entry, PCS_REBOOT_BUTTON),
        ]
    )


class APstorageClearBuzzerButton(
    CoordinatorEntity[APstorageCoordinator],
    ButtonEntity,
):
    """Button entity that clears the active PCS buzzer alarm."""

    entity_description: ButtonEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: APstorageCoordinator,
        entry: ConfigEntry,
        description: ButtonEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        address: str = entry.data[CONF_ADDRESS]
        self._attr_unique_id = f"{address}-{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(dr.CONNECTION_BLUETOOTH, address)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def available(self) -> bool:
        """Return availability from Bluetooth coordinator reachability."""
        return self.coordinator.available

    async def async_press(self) -> None:
        """Clear the active buzzer alarm."""
        await self.coordinator.async_clear_buzzer()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose write diagnostics for automation/debugging."""
        attrs: dict[str, Any] = {}
        write = self.coordinator.last_clear_buzzer_write
        if write is not None:
            attrs["last_write_ok"] = write.get("ok")
            attrs["last_write_code"] = write.get("code")
            attrs["last_write_message"] = write.get("message")
            attrs["last_write_at"] = write.get("at")
        return attrs or None


class APstoragePcsRebootButton(
    CoordinatorEntity[APstorageCoordinator],
    ButtonEntity,
):
    """Button entity that reboots the PCS."""

    entity_description: ButtonEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: APstorageCoordinator,
        entry: ConfigEntry,
        description: ButtonEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        address: str = entry.data[CONF_ADDRESS]
        self._attr_unique_id = f"{address}-{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(dr.CONNECTION_BLUETOOTH, address)},
            name=entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def available(self) -> bool:
        """Return availability from Bluetooth coordinator reachability."""
        return self.coordinator.available

    async def async_press(self) -> None:
        """Reboot the PCS."""
        await self.coordinator.async_reboot_pcs()

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose write diagnostics for automation/debugging."""
        attrs: dict[str, Any] = {}
        write = self.coordinator.last_pcs_reboot_write
        if write is not None:
            attrs["last_write_ok"] = write.get("ok")
            attrs["last_write_code"] = write.get("code")
            attrs["last_write_message"] = write.get("message")
            attrs["last_write_at"] = write.get("at")
        return attrs or None
