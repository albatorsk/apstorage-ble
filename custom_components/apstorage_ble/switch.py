"""Switch platform for writable APstorage settings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, get_model
from .coordinator import APstorageCoordinator
from .protocol import MODE_CODE_TO_OPTION, mode_name, resolve_mode_code, supports_peak_valley_switches


@dataclass(frozen=True, kw_only=True)
class APstorageSwitchDescription(SwitchEntityDescription):
    """Description for APstorage switch entities."""


SELLING_FIRST_SWITCH = APstorageSwitchDescription(
    key="selling_first",
    name="Selling First",
    icon="mdi:transmission-tower-export",
)

VALLEY_CHARGE_SWITCH = APstorageSwitchDescription(
    key="valley_charge",
    name="Valley Charge",
    icon="mdi:battery-charging-medium",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up APstorage switch entities."""
    coordinator: APstorageCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            APstorageSellingFirstSwitch(coordinator, entry, SELLING_FIRST_SWITCH),
            APstorageValleyChargeSwitch(coordinator, entry, VALLEY_CHARGE_SWITCH),
        ]
    )


class APstorageBaseSwitch(
    CoordinatorEntity[APstorageCoordinator],
    SwitchEntity,
):
    """Base class for APstorage writable switch entities."""

    entity_description: APstorageSwitchDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: APstorageCoordinator,
        entry: ConfigEntry,
        description: APstorageSwitchDescription,
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
            model=get_model(address),
        )

    @property
    def available(self) -> bool:
        """Return availability from Bluetooth coordinator reachability."""
        return self.coordinator.runtime_available

    def _current_mode_code(self) -> str | None:
        """Return current mode code from decoded fields."""
        data = self.coordinator.data
        if data is None:
            return None
        return resolve_mode_code(data.system_mode, data.system_state)


class APstorageSellingFirstSwitch(APstorageBaseSwitch):
    """Writable selling-first switch (setsysmode.sellingFirst)."""

    @property
    def available(self) -> bool:
        """Only available when Peak-Valley mode is active."""
        return super().available and supports_peak_valley_switches(self._current_mode_code())

    @property
    def is_on(self) -> bool | None:
        """Return current selling-first state."""
        data = self.coordinator.data
        if data is not None and data.selling_first is not None:
            return bool(data.selling_first)

        write = self.coordinator.last_selling_first_write
        if write is not None:
            requested = write.get("requested_selling_first")
            if requested is not None:
                return bool(requested)

        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable selling-first behavior."""
        await self.coordinator.async_set_selling_first(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable selling-first behavior."""
        await self.coordinator.async_set_selling_first(False)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose write diagnostics and mode context."""
        attrs: dict[str, Any] = {}

        mode_code = self._current_mode_code()
        if mode_code is not None:
            attrs["mode_code"] = mode_code
            attrs["mode_name"] = mode_name(mode_code)

        write = self.coordinator.last_selling_first_write
        if write is not None:
            attrs["last_write_ok"] = write.get("ok")
            attrs["last_write_code"] = write.get("code")
            attrs["last_write_message"] = write.get("message")
            attrs["last_write_requested_selling_first"] = write.get("requested_selling_first")
            attrs["last_write_at"] = write.get("at")

        return attrs or None


class APstorageValleyChargeSwitch(APstorageBaseSwitch):
    """Writable valley-charge switch (setsysmode.valleycharge)."""

    @property
    def available(self) -> bool:
        """Only available when Peak-Valley mode is active."""
        return super().available and supports_peak_valley_switches(self._current_mode_code())

    @property
    def is_on(self) -> bool | None:
        """Return current valley-charge state."""
        data = self.coordinator.data
        if data is not None and data.valley_charge is not None:
            return bool(data.valley_charge)

        write = self.coordinator.last_valley_charge_write
        if write is not None:
            requested = write.get("requested_valley_charge")
            if requested is not None:
                return bool(requested)

        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable valley-charge behavior."""
        await self.coordinator.async_set_valley_charge(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable valley-charge behavior."""
        await self.coordinator.async_set_valley_charge(False)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose write diagnostics and mode context."""
        attrs: dict[str, Any] = {}

        mode_code = self._current_mode_code()
        if mode_code is not None:
            attrs["mode_code"] = mode_code
            attrs["mode_name"] = mode_name(mode_code)

        write = self.coordinator.last_valley_charge_write
        if write is not None:
            attrs["last_write_ok"] = write.get("ok")
            attrs["last_write_code"] = write.get("code")
            attrs["last_write_message"] = write.get("message")
            attrs["last_write_requested_valley_charge"] = write.get("requested_valley_charge")
            attrs["last_write_at"] = write.get("at")

        return attrs or None
