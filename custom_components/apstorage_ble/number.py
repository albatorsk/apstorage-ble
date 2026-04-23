"""Number platform for writable APstorage settings."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, get_model
from .coordinator import APstorageCoordinator


MODE_CODE_TO_OPTION: dict[str, str] = {
    "0": "Peak Valley",
    "1": "Self-Consumption",
    "2": "Manual Control",
    "3": "Advanced",
    "4": "Backup power supply",
    "5": "Peak-Shaving",
    "6": "Intelligent",
}

LEGACY_STATE_TO_CODE: dict[str, str] = {
    "Self-consumption": "1",
    "Advanced": "3",
    "Intelligent": "6",
}


@dataclass(frozen=True, kw_only=True)
class APstorageNumberDescription(NumberEntityDescription):
    """Description for APstorage number entities."""


PEAK_POWER_NUMBER = APstorageNumberDescription(
    key="peak_power",
    name="Peak Power",
    native_min_value=100,
    native_max_value=50000,
    native_step=1,
    native_unit_of_measurement=UnitOfPower.WATT,
    icon="mdi:flash-outline",
)


def _normalize_mode_code(value: Any) -> str | None:
    """Normalize mode value to compact integer code string."""
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    if text.isdigit():
        return str(int(text))

    try:
        number = float(text)
    except (TypeError, ValueError):
        return text

    if number.is_integer():
        return str(int(number))

    return text


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up APstorage number entities."""
    coordinator: APstorageCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([APstoragePeakPowerNumber(coordinator, entry, PEAK_POWER_NUMBER)])


class APstoragePeakPowerNumber(
    CoordinatorEntity[APstorageCoordinator],
    NumberEntity,
):
    """Writable peak-power setpoint number (setsysmode.peakPower)."""

    entity_description: APstorageNumberDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: APstorageCoordinator,
        entry: ConfigEntry,
        description: APstorageNumberDescription,
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

    def _current_mode_code(self) -> str | None:
        """Return current mode code from decoded fields."""
        data = self.coordinator.data
        if data is None:
            return None

        if data.system_mode is not None:
            return _normalize_mode_code(data.system_mode)

        if data.system_state is not None:
            state = str(data.system_state)
            if state in MODE_CODE_TO_OPTION:
                return state
            return LEGACY_STATE_TO_CODE.get(state)

        return None

    @property
    def available(self) -> bool:
        """Only available when connected and mode supports peak power."""
        if not self.coordinator.available:
            return False
        return self._current_mode_code() == "5"

    @property
    def native_value(self) -> float | None:
        """Return current peak-power setpoint in watts."""
        data = self.coordinator.data
        if data is not None and data.peak_power is not None:
            return float(data.peak_power)

        write = self.coordinator.last_peak_power_write
        if write is not None:
            requested = write.get("requested_peak_power")
            if requested is not None:
                return float(requested)

        return None

    async def async_set_native_value(self, value: float) -> None:
        """Set peak-power setpoint."""
        await self.coordinator.async_set_peak_power(int(round(value)))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose write diagnostics and mode context."""
        attrs: dict[str, Any] = {}

        mode_code = self._current_mode_code()
        if mode_code is not None:
            attrs["mode_code"] = mode_code
            attrs["mode_name"] = MODE_CODE_TO_OPTION.get(mode_code)

        write = self.coordinator.last_peak_power_write
        if write is not None:
            attrs["last_write_ok"] = write.get("ok")
            attrs["last_write_code"] = write.get("code")
            attrs["last_write_message"] = write.get("message")
            attrs["last_write_requested_peak_power"] = write.get("requested_peak_power")
            attrs["last_write_at"] = write.get("at")

        return attrs or None
