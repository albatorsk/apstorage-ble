"""Polling coordinator for the APstorage BLE integration.

Every poll opens one BLE connection, reads live data (`getDeviceLastDataLocal`)
and the operating-mode settings (`getsysmode`), and disconnects. `deviceInfo`
(device ID, firmware, Wi-Fi) is read on the first poll and then every few hours.
Writes use their own short connection and update the data from the read-back.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
import logging
import time
from typing import Any, TypeVar

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .models import PCSData
from .pcs_client import PCSClient, PCSDeviceError, PCSError
from .protocol import normalize_mode_code

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

DEVICE_INFO_REFRESH_SECONDS = 6 * 3600
# Keep showing the last values through this many failed polls in a row.
MAX_TOLERATED_POLL_FAILURES = 3
# The PCS restarts after set/pcsReboot and may not answer the request.
REBOOT_REFRESH_DELAY_SECONDS = 60

# Defaults the app uses for missing setsysmode fields.
_SYSMODE_DEFAULTS = {
    "valleycharge": "1",
    "backupSOC": "50",
    "peakPower": "5000",
    "sellingFirst": "0",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class APstorageCoordinator(DataUpdateCoordinator[PCSData]):
    """Polls one APstorage PCS over BLE and runs its write commands."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        *,
        address: str,
        name: str,
        ble_name: str | None,
        poll_interval_seconds: int,
    ) -> None:
        super().__init__(
            hass,
            logger,
            name=f"{DOMAIN} {name}",
            update_interval=timedelta(seconds=poll_interval_seconds),
        )
        self._address = address
        self._device_name = name
        self._client = PCSClient(ble_name)
        self._lock = asyncio.Lock()
        self._shutdown = False
        self._device_info_read_at: float | None = None
        self._reboot_refresh: asyncio.TimerHandle | None = None

        self.total_poll_attempts = 0
        self.successful_poll_count = 0
        self.consecutive_poll_failures = 0
        self.last_successful_poll_at: datetime | None = None
        self.last_poll_error: str | None = None

        self._last_system_mode_write: dict[str, Any] | None = None
        self._last_backup_soc_write: dict[str, Any] | None = None
        self._last_system_mode_payload_read: dict[str, Any] | None = None
        self._last_version_probe_read: dict[str, Any] | None = None
        self._last_advanced_schedule_write: dict[str, Any] | None = None
        self._last_peak_valley_schedule_write: dict[str, Any] | None = None
        self._last_buzzer_mode_write: dict[str, Any] | None = None
        self._last_clear_buzzer_write: dict[str, Any] | None = None
        self._last_pcs_reboot_write: dict[str, Any] | None = None
        self._last_selling_first_write: dict[str, Any] | None = None
        self._last_valley_charge_write: dict[str, Any] | None = None
        self._last_peak_power_write: dict[str, Any] | None = None

    # ------------------------------------------------------------ state

    @property
    def runtime_available(self) -> bool:
        """True while recent data from the PCS is available."""
        return self.last_update_success and self.data is not None

    @property
    def connection_quality(self) -> float | None:
        """Share of successful polls, in percent."""
        if not self.total_poll_attempts:
            return None
        return round(100.0 * self.successful_poll_count / self.total_poll_attempts, 1)

    @property
    def ble_att_mtu(self) -> int | None:
        """ATT MTU negotiated on the last connection."""
        return self._client.last_mtu

    def _ble_device(self) -> Any:
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self._address, connectable=True
        )
        if ble_device is None:
            raise PCSError(
                f"{self._address} is not in range of a connectable Bluetooth adapter or proxy"
            )
        return ble_device

    async def async_shutdown(self) -> None:
        """Stop polling and cancel pending work."""
        self._shutdown = True
        if self._reboot_refresh is not None:
            self._reboot_refresh.cancel()
            self._reboot_refresh = None
        await super().async_shutdown()

    # ------------------------------------------------------------ polling

    async def _async_update_data(self) -> PCSData:
        """Read live data, getsysmode and (now and then) deviceInfo."""
        if self._shutdown:
            raise UpdateFailed("shutting down")
        self.total_poll_attempts += 1
        want_info = (
            self._device_info_read_at is None
            or time.monotonic() - self._device_info_read_at > DEVICE_INFO_REFRESH_SECONDS
        )
        try:
            async with self._lock:
                result = await self._client.async_read(
                    self._ble_device(), device_info=want_info, sysmode=True
                )
        except PCSError as err:
            self.consecutive_poll_failures += 1
            self.last_poll_error = str(err)
            if self.data is not None and self.consecutive_poll_failures < MAX_TOLERATED_POLL_FAILURES:
                self.logger.debug(
                    "Poll %d failed (%s); keeping last values", self.consecutive_poll_failures, err
                )
                return self.data
            raise UpdateFailed(str(err)) from err

        data = self.data or PCSData()
        data.apply_last_data(result.last_data)
        if result.sysmode is not None:
            data.apply_sysmode(result.sysmode)
        if result.device_info is not None:
            data.apply_device_info(result.device_info)
            self._device_info_read_at = time.monotonic()
        if not data.device_id:
            data.device_id = result.device_id

        self.successful_poll_count += 1
        self.consecutive_poll_failures = 0
        self.last_poll_error = None
        self.last_successful_poll_at = datetime.now(timezone.utc)
        return data

    # ------------------------------------------------------------ writes

    async def _async_run(
        self,
        record_attr: str,
        requested: dict[str, Any],
        operation: Callable[[Any], Awaitable[_T]],
    ) -> _T:
        """Run one BLE operation, record its outcome and raise HomeAssistantError on failure."""
        record: dict[str, Any] = {"ok": False, "code": None, "message": None, **requested}
        try:
            async with self._lock:
                result = await operation(self._ble_device())
        except PCSDeviceError as err:
            record.update(code=err.code, message=err.message or str(err))
            raise HomeAssistantError(f"APstorage PCS rejected the change: {err}") from err
        except PCSError as err:
            record.update(code="error", message=str(err))
            raise HomeAssistantError(f"APstorage PCS did not respond: {err}") from err
        else:
            record.update(ok=True, code=200, message="ok")
            return result
        finally:
            record["at"] = _now_iso()
            setattr(self, record_attr, record)
            self.async_update_listeners()

    async def _async_update_sysmode(
        self,
        record_attr: str,
        requested: dict[str, Any],
        mutate: Callable[[dict[str, Any]], None],
    ) -> None:
        """Change getsysmode settings and apply the read-back to the data."""
        sysmode = await self._async_run(
            record_attr,
            requested,
            lambda ble_device: self._client.async_update_sysmode(ble_device, mutate),
        )
        if self.data is not None:
            self.data.apply_sysmode(sysmode)
            self.async_update_listeners()

    async def async_set_system_mode(self, mode: int) -> None:
        """Set the operating mode (0..6)."""
        if mode < 0 or mode > 6:
            raise HomeAssistantError(f"Invalid system mode {mode}; use 0..6")

        def mutate(payload: dict[str, Any]) -> None:
            payload["mode"] = str(mode)
            for key, value in _SYSMODE_DEFAULTS.items():
                payload.setdefault(key, value)

        await self._async_update_sysmode(
            "_last_system_mode_write", {"requested_mode": str(mode)}, mutate
        )

    async def async_set_backup_soc(self, backup_soc: int) -> None:
        """Set the backup SoC (10..90 %); the PCS allows it in mode 1 and 3."""
        if backup_soc < 10 or backup_soc > 90:
            raise HomeAssistantError(f"Invalid backup SOC {backup_soc}; use 10..90")

        def mutate(payload: dict[str, Any]) -> None:
            if normalize_mode_code(payload.get("mode")) not in {"1", "3"}:
                raise PCSDeviceError(
                    "setsysmode", "not_applicable", "backup SOC can only be changed in mode 1 or 3"
                )
            payload["backupSOC"] = str(int(backup_soc))
            for key, value in _SYSMODE_DEFAULTS.items():
                payload.setdefault(key, value)

        await self._async_update_sysmode(
            "_last_backup_soc_write", {"requested_backup_soc": int(backup_soc)}, mutate
        )

    async def async_set_selling_first(self, enabled: bool) -> None:
        """Turn selling-first on or off."""

        def mutate(payload: dict[str, Any]) -> None:
            payload["sellingFirst"] = "1" if enabled else "0"

        await self._async_update_sysmode(
            "_last_selling_first_write", {"requested_selling_first": bool(enabled)}, mutate
        )

    async def async_set_valley_charge(self, enabled: bool) -> None:
        """Turn grid charging during valley hours on or off."""

        def mutate(payload: dict[str, Any]) -> None:
            payload["valleycharge"] = "1" if enabled else "0"

        await self._async_update_sysmode(
            "_last_valley_charge_write", {"requested_valley_charge": bool(enabled)}, mutate
        )

    async def async_set_peak_power(self, peak_power: int) -> None:
        """Set the peak-shaving limit (100..50000 W); only in mode 5."""
        if peak_power < 100 or peak_power > 50000:
            raise HomeAssistantError(f"Invalid peak power {peak_power}; use 100..50000 W")

        def mutate(payload: dict[str, Any]) -> None:
            if normalize_mode_code(payload.get("mode")) != "5":
                raise PCSDeviceError(
                    "setsysmode",
                    "not_applicable",
                    "peak power can only be changed in mode 5 (Peak-Shaving)",
                )
            payload["peakPower"] = str(int(peak_power))

        await self._async_update_sysmode(
            "_last_peak_power_write", {"requested_peak_power": int(peak_power)}, mutate
        )

    async def async_set_peak_valley_schedule(
        self, *, peak_time: list[str], valley_time: list[str]
    ) -> None:
        """Switch to mode 0 with the given peak (discharge) and valley (charge) windows."""

        def mutate(payload: dict[str, Any]) -> None:
            payload["mode"] = "0"
            payload["peakTime"] = list(peak_time) if peak_time else None
            payload["valleyTime"] = list(valley_time) if valley_time else None
            payload["schedule"] = None
            for key, value in _SYSMODE_DEFAULTS.items():
                payload.setdefault(key, value)

        await self._async_update_sysmode(
            "_last_peak_valley_schedule_write",
            {"requested_peak_time": list(peak_time), "requested_valley_time": list(valley_time)},
            mutate,
        )

    async def async_set_advanced_schedule(
        self,
        *,
        peak_time: list[str],
        valley_time: list[str],
        schedule: list[Any] | None = None,
    ) -> None:
        """Switch to mode 3 with peak/valley windows or a structured schedule."""
        schedule_items = list(schedule or [])

        def mutate(payload: dict[str, Any]) -> None:
            payload["mode"] = "3"
            if schedule_items:
                payload["peakTime"] = None
                payload["valleyTime"] = None
                payload["schedule"] = schedule_items
            else:
                payload["peakTime"] = list(peak_time)
                payload["valleyTime"] = list(valley_time)
                payload["schedule"] = None
            for key, value in _SYSMODE_DEFAULTS.items():
                payload.setdefault(key, value)

        await self._async_update_sysmode(
            "_last_advanced_schedule_write",
            {
                "requested_peak_time": list(peak_time),
                "requested_valley_time": list(valley_time),
                "requested_schedule": schedule_items,
            },
            mutate,
        )

    async def async_set_buzzer_mode(self, mode: int) -> None:
        """Set the buzzer mode (0 = Silent, 1 = Normal)."""
        if mode not in (0, 1):
            raise HomeAssistantError(f"Invalid buzzer mode {mode}; use 0 or 1")
        # The app sends `status`; `mode` is kept for firmware that used it.
        await self._async_run(
            "_last_buzzer_mode_write",
            {"requested_buzzer_mode": str(mode)},
            lambda ble_device: self._client.async_command(
                ble_device, "set/buzzerMode", {"status": str(mode), "mode": str(mode)}
            ),
        )
        if self.data is not None:
            self.data.buzzer = mode
            self.async_update_listeners()

    async def async_clear_buzzer(self) -> None:
        """Silence an active buzzer alarm."""
        await self._async_run(
            "_last_clear_buzzer_write",
            {},
            lambda ble_device: self._client.async_command(ble_device, "setClearBuzzer"),
        )

    async def async_reboot_pcs(self) -> None:
        """Restart the PCS; it may drop the connection before answering."""

        async def reboot(ble_device: Any) -> None:
            try:
                await self._client.async_command(ble_device, "set/pcsReboot")
            except PCSDeviceError:
                raise
            except PCSError as err:
                _LOGGER.debug("No reply to set/pcsReboot (PCS restarting?): %s", err)

        await self._async_run("_last_pcs_reboot_write", {}, reboot)
        if self._reboot_refresh is not None:
            self._reboot_refresh.cancel()
        self._reboot_refresh = self.hass.loop.call_later(
            REBOOT_REFRESH_DELAY_SECONDS,
            lambda: self.hass.async_create_task(self.async_request_refresh()),
        )

    # ------------------------------------------------------------ diagnostic reads

    async def async_read_system_mode_payload(self) -> dict[str, Any]:
        """Read getsysmode and return it as {ok, code, message, storage_id, payload}."""
        result: dict[str, Any] = {
            "ok": False,
            "code": None,
            "message": None,
            "storage_id": self._client.device_id,
            "payload": None,
        }
        try:
            async with self._lock:
                payload = await self._client.async_get(self._ble_device(), "getsysmode")
        except PCSDeviceError as err:
            result.update(code=err.code, message=err.message or str(err))
        except PCSError as err:
            result.update(code="error", message=str(err))
        else:
            result.update(ok=True, code=200, message="ok", payload=payload)
            if self.data is not None:
                self.data.apply_sysmode(payload)
        result["at"] = _now_iso()
        self._last_system_mode_payload_read = result
        self.async_update_listeners()
        return result

    async def async_probe_version_once(self) -> dict[str, Any]:
        """Read deviceInfo now and return the firmware versions."""
        result: dict[str, Any] = {"ok": False, "message": None, "versions": {}}
        try:
            async with self._lock:
                info = await self._client.async_get(self._ble_device(), "deviceInfo")
        except PCSError as err:
            result["message"] = str(err)
        else:
            versions = {
                key: str(info[key])
                for key in ("dcmVer", "moduleVer1", "moduleVer2", "moduleVer3")
                if info.get(key)
            }
            result.update(ok=True, message="ok", versions=versions)
            if self.data is not None:
                self.data.apply_device_info(info)
            self._device_info_read_at = time.monotonic()
        result["at"] = _now_iso()
        self._last_version_probe_read = result
        self.async_update_listeners()
        return result

    # ------------------------------------------------------------ last results

    @property
    def last_system_mode_write(self) -> dict[str, Any] | None:
        return self._last_system_mode_write

    @property
    def last_backup_soc_write(self) -> dict[str, Any] | None:
        return self._last_backup_soc_write

    @property
    def last_system_mode_payload_read(self) -> dict[str, Any] | None:
        return self._last_system_mode_payload_read

    @property
    def last_version_probe_read(self) -> dict[str, Any] | None:
        return self._last_version_probe_read

    @property
    def last_advanced_schedule_write(self) -> dict[str, Any] | None:
        return self._last_advanced_schedule_write

    @property
    def last_peak_valley_schedule_write(self) -> dict[str, Any] | None:
        return self._last_peak_valley_schedule_write

    @property
    def last_buzzer_mode_write(self) -> dict[str, Any] | None:
        return self._last_buzzer_mode_write

    @property
    def last_clear_buzzer_write(self) -> dict[str, Any] | None:
        return self._last_clear_buzzer_write

    @property
    def last_pcs_reboot_write(self) -> dict[str, Any] | None:
        return self._last_pcs_reboot_write

    @property
    def last_selling_first_write(self) -> dict[str, Any] | None:
        return self._last_selling_first_write

    @property
    def last_valley_charge_write(self) -> dict[str, Any] | None:
        return self._last_valley_charge_write

    @property
    def last_peak_power_write(self) -> dict[str, Any] | None:
        return self._last_peak_power_write
