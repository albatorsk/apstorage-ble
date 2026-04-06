"""Select platform for writable APstorage settings."""
from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import APstorageCoordinator

_LOGGER = logging.getLogger(__name__)

MODE_CODE_TO_OPTION: dict[str, str] = {
    "0": "Peak-Valley",
    "1": "Self-Consumption",
    "2": "Manual Control",
    "3": "Mixed",
    "4": "Backup Battery",
    "5": "Peak-Shaving",
    "6": "Intelligent",
}

OPTION_TO_MODE_CODE: dict[str, str] = {v: k for k, v in MODE_CODE_TO_OPTION.items()}

# Accept labels that may already be exposed by system-state sensor formatting.
LEGACY_STATE_TO_CODE: dict[str, str] = {
    "Self-consumption": "1",
    "Advanced mode": "3",
    "Intelligent": "6",
}


@dataclass(frozen=True, kw_only=True)
class APstorageSelectDescription(SelectEntityDescription):
    """Description for APstorage select entities."""


SYSTEM_MODE_SELECT = APstorageSelectDescription(
    key="system_mode",
    name="System Mode",
    options=list(OPTION_TO_MODE_CODE.keys()),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up APstorage select entities."""
    coordinator: APstorageCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([APstorageSystemModeSelect(coordinator, entry, SYSTEM_MODE_SELECT)])


class APstorageSystemModeSelect(
    CoordinatorEntity[APstorageCoordinator],
    SelectEntity,
):
    """Writable system mode selector for APstorage storage device."""

    entity_description: APstorageSelectDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: APstorageCoordinator,
        entry: ConfigEntry,
        description: APstorageSelectDescription,
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

    @property
    def current_option(self) -> str | None:
        """Return the currently active system mode option."""
        data = self.coordinator.data
        if data is None:
            return None

        if data.system_mode is not None:
            option = MODE_CODE_TO_OPTION.get(str(data.system_mode))
            if option is not None:
                return option

        if data.system_state is not None:
            state = str(data.system_state)
            option = MODE_CODE_TO_OPTION.get(state)
            if option is not None:
                return option
            code = LEGACY_STATE_TO_CODE.get(state)
            if code is not None:
                return MODE_CODE_TO_OPTION.get(code)

        return None

    async def async_select_option(self, option: str) -> None:
        """Set system mode on the device."""
        mode_code = OPTION_TO_MODE_CODE.get(option)
        if mode_code is None:
            raise ValueError(f"Unknown system mode option: {option}")

        _LOGGER.debug("Setting APstorage system mode to %s (%s)", option, mode_code)
        await self.coordinator.async_set_system_mode(int(mode_code))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose raw mode code for automation/debugging."""
        data = self.coordinator.data
        attrs: dict[str, Any] = {}

        if data is not None:
            raw_mode = data.system_mode if data.system_mode is not None else data.system_state
            if raw_mode is not None:
                attrs["mode_code"] = str(raw_mode)

        write = self.coordinator.last_system_mode_write
        if write is not None:
            attrs["last_write_ok"] = write.get("ok")
            attrs["last_write_code"] = write.get("code")
            attrs["last_write_message"] = write.get("message")
            attrs["last_write_requested_mode"] = write.get("requested_mode")
            attrs["last_write_at"] = write.get("at")

        return attrs or None
