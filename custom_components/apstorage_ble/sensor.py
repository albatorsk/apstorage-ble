    # ...existing code...
"""Sensor platform for the APstorage BLE integration."""
from __future__ import annotations

from datetime import datetime
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_ADDRESS,
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfPower,
    UnitOfMass,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MANUFACTURER, MODEL
from .coordinator import APstorageCoordinator
from .models import PCSData

_LOGGER = logging.getLogger(__name__)


SYSTEM_STATE_LABELS: dict[str, str] = {
    "0": "Peak Valley",
    "1": "Self-consumption",
    "2": "Manual Control",
    "3": "Advanced",
    "4": "Backup power supply",
    "5": "Peak-Shaving",
    "6": "Intelligent",
}

BUZZER_LABELS: dict[int, str] = {
    0: "Silent",
    1: "Normal",
}


def _format_system_state(value: Any) -> Any:
    """Return human-readable label for known system state codes."""
    if value is None:
        return None
    return SYSTEM_STATE_LABELS.get(str(value), value)


@dataclass(frozen=True, kw_only=True)
class APstorageSensorDescription(SensorEntityDescription):
    """Describes a single APstorage sensor."""

    value_fn: Callable[[PCSData], Any]


SENSOR_DESCRIPTIONS: tuple[APstorageSensorDescription, ...] = (
    # --- Battery ---
    APstorageSensorDescription(
        key="battery_soc",
        name="Battery State of Charge",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.battery_soc,
    ),
    APstorageSensorDescription(
        key="battery_discharging_power",
        name="Battery Discharging Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: max(d.signed_battery_power, 0) if d.signed_battery_power is not None else None,
    ),
    APstorageSensorDescription(
        key="battery_charging_power",
        name="Battery Charging Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.battery_charging_power,
    ),
    APstorageSensorDescription(
        key="battery_charged_energy",
        name="Daily Charged Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        # Daily counter that resets at midnight.
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda d: d.battery_charged_energy,
    ),
    APstorageSensorDescription(
        key="battery_discharged_energy",
        name="Daily Discharged Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        # Daily counter that resets at midnight.
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda d: d.battery_discharged_energy,
    ),
    APstorageSensorDescription(
        key="pv_energy_produced",
        name="Daily PV Energy Produced",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        # Daily counter that resets at midnight.
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda d: d.pv_energy_produced,
    ),
    # --- Grid ---
    # Grid Current entity removed (not available or derivable)
    APstorageSensorDescription(
        key="grid_power",
        name="Grid Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.grid_power,
    ),
    APstorageSensorDescription(
        key="grid_frequency",
        name="Grid Frequency",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.grid_frequency if d.grid_frequency is not None else 50,
    ),
    # --- PV / Solar ---
    APstorageSensorDescription(
        key="pv_power",
        name="PV Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.pv_power,
    ),
    # --- Load / Output ---
    APstorageSensorDescription(
        key="load_power",
        name="Load Power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.load_power,
    ),
    # --- System ---
    APstorageSensorDescription(
        key="system_state",
        name="System State",
        device_class=None,
        state_class=None,
        value_fn=lambda d: _format_system_state(d.system_state),
    ),
    APstorageSensorDescription(
        key="battery_flow_state",
        name="Battery Flow State",
        device_class=None,
        state_class=None,
        value_fn=lambda d: d.battery_flow_state,
    ),
    APstorageSensorDescription(
        key="alarm_summary",
        name="Alarm Summary",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.alarm_summary,
    ),
    APstorageSensorDescription(
        key="pcs_alarm",
        name="PCS Alarm",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.pcs_alarm,
    ),
    APstorageSensorDescription(
        key="battery_alarm",
        name="Battery Alarm",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.battery_alarm,
    ),
    APstorageSensorDescription(
        key="co2_reduction",
        name="CO2 Reduction",
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.co2_reduction,
    ),
    APstorageSensorDescription(
        key="total_produced",
        name="Total Produced",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda d: d.total_produced,
    ),
    APstorageSensorDescription(
        key="total_consumed",
        name="Total Consumed",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=lambda d: d.total_consumed,
    ),
    APstorageSensorDescription(
        key="total_consumed_daily",
        name="Daily Total Consumed",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        # Daily counter that resets at midnight.
        state_class=SensorStateClass.TOTAL,
        suggested_display_precision=2,
        value_fn=lambda d: d.total_consumed_daily,
    ),
    APstorageSensorDescription(
        key="pcs_firmware_version_1",
        name="PCS Firmware 1",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.pcs_firmware_version_1,
    ),
    APstorageSensorDescription(
        key="pcs_firmware_version_2",
        name="PCS Firmware 2",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.pcs_firmware_version_2,
    ),
    APstorageSensorDescription(
        key="pcs_firmware_version_3",
        name="PCS Firmware 3",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.pcs_firmware_version_3,
    ),
    APstorageSensorDescription(
        key="pcs_latest_firmware_version_1",
        name="PCS Latest Firmware 1",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.pcs_latest_firmware_version_1,
    ),
    APstorageSensorDescription(
        key="pcs_latest_firmware_version_2",
        name="PCS Latest Firmware 2",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.pcs_latest_firmware_version_2,
    ),
    APstorageSensorDescription(
        key="pcs_latest_firmware_version_3",
        name="PCS Latest Firmware 3",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.pcs_latest_firmware_version_3,
    ),
    APstorageSensorDescription(
        key="pcs_hardware_version",
        name="PCS Hardware Version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.pcs_hardware_version,
    ),
)


async def _async_migrate_sensor_unique_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Migrate renamed sensor unique IDs in the entity registry."""
    address: str = entry.data[CONF_ADDRESS]
    entity_registry = er.async_get(hass)

    migrations = {
        f"{address}-battery_power": f"{address}-battery_discharging_power",
    }

    for old_unique_id, new_unique_id in migrations.items():
        entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, old_unique_id)
        new_entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, new_unique_id)

        if entity_id and new_entity_id:
            entity_registry.async_remove(entity_id)
            _LOGGER.debug("Removed legacy sensor entity with unique ID %s", old_unique_id)
        elif entity_id:
            entity_registry.async_update_entity(entity_id, new_unique_id=new_unique_id)
            _LOGGER.debug("Migrated sensor unique ID from %s to %s", old_unique_id, new_unique_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up APstorage BLE sensors from a config entry."""
    await _async_migrate_sensor_unique_ids(hass, entry)
    coordinator: APstorageCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        APstorageSensor(coordinator, entry, description)
        for description in SENSOR_DESCRIPTIONS
    )


class APstorageSensor(
    CoordinatorEntity[APstorageCoordinator],
    SensorEntity,
):
    """Represents a single sensor on the APstorage ELT-12 PCS."""

    entity_description: APstorageSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: APstorageCoordinator,
        entry: ConfigEntry,
        description: APstorageSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._address: str = entry.data[CONF_ADDRESS]
        self._device_name = entry.title
        # Unique ID: domain + MAC + sensor key so entities survive renames.
        self._attr_unique_id = f"{self._address}-{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device metadata, including version information when available."""
        data = self.coordinator.data
        return DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            connections={(dr.CONNECTION_BLUETOOTH, self._address)},
            name=self._device_name,
            manufacturer=MANUFACTURER,
            model=MODEL,
            sw_version=(
                data.pcs_firmware_version
                if data and data.pcs_firmware_version is not None
                else data.pcs_software_version if data else None
            ),
            hw_version=data.pcs_hardware_version if data else None,
        )

    @property
    def available(self) -> bool:
        """Return True when the BLE device is reachable.

        ActiveBluetoothDataUpdateCoordinator does not expose
        last_update_success (that is a DataUpdateCoordinator concept).
        Use the coordinator's own .available property instead, which is
        maintained by PassiveBluetoothDataUpdateCoordinator based on
        whether the device is still advertising.
        """
        return self.coordinator.available

    @property
    def native_value(self) -> Any:
        """Return the current sensor value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def icon(self) -> str | None:
        """Return a dynamic icon for selected sensors."""
        value = self.native_value
        key = self.entity_description.key
        if key == "buzzer":
            return "mdi:bell-off" if value == "Silent" else "mdi:bell"
        if key == "battery_flow_state":
            if value == "Charging":
                return "mdi:battery-arrow-up"
            if value == "Discharging":
                return "mdi:battery-arrow-down"
            return "mdi:battery"
        if key in {"alarm_summary", "pcs_alarm", "battery_alarm"}:
            return "mdi:alert-circle" if value and value != "Clear" else "mdi:check-circle-outline"
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return optional extra attributes for selected sensors."""
        if self.entity_description.key in {
            "battery_charged_energy",
            "battery_discharged_energy",
        }:
            now = dt_util.now()
            last_reset = now.replace(hour=0, minute=0, second=0, microsecond=0)
            return {"last_reset": last_reset.isoformat()}
        return None

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()
