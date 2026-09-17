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
from .protocol import mode_name, resolve_mode_code, supports_peak_power


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

MODBUS_ADDRESS_NUMBER = APstorageNumberDescription(
    key="modbus_address",
    name="Modbus Address",
    native_min_value=1,
    native_max_value=247,
    native_step=1,
    icon="mdi:identifier",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up APstorage number entities."""
    coordinator: APstorageCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            APstoragePeakPowerNumber(coordinator, entry, PEAK_POWER_NUMBER),
            APstorageModbusAddressNumber(coordinator, entry, MODBUS_ADDRESS_NUMBER),
        ]
    )


def _modbus_write_inputs(coordinator: APstorageCoordinator) -> tuple[bool, str, str, int]:
    """Return the current Modbus state used to build full set/thirdParty writes."""
    data = coordinator.data
    enabled = bool(data.modbus_enabled) if data and data.modbus_enabled is not None else False
    communication = (
        str(data.modbus_communication).lower()
        if data and data.modbus_communication
        else "rs485"
    )
    if communication not in {"rs485", "tcp"}:
        communication = "rs485"
    baud = str(data.modbus_baud) if data and data.modbus_baud else "9600"
    try:
        address = int(data.modbus_address) if data and data.modbus_address is not None else 1
    except (TypeError, ValueError):
        address = 1

    write = coordinator.last_modbus_settings_write
    if write is not None:
        enabled = bool(write.get("requested_enabled", enabled))
        requested_comm = str(write.get("requested_communication", communication)).lower()
        communication = requested_comm if requested_comm in {"rs485", "tcp"} else communication
        baud = str(write.get("requested_baud", baud))
        try:
            address = int(write.get("requested_address", address))
        except (TypeError, ValueError):
            pass

    return enabled, communication, baud, max(1, min(247, address))


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
        return resolve_mode_code(data.system_mode, data.system_state)

    @property
    def available(self) -> bool:
        """Only available when connected and mode supports peak power."""
        if not self.coordinator.runtime_available:
            return False
        return supports_peak_power(self._current_mode_code())

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
            attrs["mode_name"] = mode_name(mode_code)

        write = self.coordinator.last_peak_power_write
        if write is not None:
            attrs["last_write_ok"] = write.get("ok")
            attrs["last_write_code"] = write.get("code")
            attrs["last_write_message"] = write.get("message")
            attrs["last_write_requested_peak_power"] = write.get("requested_peak_power")
            attrs["last_write_at"] = write.get("at")

        return attrs or None


class APstorageModbusAddressNumber(
    CoordinatorEntity[APstorageCoordinator],
    NumberEntity,
):
    """Writable Modbus RS485 address number (set/thirdParty.RS485.addr)."""

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

    @property
    def available(self) -> bool:
        """Return availability from Bluetooth coordinator reachability."""
        return self.coordinator.runtime_available

    @property
    def native_value(self) -> float | None:
        """Return current Modbus RS485 address."""
        data = self.coordinator.data
        if data is not None and data.modbus_address is not None:
            return float(data.modbus_address)

        write = self.coordinator.last_modbus_settings_write
        if write is not None:
            requested = write.get("requested_address")
            if requested is not None:
                return float(requested)

        return None

    async def async_set_native_value(self, value: float) -> None:
        """Set Modbus RS485 address."""
        enabled, communication, baud, _current_address = _modbus_write_inputs(self.coordinator)
        await self.coordinator.async_set_modbus_settings(
            enabled=enabled,
            communication=communication,
            baud=baud,
            address=int(round(value)),
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose write diagnostics for automation/debugging."""
        write = self.coordinator.last_modbus_settings_write
        if write is None:
            return None

        return {
            "last_write_ok": write.get("ok"),
            "last_write_code": write.get("code"),
            "last_write_message": write.get("message"),
            "last_write_requested_enabled": write.get("requested_enabled"),
            "last_write_requested_communication": write.get("requested_communication"),
            "last_write_requested_baud": write.get("requested_baud"),
            "last_write_requested_address": write.get("requested_address"),
            "last_write_at": write.get("at"),
        }
