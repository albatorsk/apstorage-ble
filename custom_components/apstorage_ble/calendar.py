"""Calendar of the PCS's charge and discharge windows."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_BLE_NAME, DOMAIN, MANUFACTURER, get_model
from .coordinator import APstorageCoordinator
from .protocol import mode_name
from .schedule import CHARGE, Event, active_windows, expand, format_window

# The schedule is what the PCS holds now; showing it far into the past or
# future would suggest a history or a plan that may not exist.
PAST_DAYS = 1
FUTURE_DAYS = 7


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the schedule calendar."""
    coordinator: APstorageCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([APstorageScheduleCalendar(coordinator, entry)])


class APstorageScheduleCalendar(CoordinatorEntity[APstorageCoordinator], CalendarEntity):
    """Charge (valley) and discharge (peak) windows from getsysmode."""

    _attr_has_entity_name = True
    _attr_name = "Schedule"
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: APstorageCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        address: str = entry.data[CONF_ADDRESS]
        self._attr_unique_id = f"{address}-schedule"
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

    def _sysmode(self) -> dict[str, Any] | None:
        data = self.coordinator.data
        return data.sysmode if data is not None else None

    def _events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        now = dt_util.now()
        start = max(start, now - timedelta(days=PAST_DAYS))
        end = min(end, now + timedelta(days=FUTURE_DAYS))
        sysmode = self._sysmode()
        mode = mode_name((sysmode or {}).get("mode"))
        windows = active_windows(sysmode)
        return [
            self._to_calendar_event(event, mode)
            for event in expand(windows, start, end, dt_util.get_default_time_zone())
        ]

    @staticmethod
    def _to_calendar_event(event: Event, mode: str | None) -> CalendarEvent:
        verb = "charges the battery" if event.kind == CHARGE else "discharges the battery"
        description = f"The PCS {verb} ({mode} mode)." if mode else f"The PCS {verb}."
        return CalendarEvent(
            start=event.start,
            end=event.end,
            summary=event.kind,
            description=description,
            uid=f"{event.kind.lower()}-{event.start.isoformat()}",
        )

    @property
    def event(self) -> CalendarEvent | None:
        """The current window, or the next one."""
        now = dt_util.now()
        upcoming = [e for e in self._events(now, now + timedelta(days=FUTURE_DAYS)) if e.end > now]
        return upcoming[0] if upcoming else None

    async def async_get_events(
        self, hass: HomeAssistant, start_date: datetime, end_date: datetime
    ) -> list[CalendarEvent]:
        return self._events(start_date, end_date)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        sysmode = self._sysmode()
        if sysmode is None:
            return None
        windows = active_windows(sysmode)
        return {
            "mode": mode_name(sysmode.get("mode")),
            "windows": [format_window(window) for window in windows],
        }
