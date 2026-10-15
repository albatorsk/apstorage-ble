"""Sensor platform for the APstorage BLE integration."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
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
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfMass,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BLE_NAME, DOMAIN, MANUFACTURER, get_model
from .coordinator import APstorageCoordinator
from .models import PCSData
from .protocol import MODE_CODE_TO_OPTION

_LOGGER = logging.getLogger(__name__)


def _format_system_mode(value: Any) -> Any:
    """Return the label for a mode code, or the raw value."""
    if value is None:
        return None
    return MODE_CODE_TO_OPTION.get(str(value), value)


@dataclass(frozen=True, kw_only=True)
class APstorageSensorDescription(SensorEntityDescription):
    """Describes a sensor fed from PCSData."""

    value_fn: Callable[[PCSData], Any]


def _power(key: str, name: str, value_fn: Callable[[PCSData], Any], **kwargs: Any) -> APstorageSensorDescription:
    return APstorageSensorDescription(
        key=key,
        name=name,
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=value_fn,
        **kwargs,
    )


def _daily_energy(key: str, name: str, value_fn: Callable[[PCSData], Any], **kwargs: Any) -> APstorageSensorDescription:
    # Daily counters reset at midnight; TOTAL_INCREASING treats the drop as a new cycle.
    return APstorageSensorDescription(
        key=key,
        name=name,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        suggested_display_precision=2,
        value_fn=value_fn,
        **kwargs,
    )


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
    _power(
        "battery_discharging_power",
        "Battery Discharging Power",
        lambda d: max(d.signed_battery_power, 0) if d.signed_battery_power is not None else None,
    ),
    _power("battery_charging_power", "Battery Charging Power", lambda d: d.battery_charging_power),
    _power("battery_power_net", "Battery Power", lambda d: d.signed_battery_power),
    _daily_energy("battery_charged_energy", "Daily Charged Energy", lambda d: d.battery_charged_energy),
    _daily_energy("battery_discharged_energy", "Daily Discharged Energy", lambda d: d.battery_discharged_energy),
    APstorageSensorDescription(
        key="battery_flow_state",
        name="Battery Flow State",
        device_class=SensorDeviceClass.ENUM,
        options=["Charging", "Discharging", "Holding"],
        value_fn=lambda d: d.battery_flow_state,
    ),
    APstorageSensorDescription(
        key="battery_capacity",
        name="Battery Capacity",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY_STORAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.battery_capacity,
    ),
    APstorageSensorDescription(
        key="min_soc",
        name="Minimum SOC",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:battery-low",
        value_fn=lambda d: d.min_soc,
    ),
    APstorageSensorDescription(
        key="backup_soc_level",
        name="Backup SOC Level",
        native_unit_of_measurement=PERCENTAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:battery-lock",
        value_fn=lambda d: d.backup_soc,
    ),
    # --- PV ---
    _power("pv_power", "PV Power", lambda d: d.pv_power),
    _daily_energy("pv_energy_produced", "Daily PV Energy Produced", lambda d: d.pv_energy_produced),
    # --- Load ---
    _power("load_power", "Load Power", lambda d: d.load_power),
    _daily_energy("total_consumed_daily", "Daily Total Consumed", lambda d: d.total_consumed_daily),
    # --- Grid ---
    _power("grid_power", "Grid Power", lambda d: d.grid_power),
    _power("grid_import_power", "Grid Import Power", lambda d: d.grid_import_power),
    _power("grid_export_power", "Grid Export Power", lambda d: d.grid_export_power),
    _daily_energy("grid_imported_energy", "Daily Grid Import", lambda d: d.grid_imported_energy),
    _daily_energy("grid_exported_energy", "Daily Grid Export", lambda d: d.grid_exported_energy),
    APstorageSensorDescription(
        key="grid_frequency",
        name="Grid Frequency",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.grid_frequency,
    ),
    APstorageSensorDescription(
        key="grid_status",
        name="Grid Status",
        device_class=SensorDeviceClass.ENUM,
        options=["On-grid", "Off-grid"],
        value_fn=lambda d: d.grid_status,
    ),
    _power(
        "generator_power",
        "Generator Power",
        lambda d: d.generator_power,
        entity_registry_enabled_default=False,
    ),
    # --- Lifetime ---
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
        key="co2_reduction",
        name="CO2 Reduction",
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        device_class=SensorDeviceClass.WEIGHT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda d: d.co2_reduction,
    ),
    # --- Mode / status ---
    APstorageSensorDescription(
        key="system_state",
        name="System Mode",
        value_fn=lambda d: _format_system_mode(d.system_mode),
    ),
    APstorageSensorDescription(
        key="price_area",
        name="Price Area",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:map-marker",
        value_fn=lambda d: d.price_area,
    ),
    APstorageSensorDescription(
        key="buzzer_alarm",
        name="Buzzer Alarm",
        device_class=SensorDeviceClass.ENUM,
        options=["Active", "None"],
        value_fn=lambda d: None if d.buzzer_alarm is None else ("Active" if d.buzzer_alarm else "None"),
    ),
    # --- Device ---
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
        key="comm_firmware_version",
        name="Communication Module Firmware",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.comm_firmware_version,
    ),
    APstorageSensorDescription(
        key="wifi_ssid",
        name="Wi-Fi Network",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:wifi",
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.wifi_ssid,
    ),
    APstorageSensorDescription(
        key="wifi_rssi",
        name="Wi-Fi Signal",
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.wifi_rssi,
    ),
    APstorageSensorDescription(
        key="wifi_ip",
        name="Wi-Fi IP Address",
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:ip-network",
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.wifi_ip,
    ),
    APstorageSensorDescription(
        key="cloud_connection",
        name="Cloud Connection",
        device_class=SensorDeviceClass.ENUM,
        options=["Connected", "Disconnected"],
        entity_category=EntityCategory.DIAGNOSTIC,
        icon="mdi:cloud-outline",
        value_fn=lambda d: None if d.cloud_connected is None else ("Connected" if d.cloud_connected else "Disconnected"),
    ),
)


@dataclass(frozen=True, kw_only=True)
class APstorageDiagnosticDescription(SensorEntityDescription):
    """Describes a sensor fed from the coordinator itself (always available)."""

    value_fn: Callable[[APstorageCoordinator], Any]
    attrs_fn: Callable[[APstorageCoordinator], dict[str, Any] | None] = lambda c: None


def _payload_read_state(coordinator: APstorageCoordinator) -> str:
    read = coordinator.last_system_mode_payload_read
    if read is None:
        return "Unread"
    return "Read" if read.get("ok") else "Failed"


DIAGNOSTIC_DESCRIPTIONS: tuple[APstorageDiagnosticDescription, ...] = (
    APstorageDiagnosticDescription(
        key="system_mode_payload_read",
        name="System Mode Payload",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:file-search-outline",
        value_fn=_payload_read_state,
        attrs_fn=lambda c: (
            None
            if c.last_system_mode_payload_read is None
            else {
                "last_read_ok": c.last_system_mode_payload_read.get("ok"),
                "last_read_code": c.last_system_mode_payload_read.get("code"),
                "last_read_message": c.last_system_mode_payload_read.get("message"),
                "last_read_storage_id": c.last_system_mode_payload_read.get("storage_id"),
                "last_read_at": c.last_system_mode_payload_read.get("at"),
                "payload": c.last_system_mode_payload_read.get("payload"),
            }
        ),
    ),
    APstorageDiagnosticDescription(
        key="last_update",
        name="Last Update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda c: c.last_successful_poll_at,
    ),
    APstorageDiagnosticDescription(
        key="connection_quality",
        name="Connection Quality",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:bluetooth-connect",
        value_fn=lambda c: c.connection_quality,
        attrs_fn=lambda c: {
            "total_polls": c.total_poll_attempts,
            "successful_polls": c.successful_poll_count,
            "consecutive_failures": c.consecutive_poll_failures,
            "last_error": c.last_poll_error,
        },
    ),
    APstorageDiagnosticDescription(
        key="ble_att_mtu",
        name="BLE MTU",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        icon="mdi:unfold-more-horizontal",
        value_fn=lambda c: c.ble_att_mtu,
    ),
)


async def _async_migrate_sensor_unique_ids(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Migrate renamed sensor unique IDs in the entity registry."""
    address: str = entry.data[CONF_ADDRESS]
    entity_registry = er.async_get(hass)

    migrations = {
        f"{address}-battery_power": f"{address}-battery_discharging_power",
    }

    removed_sensor_unique_ids = {
        f"{address}-alarm_summary",
        f"{address}-pcs_alarm",
        f"{address}-battery_alarm",
        f"{address}-pcs_hardware_version",
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

    for unique_id in removed_sensor_unique_ids:
        entity_id = entity_registry.async_get_entity_id("sensor", DOMAIN, unique_id)
        if entity_id:
            entity_registry.async_remove(entity_id)
            _LOGGER.debug("Removed deprecated sensor entity with unique ID %s", unique_id)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up APstorage BLE sensors from a config entry."""
    await _async_migrate_sensor_unique_ids(hass, entry)
    coordinator: APstorageCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        APstorageSensor(coordinator, entry, description) for description in SENSOR_DESCRIPTIONS
    ]
    entities.extend(
        APstorageDiagnosticSensor(coordinator, entry, description)
        for description in DIAGNOSTIC_DESCRIPTIONS
    )
    async_add_entities(entities)


class _APstorageSensorBase(CoordinatorEntity[APstorageCoordinator], SensorEntity):
    """Common device info and unique ID."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: APstorageCoordinator,
        entry: ConfigEntry,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._address: str = entry.data[CONF_ADDRESS]
        self._device_name = entry.title
        self._model = get_model(entry.data.get(CONF_BLE_NAME))
        # Unique ID: MAC + sensor key so entities survive renames.
        self._attr_unique_id = f"{self._address}-{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device metadata, including firmware versions when known."""
        data = self.coordinator.data
        return DeviceInfo(
            identifiers={(DOMAIN, self._address)},
            connections={(dr.CONNECTION_BLUETOOTH, self._address)},
            name=self._device_name,
            manufacturer=MANUFACTURER,
            model=self._model,
            serial_number=data.device_id if data else None,
            sw_version=data.pcs_firmware_version.replace("_", " / ")
            if data and data.pcs_firmware_version
            else None,
        )


class APstorageSensor(_APstorageSensorBase):
    """A value reported by the PCS."""

    entity_description: APstorageSensorDescription

    @property
    def available(self) -> bool:
        return self.coordinator.runtime_available

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def icon(self) -> str | None:
        if self.entity_description.key == "battery_flow_state":
            value = self.native_value
            if value == "Charging":
                return "mdi:battery-arrow-up"
            if value == "Discharging":
                return "mdi:battery-arrow-down"
            return "mdi:battery"
        if self.entity_description.key == "grid_status":
            return "mdi:transmission-tower" if self.native_value == "On-grid" else "mdi:transmission-tower-off"
        if self.entity_description.key == "buzzer_alarm":
            return "mdi:bell-alert" if self.native_value == "Active" else "mdi:bell-outline"
        return self.entity_description.icon


class APstorageDiagnosticSensor(_APstorageSensorBase):
    """Integration diagnostics; available even when the PCS is not."""

    entity_description: APstorageDiagnosticDescription

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.coordinator)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return self.entity_description.attrs_fn(self.coordinator)
