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

from .const import DOMAIN, MANUFACTURER, get_model
from .coordinator import APstorageCoordinator

_LOGGER = logging.getLogger(__name__)

MODE_CODE_TO_OPTION: dict[str, str] = {
    "0": "Peak Valley",
    "1": "Self-Consumption",
    "2": "Manual Control",
    "3": "Advanced",
    "4": "Backup power supply",
    "5": "Peak-Shaving",
    "6": "Intelligent",
}

OPTION_TO_MODE_CODE: dict[str, str] = {v: k for k, v in MODE_CODE_TO_OPTION.items()}
BACKUP_SOC_OPTIONS: list[str] = [str(value) for value in range(20, 91, 10)]
BUZZER_MODE_CODE_TO_OPTION: dict[int, str] = {
    0: "Silent",
    1: "Normal",
}
BUZZER_MODE_OPTION_TO_CODE: dict[str, int] = {
    label: code for code, label in BUZZER_MODE_CODE_TO_OPTION.items()
}

# Accept labels that may already be exposed by system-state sensor formatting.
LEGACY_STATE_TO_CODE: dict[str, str] = {
    "Self-consumption": "1",
    "Self-Consumption": "1",
    "self consumption": "1",
    "self-consumption": "1",
    "Advanced": "3",
    "Intelligent": "6",
}

LEGACY_STATE_TO_CODE_NORMALIZED: dict[str, str] = {
    "selfconsumption": "1",
    "advanced": "3",
    "intelligent": "6",
}


def _normalize_mode_code(value: Any) -> str | None:
    """Normalize mode value to a compact integer code string (e.g. '1')."""
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


def _normalize_label(value: Any) -> str:
    """Normalize human-readable labels for tolerant matching."""
    return "".join(ch for ch in str(value).strip().lower() if ch.isalnum())


def _normalize_backup_soc_option(value: Any) -> str | None:
    """Convert raw backup SoC into the nearest supported select option."""
    try:
        raw = int(round(float(value)))
    except (TypeError, ValueError):
        return None

    # Device writes are constrained to 20..90 and select options are 10% steps.
    clamped = max(20, min(90, raw))
    snapped = int(round((clamped - 20) / 10.0) * 10 + 20)
    option = str(snapped)
    return option if option in BACKUP_SOC_OPTIONS else None


@dataclass(frozen=True, kw_only=True)
class APstorageSelectDescription(SelectEntityDescription):
    """Description for APstorage select entities."""


SYSTEM_MODE_SELECT = APstorageSelectDescription(
    key="system_mode",
    name="System Mode",
    options=list(OPTION_TO_MODE_CODE.keys()),
)

BACKUP_SOC_SELECT = APstorageSelectDescription(
    key="backup_soc",
    name="Backup SOC",
    options=BACKUP_SOC_OPTIONS,
)

BUZZER_MODE_SELECT = APstorageSelectDescription(
    key="buzzer_mode",
    name="Buzzer Mode",
    options=list(BUZZER_MODE_OPTION_TO_CODE.keys()),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up APstorage select entities."""
    coordinator: APstorageCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            APstorageSystemModeSelect(coordinator, entry, SYSTEM_MODE_SELECT),
            APstorageBackupSocSelect(coordinator, entry, BACKUP_SOC_SELECT),
            APstorageBuzzerModeSelect(coordinator, entry, BUZZER_MODE_SELECT),
        ]
    )


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
            model=get_model(address),
        )

    @property
    def available(self) -> bool:
        """Return availability from Bluetooth coordinator reachability."""
        return self.coordinator.runtime_available

    @property
    def current_option(self) -> str | None:
        """Return the currently active system mode option."""
        data = self.coordinator.data
        if data is None:
            return None

        if data.system_mode is not None:
            option = MODE_CODE_TO_OPTION.get(_normalize_mode_code(data.system_mode) or "")
            if option is not None:
                return option

        if data.system_state is not None:
            state = _normalize_mode_code(data.system_state) or ""
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


class APstorageBackupSocSelect(
    CoordinatorEntity[APstorageCoordinator],
    SelectEntity,
):
    """Writable backup SOC selector for supported APstorage modes."""

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
            state = _normalize_mode_code(data.system_state) or ""
            if state in MODE_CODE_TO_OPTION:
                return state
            legacy = LEGACY_STATE_TO_CODE.get(state)
            if legacy is not None:
                return legacy
            return LEGACY_STATE_TO_CODE_NORMALIZED.get(_normalize_label(state))

        return None

    @property
    def available(self) -> bool:
        """Only available when connected and mode supports backup SOC."""
        if not self.coordinator.runtime_available:
            return False
        return self._current_mode_code() in {"1", "3"}

    @property
    def current_option(self) -> str | None:
        """Return current backup SOC as string percent without percent sign."""
        data = self.coordinator.data
        if data is not None and data.backup_soc is not None:
            current = _normalize_backup_soc_option(data.backup_soc)
            if current is not None:
                return current

        write = self.coordinator.last_backup_soc_write
        if write is not None:
            requested = _normalize_backup_soc_option(write.get("requested_backup_soc"))
            if requested is not None:
                return requested

        return None

    async def async_select_option(self, option: str) -> None:
        """Set backup SOC on the device."""
        if option not in BACKUP_SOC_OPTIONS:
            raise ValueError(f"Unknown backup SOC option: {option}")

        _LOGGER.debug("Setting APstorage backup SOC to %s", option)
        await self.coordinator.async_set_backup_soc(int(option))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose backup SOC write diagnostics for automations/debugging."""
        attrs: dict[str, Any] = {}

        mode_code = self._current_mode_code()
        if mode_code is not None:
            attrs["mode_code"] = mode_code
            attrs["mode_name"] = MODE_CODE_TO_OPTION.get(mode_code)

        write = self.coordinator.last_backup_soc_write
        if write is not None:
            attrs["last_write_ok"] = write.get("ok")
            attrs["last_write_code"] = write.get("code")
            attrs["last_write_message"] = write.get("message")
            attrs["last_write_requested_backup_soc"] = write.get("requested_backup_soc")
            attrs["last_write_at"] = write.get("at")

        return attrs or None


class APstorageBuzzerModeSelect(
    CoordinatorEntity[APstorageCoordinator],
    SelectEntity,
):
    """Writable buzzer mode selector for APstorage storage device."""

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
            model=get_model(address),
        )

    @property
    def available(self) -> bool:
        """Return availability from Bluetooth coordinator reachability."""
        return self.coordinator.runtime_available

    @property
    def current_option(self) -> str | None:
        """Return the currently active buzzer mode option."""
        data = self.coordinator.data
        if data is not None and data.buzzer is not None:
            return BUZZER_MODE_CODE_TO_OPTION.get(int(data.buzzer))

        write = self.coordinator.last_buzzer_mode_write
        if write is not None:
            requested = str(write.get("requested_buzzer_mode") or "")
            if requested.isdigit():
                return BUZZER_MODE_CODE_TO_OPTION.get(int(requested))

        return None

    async def async_select_option(self, option: str) -> None:
        """Set buzzer mode on the device."""
        mode_code = BUZZER_MODE_OPTION_TO_CODE.get(option)
        if mode_code is None:
            raise ValueError(f"Unknown buzzer mode option: {option}")

        _LOGGER.debug("Setting APstorage buzzer mode to %s (%s)", option, mode_code)
        await self.coordinator.async_set_buzzer_mode(mode_code)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose write diagnostics for automation/debugging."""
        attrs: dict[str, Any] = {}

        write = self.coordinator.last_buzzer_mode_write
        if write is not None:
            attrs["last_write_ok"] = write.get("ok")
            attrs["last_write_code"] = write.get("code")
            attrs["last_write_message"] = write.get("message")
            attrs["last_write_requested_buzzer_mode"] = write.get("requested_buzzer_mode")
            attrs["last_write_at"] = write.get("at")

        return attrs or None

