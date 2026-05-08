"""The APstorage BLE integration."""
from __future__ import annotations

from datetime import timedelta
import json
import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import async_ble_device_from_address
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_POLL_INTERVAL_SECONDS,
    DOMAIN,
    MANUFACTURER,
    get_model,
    POLL_INTERVAL_MAX_SECONDS,
    POLL_INTERVAL_MIN_SECONDS,
    POLL_INTERVAL_SECONDS,
)
from .coordinator import APstorageCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.SELECT,
    Platform.BUTTON,
    Platform.SWITCH,
    Platform.NUMBER,
]

SERVICE_SET_SYSTEM_MODE = "set_system_mode"
SERVICE_GET_SYSTEM_MODE_PAYLOAD = "get_system_mode_payload"
SERVICE_SET_ADVANCED_SCHEDULE = "set_advanced_schedule"
SERVICE_SET_PEAK_VALLEY_SCHEDULE = "set_peak_valley_schedule"
SERVICE_SET_BUZZER_MODE = "set_buzzer_mode"
SERVICE_CLEAR_BUZZER = "clear_buzzer"
SERVICE_REBOOT_PCS = "reboot_pcs"
SERVICE_SET_SELLING_FIRST = "set_selling_first"
SERVICE_SET_VALLEY_CHARGE = "set_valley_charge"
SERVICE_SET_PEAK_POWER = "set_peak_power"
ATTR_MODE = "mode"
ATTR_BUZZER_MODE = "buzzer_mode"
ATTR_ENABLED = "enabled"
ATTR_PEAK_POWER = "peak_power"
ATTR_PEAK_TIME = "peak_time"
ATTR_VALLEY_TIME = "valley_time"
ATTR_SCHEDULE = "schedule"
ATTR_ENTRY_ID = "entry_id"
ATTR_ADDRESS = "address"
SYSTEM_MODE_PAYLOAD_EVENT = f"{DOMAIN}_system_mode_payload"

_MODE_LABEL_TO_CODE: dict[str, int] = {
    "peak valley": 0,
    "self-consumption": 1,
    "manual control": 2,
    "advanced": 3,
    "backup power supply": 4,
    "peak-shaving": 5,
    "intelligent": 6,
}

SERVICE_SET_SYSTEM_MODE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_MODE): vol.Any(vol.Coerce(int), cv.string),
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(ATTR_ADDRESS): cv.string,
    }
)

SERVICE_GET_SYSTEM_MODE_PAYLOAD_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(ATTR_ADDRESS): cv.string,
    }
)

SERVICE_SET_ADVANCED_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_PEAK_TIME): vol.Any(cv.string, [cv.string]),
        vol.Optional(ATTR_VALLEY_TIME): vol.Any(cv.string, [cv.string]),
        vol.Optional(ATTR_SCHEDULE): vol.Any(cv.string, list),
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(ATTR_ADDRESS): cv.string,
    }
)

SERVICE_SET_PEAK_VALLEY_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_PEAK_TIME): vol.Any(cv.string, [cv.string]),
        vol.Optional(ATTR_VALLEY_TIME): vol.Any(cv.string, [cv.string]),
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(ATTR_ADDRESS): cv.string,
    }
)

SERVICE_SET_BUZZER_MODE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_BUZZER_MODE): vol.Any(vol.Coerce(int), cv.string),
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(ATTR_ADDRESS): cv.string,
    }
)

SERVICE_CLEAR_BUZZER_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(ATTR_ADDRESS): cv.string,
    }
)

SERVICE_REBOOT_PCS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(ATTR_ADDRESS): cv.string,
    }
)

SERVICE_SET_SELLING_FIRST_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENABLED): cv.boolean,
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(ATTR_ADDRESS): cv.string,
    }
)

SERVICE_SET_VALLEY_CHARGE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENABLED): cv.boolean,
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(ATTR_ADDRESS): cv.string,
    }
)

SERVICE_SET_PEAK_POWER_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_PEAK_POWER): vol.Coerce(int),
        vol.Optional(ATTR_ENTRY_ID): cv.string,
        vol.Optional(ATTR_ADDRESS): cv.string,
    }
)

_RANGE_COMPACT_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})$")
_RANGE_HHMM_RE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")


def _parse_hh_mm(hour: str, minute: str) -> tuple[int, int]:
    """Parse and validate HH:MM parts."""
    hh = int(hour)
    mm = int(minute)
    if hh < 0 or hh > 23 or mm < 0 or mm > 59:
        raise HomeAssistantError(f"Invalid time {hour}:{minute}; expected 00:00..23:59")
    return hh, mm


def _normalize_time_range(value: str) -> str:
    """Normalize a time range to HHMMSSHHMMSS format."""
    text = value.strip()

    m_compact = _RANGE_COMPACT_RE.fullmatch(text)
    if m_compact:
        sh, sm, ss, eh, em, es = (int(part) for part in m_compact.groups())
        if sh > 23 or eh > 23 or sm > 59 or em > 59 or ss > 59 or es > 59:
            raise HomeAssistantError(
                f"Invalid compact range {value!r}; expected HHMMSSHHMMSS with valid time fields"
            )
        return text

    m_hhmm = _RANGE_HHMM_RE.fullmatch(text)
    if m_hhmm:
        sh, sm = _parse_hh_mm(m_hhmm.group(1), m_hhmm.group(2))
        eh, em = _parse_hh_mm(m_hhmm.group(3), m_hhmm.group(4))
        return f"{sh:02d}{sm:02d}00{eh:02d}{em:02d}00"

    raise HomeAssistantError(
        f"Invalid range {value!r}; expected 'HH:MM-HH:MM' or 'HHMMSSHHMMSS'"
    )


def _normalize_time_ranges(value: Any, field_name: str) -> list[str]:
    """Normalize range list input from service payload."""
    if value is None:
        return []

    raw_items: list[str]
    if isinstance(value, str):
        raw_items = [value]
    elif isinstance(value, list):
        raw_items = [str(item) for item in value]
    else:
        raise HomeAssistantError(
            f"{field_name} must be a string or a list of strings"
        )

    out: list[str] = []
    for item in raw_items:
        normalized = _normalize_time_range(item)
        out.append(normalized)

    if len(out) > 5:
        raise HomeAssistantError(f"{field_name} supports at most 5 ranges")

    return out


def _range_to_segments(compact: str) -> list[tuple[int, int]]:
    """Convert HHMMSSHHMMSS range to minute segments in a single day.

    Overnight ranges are split into two non-overlapping segments.
    """
    sh = int(compact[0:2])
    sm = int(compact[2:4])
    eh = int(compact[6:8])
    em = int(compact[8:10])

    start = sh * 60 + sm
    end = eh * 60 + em

    if start == end:
        raise HomeAssistantError(
            f"Invalid range {compact}; start and end cannot be equal"
        )

    if end > start:
        return [(start, end)]

    return [(start, 24 * 60), (0, end)]


def _validate_no_overlap(peak_ranges: list[str], valley_ranges: list[str]) -> None:
    """Validate that all configured ranges are pairwise non-overlapping."""
    segments: list[tuple[int, int]] = []
    for compact in peak_ranges + valley_ranges:
        segments.extend(_range_to_segments(compact))

    segments.sort(key=lambda seg: (seg[0], seg[1]))
    prev_end = -1
    for seg_start, seg_end in segments:
        if seg_start < prev_end:
            raise HomeAssistantError(
                "Peak and valley ranges overlap; schedule is invalid"
            )
        prev_end = max(prev_end, seg_end)


def _normalize_schedule_payload(value: Any) -> list[Any]:
    """Normalize optional raw schedule payload returned by app TOU page."""
    if value is None:
        return []

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception as err:  # noqa: BLE001
            raise HomeAssistantError(f"schedule must be valid JSON when provided as string: {err}") from err
        if not isinstance(parsed, list):
            raise HomeAssistantError("schedule JSON must decode to a list")
        return parsed

    if isinstance(value, list):
        return value

    raise HomeAssistantError("schedule must be a list or a JSON string")


def _parse_mode(value: Any) -> int:
    """Parse a mode value from int/code or human label."""
    if isinstance(value, int):
        mode = value
    else:
        text = str(value).strip()
        if text.isdigit():
            mode = int(text)
        else:
            label_key = text.lower()
            if label_key not in _MODE_LABEL_TO_CODE:
                raise HomeAssistantError(
                    f"Invalid mode {value!r}. Use 0-6 or a known label."
                )
            mode = _MODE_LABEL_TO_CODE[label_key]

    if mode < 0 or mode > 6:
        raise HomeAssistantError("mode must be in range 0..6")
    return mode


def _parse_buzzer_mode(value: Any) -> int:
    """Parse buzzer mode from int/code or human label."""
    if isinstance(value, int):
        mode = value
    else:
        text = str(value).strip().lower()
        if text.isdigit():
            mode = int(text)
        elif text in {"silent", "off", "mute", "muted"}:
            mode = 0
        elif text in {"normal", "on", "enabled"}:
            mode = 1
        else:
            raise HomeAssistantError(
                f"Invalid buzzer_mode {value!r}. Use 0/1 or label (Silent/Normal)."
            )

    if mode not in {0, 1}:
        raise HomeAssistantError("buzzer_mode must be 0 or 1")
    return mode


def _parse_peak_power(value: Any) -> int:
    """Parse and validate peak-shaving power setpoint."""
    try:
        peak_power = int(value)
    except (TypeError, ValueError) as err:
        raise HomeAssistantError(f"Invalid peak_power {value!r}. Use an integer.") from err

    if peak_power < 100 or peak_power > 50000:
        raise HomeAssistantError("peak_power must be in range 100..50000")

    return peak_power


def _resolve_target_coordinator(
    hass: HomeAssistant,
    *,
    entry_id: str | None,
    address: str | None,
) -> APstorageCoordinator:
    """Resolve a single target coordinator for a service call."""
    coordinators: dict[str, APstorageCoordinator] = hass.data.get(DOMAIN, {})
    if not coordinators:
        raise HomeAssistantError("No APstorage BLE config entries are loaded")

    if entry_id is not None:
        coordinator = coordinators.get(entry_id)
        if coordinator is None:
            raise HomeAssistantError(f"Unknown entry_id: {entry_id}")
        return coordinator

    if address is not None:
        wanted = address.upper()
        for coordinator in coordinators.values():
            if coordinator._address.upper() == wanted:  # pylint: disable=protected-access
                return coordinator
        raise HomeAssistantError(f"No APstorage BLE entry found for address: {address}")

    if len(coordinators) == 1:
        return next(iter(coordinators.values()))

    raise HomeAssistantError(
        "Multiple APstorage BLE entries loaded; provide entry_id or address"
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up APstorage BLE from a config entry."""
    address: str = entry.data[CONF_ADDRESS]
    name: str = entry.title
    poll_interval = int(entry.options.get(CONF_POLL_INTERVAL_SECONDS, POLL_INTERVAL_SECONDS))
    poll_interval = max(POLL_INTERVAL_MIN_SECONDS, min(POLL_INTERVAL_MAX_SECONDS, poll_interval))

    # Verify that HA can see the device (or a proxy for it) before proceeding.
    # This prevents ConfigEntryNotReady loops when the device is temporarily
    # out of range — HA will retry setup automatically once the device
    # re-appears via the Bluetooth integration.
    ble_device = async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise ConfigEntryNotReady(
            f"Cannot find BLE device {address!r}. "
            "Make sure the PCS is powered on and in range of an ESPHome "
            "Bluetooth proxy or a local Bluetooth adapter."
        )

    coordinator = APstorageCoordinator(
        hass=hass,
        logger=_LOGGER,
        address=address,
        name=name,
        poll_interval_seconds=poll_interval,
    )
    await coordinator.async_initialize()

    # Ensure a device is registered even before entities are added so the
    # Battery State of Charge sensor is always attached to a concrete device.
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, address)},
        connections={(dr.CONNECTION_BLUETOOTH, address)},
        manufacturer=MANUFACTURER,
        model=get_model(address),
        name=name,
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    if not hass.services.has_service(DOMAIN, SERVICE_SET_SYSTEM_MODE):

        async def _async_handle_set_system_mode(call: ServiceCall) -> None:
            mode = _parse_mode(call.data[ATTR_MODE])
            target = _resolve_target_coordinator(
                hass,
                entry_id=call.data.get(ATTR_ENTRY_ID),
                address=call.data.get(ATTR_ADDRESS),
            )
            await target.async_set_system_mode(mode)

        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_SYSTEM_MODE,
            _async_handle_set_system_mode,
            schema=SERVICE_SET_SYSTEM_MODE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_ADVANCED_SCHEDULE):

        async def _async_handle_set_advanced_schedule(call: ServiceCall) -> None:
            peak_time = _normalize_time_ranges(call.data.get(ATTR_PEAK_TIME), ATTR_PEAK_TIME)
            valley_time = _normalize_time_ranges(call.data.get(ATTR_VALLEY_TIME), ATTR_VALLEY_TIME)
            schedule = _normalize_schedule_payload(call.data.get(ATTR_SCHEDULE))

            if schedule and (peak_time or valley_time):
                raise HomeAssistantError(
                    "Use either schedule or peak_time/valley_time, not both"
                )

            if not schedule and not peak_time and not valley_time:
                raise HomeAssistantError(
                    "At least one of schedule or peak_time/valley_time must be provided"
                )

            if not schedule:
                _validate_no_overlap(peak_time, valley_time)


            target = _resolve_target_coordinator(
                hass,
                entry_id=call.data.get(ATTR_ENTRY_ID),
                address=call.data.get(ATTR_ADDRESS),
            )
            await target.async_set_advanced_schedule(
                peak_time=peak_time,
                valley_time=valley_time,
                schedule=schedule,
            )

        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_ADVANCED_SCHEDULE,
            _async_handle_set_advanced_schedule,
            schema=SERVICE_SET_ADVANCED_SCHEDULE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_GET_SYSTEM_MODE_PAYLOAD):

        async def _async_handle_get_system_mode_payload(call: ServiceCall) -> None:
            target = _resolve_target_coordinator(
                hass,
                entry_id=call.data.get(ATTR_ENTRY_ID),
                address=call.data.get(ATTR_ADDRESS),
            )
            result = await target.async_read_system_mode_payload()
            payload = {
                "entry_id": call.data.get(ATTR_ENTRY_ID),
                "address": target._address,  # pylint: disable=protected-access
                "storage_id": result.get("storage_id"),
                "code": result.get("code"),
                "message": result.get("message"),
                "payload": result.get("payload"),
            }
            hass.bus.async_fire(SYSTEM_MODE_PAYLOAD_EVENT, payload)

        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_SYSTEM_MODE_PAYLOAD,
            _async_handle_get_system_mode_payload,
            schema=SERVICE_GET_SYSTEM_MODE_PAYLOAD_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_PEAK_VALLEY_SCHEDULE):

        async def _async_handle_set_peak_valley_schedule(call: ServiceCall) -> None:
            peak_time = _normalize_time_ranges(call.data.get(ATTR_PEAK_TIME), ATTR_PEAK_TIME)
            valley_time = _normalize_time_ranges(call.data.get(ATTR_VALLEY_TIME), ATTR_VALLEY_TIME)

            if not peak_time and not valley_time:
                raise HomeAssistantError(
                    "At least one of peak_time or valley_time must be provided"
                )

            _validate_no_overlap(peak_time, valley_time)

            target = _resolve_target_coordinator(
                hass,
                entry_id=call.data.get(ATTR_ENTRY_ID),
                address=call.data.get(ATTR_ADDRESS),
            )
            await target.async_set_peak_valley_schedule(
                peak_time=peak_time,
                valley_time=valley_time,
            )

        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_PEAK_VALLEY_SCHEDULE,
            _async_handle_set_peak_valley_schedule,
            schema=SERVICE_SET_PEAK_VALLEY_SCHEDULE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_BUZZER_MODE):

        async def _async_handle_set_buzzer_mode(call: ServiceCall) -> None:
            mode = _parse_buzzer_mode(call.data[ATTR_BUZZER_MODE])
            target = _resolve_target_coordinator(
                hass,
                entry_id=call.data.get(ATTR_ENTRY_ID),
                address=call.data.get(ATTR_ADDRESS),
            )
            await target.async_set_buzzer_mode(mode)

        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_BUZZER_MODE,
            _async_handle_set_buzzer_mode,
            schema=SERVICE_SET_BUZZER_MODE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_CLEAR_BUZZER):

        async def _async_handle_clear_buzzer(call: ServiceCall) -> None:
            target = _resolve_target_coordinator(
                hass,
                entry_id=call.data.get(ATTR_ENTRY_ID),
                address=call.data.get(ATTR_ADDRESS),
            )
            await target.async_clear_buzzer()

        hass.services.async_register(
            DOMAIN,
            SERVICE_CLEAR_BUZZER,
            _async_handle_clear_buzzer,
            schema=SERVICE_CLEAR_BUZZER_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_REBOOT_PCS):

        async def _async_handle_reboot_pcs(call: ServiceCall) -> None:
            target = _resolve_target_coordinator(
                hass,
                entry_id=call.data.get(ATTR_ENTRY_ID),
                address=call.data.get(ATTR_ADDRESS),
            )
            await target.async_reboot_pcs()

        hass.services.async_register(
            DOMAIN,
            SERVICE_REBOOT_PCS,
            _async_handle_reboot_pcs,
            schema=SERVICE_REBOOT_PCS_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_SELLING_FIRST):

        async def _async_handle_set_selling_first(call: ServiceCall) -> None:
            enabled = bool(call.data[ATTR_ENABLED])
            target = _resolve_target_coordinator(
                hass,
                entry_id=call.data.get(ATTR_ENTRY_ID),
                address=call.data.get(ATTR_ADDRESS),
            )
            await target.async_set_selling_first(enabled)

        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_SELLING_FIRST,
            _async_handle_set_selling_first,
            schema=SERVICE_SET_SELLING_FIRST_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_VALLEY_CHARGE):

        async def _async_handle_set_valley_charge(call: ServiceCall) -> None:
            enabled = bool(call.data[ATTR_ENABLED])
            target = _resolve_target_coordinator(
                hass,
                entry_id=call.data.get(ATTR_ENTRY_ID),
                address=call.data.get(ATTR_ADDRESS),
            )
            await target.async_set_valley_charge(enabled)

        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_VALLEY_CHARGE,
            _async_handle_set_valley_charge,
            schema=SERVICE_SET_VALLEY_CHARGE_SCHEMA,
        )

    if not hass.services.has_service(DOMAIN, SERVICE_SET_PEAK_POWER):

        async def _async_handle_set_peak_power(call: ServiceCall) -> None:
            peak_power = _parse_peak_power(call.data[ATTR_PEAK_POWER])
            target = _resolve_target_coordinator(
                hass,
                entry_id=call.data.get(ATTR_ENTRY_ID),
                address=call.data.get(ATTR_ADDRESS),
            )
            await target.async_set_peak_power(peak_power)

        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_PEAK_POWER,
            _async_handle_set_peak_power,
            schema=SERVICE_SET_PEAK_POWER_SCHEMA,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Start listening for Bluetooth advertisements *after* the platform has had
    # a chance to subscribe, so no updates are missed.
    entry.async_on_unload(coordinator.async_start())

    # Fallback periodic poll so sensors continue updating even when
    # advertisement events are sparse (common with some proxies/adapters).
    @callback
    def _periodic_poll(_now) -> None:
        hass.async_create_task(coordinator.async_periodic_poll())

    entry.async_on_unload(
        async_track_time_interval(
            hass,
            _periodic_poll,
            timedelta(seconds=poll_interval),
        )
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator: APstorageCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        await coordinator.async_shutdown()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
