"""Binary sensor platform for the APstorage BLE integration."""
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
    BinarySensorEntityPlatform,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import DOMAIN
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import APstorageCoordinator
from .const import MANUFACTURER

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class APstorageBinarySensorDescription(BinarySensorEntityDescription):
    """Describes a single binary sensor."""


BINARY_SENSOR_DESCRIPTIONS: tuple[APstorageBinarySensorDescription, ...] = (
    APstorageBinarySensorDescription(
        key="firmware_update_available",
        name="Firmware Update Available",
        device_class=BinarySensorDeviceClass.UPDATE,
        value_fn=lambda d: d.pcs_firmware_version != d.pcs_latest_firmware_version if d.pcs_firmware_version and d.pcs_latest_firmware_version else False,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    coordinator: APstorageCoordinator,
) -> list[Any]:
    """Set up the binary sensor platform."""
    return await hass.config_entries.async_forward_entry_setup(config_entry, "binary_sensor")


class APstorageBinarySensorPlatform(BinarySensorEntityPlatform):
    """Binary sensor platform."""

    async def async_setup_entry(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        coordinator: APstorageCoordinator,
    ) -> list[Any]:
        """Set up the binary sensor platform."""
        return [
            APstorageBinarySensor(coordinator, description)
            for description in BINARY_SENSOR_DESCRIPTIONS
        ]


class APstorageBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor entity for APstorage BLE."""

    def __init__(
        self,
        coordinator: APstorageCoordinator,
        description: APstorageBinarySensorDescription,
    ) -> None:
        """Initialize the binary sensor entity."""
        super().__init__(coordinator)
        self.description = description

    @property
    def entity_id(self) -> str:
        """Return the entity id."""
        return f"binary_sensor.{self.device_info.name_slug}_{self.description.key}"

    @property
    def name(self) -> str:
        """Return the name."""
        return self.description.name

    @property
    def device_info(self) -> dict[str, Any]:
        """Return the device info."""
        return {
            "manufacturer": MANUFACTURER,
            "name": self.coordinator.name,
            "identifiers": {("apstorage_ble", self.coordinator.address)},
        }

    @property
    def is_on(self) -> bool:
        """Return whether the sensor is on."""
        if self.coordinator.data is None:
            return False
        return self.description.value_fn(self.coordinator.data)
