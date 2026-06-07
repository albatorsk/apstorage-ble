"""DataUpdateCoordinator for the APstorage BLE integration.

Uses HA's ActiveBluetoothDataUpdateCoordinator so that:
  - Bluetooth advertisements from the PCS are tracked to know when the device
    is reachable (including via the ESPHome Bluetooth proxy).
    - A GATT poll is triggered at most once per configured polling interval.
  - The device is marked as unavailable automatically when advertisements stop.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace as dataclass_replace
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import logging
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.components.bluetooth.active_update_coordinator import (
    ActiveBluetoothDataUpdateCoordinator,
)
from homeassistant.core import CoreState, HomeAssistant, callback

from .const import DOMAIN
from .models import PCSData
from .soc_client import APstorageSocClient

_LOGGER = logging.getLogger(__name__)

# Hard wall-clock cap for a single poll while holding the coordinator lock.
# This prevents a wedged BLE operation from blocking all future polls until
# Home Assistant is restarted.  Must be longer than the natural failure path
# (2 × RESPONSE_TIMEOUT_SECONDS + connection overhead ≈ 75 s).
POLL_WATCHDOG_TIMEOUT_SECONDS = 120

SHUTDOWN_WAIT_SECONDS = 10
DEFAULT_PERSISTENT_SESSION_ENABLED = False
STARTUP_VERSION_PROBE_ENABLED = True
STARTUP_VERSION_PROBE_RETRIES = 3
STARTUP_VERSION_PROBE_RETRY_DELAY_SECONDS = 2
DEFERRED_VERSION_PROBE_ENABLED = False
VERSION_RETRY_INTERVAL_SECONDS = 300
POLL_FAILURE_RECONNECT_THRESHOLD = 3
NO_DEVICE_STRONG_RESET_THRESHOLD = 6
NO_DEVICE_STRONG_RESET_COOLDOWN_SECONDS = 300
NO_DEVICE_FALLBACK_PROBE_INTERVAL_SECONDS = 90
ONE_SHOT_FALLBACK_MAX_RETRIES = 2


class APstorageCoordinator(ActiveBluetoothDataUpdateCoordinator[PCSData | None]):
    """Coordinator that polls the APstorage ELT-12 via BLE on advertisement."""

    def __init__(
        self,
        hass: HomeAssistant,
        logger: logging.Logger,
        address: str,
        name: str,
        poll_interval_seconds: int,
    ) -> None:
        """Initialise the coordinator.

        Args:
            hass:    The HA instance.
            logger:  Logger for this coordinator.
            address: BLE MAC address of the PCS (upper-case, colon-separated).
            name:    Human-readable name of the device.
        """
        super().__init__(
            hass=hass,
            logger=logger,
            address=address,
            needs_poll_method=self._needs_poll,
            poll_method=self._async_poll,
            mode=bluetooth.BluetoothScanningMode.ACTIVE,
            connectable=True,
        )
        self._address = address
        self._name = name
        self._poll_interval_seconds = poll_interval_seconds
        self._soc_client = APstorageSocClient()
        self._poll_lock = asyncio.Lock()
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
        self._last_listener_fingerprint: tuple[Any, ...] | None = None
        # Track write timestamps to avoid poll overwriting recent writes (5 second grace period)
        self._field_write_timestamps: dict[str, datetime] = {}
        self._consecutive_poll_failures = 0
        self._shutdown = False
        self._write_pending = False
        self._active_poll_task: asyncio.Task[Any] | None = None
        self._post_write_refresh_task: asyncio.Task[Any] | None = None
        self._startup_version_task: asyncio.Task[Any] | None = None
        self._startup_version_fetch_attempted = False
        self._deferred_version_probe_attempted = False
        self._last_version_retry_at: datetime | None = None
        self._last_successful_poll_at: datetime | None = None
        self._last_poll_used_cached_data: bool | None = None
        self._consecutive_no_device_polls = 0
        self._last_no_device_probe_at: datetime | None = None
        self._last_no_device_strong_reset_at: datetime | None = None
        # Persistent sessions improve latency when stable, but shared proxy
        # environments can invalidate long-lived connections unpredictably.
        # Keep persistent mode enabled by default and reconnect the session
        # when needed instead of permanently falling back to one-shot polling.
        self._persistent_session_enabled = DEFAULT_PERSISTENT_SESSION_ENABLED
        # Most-recent successfully parsed data; also exposed as coordinator.data
        self.data: PCSData | None = None

    @property
    def runtime_available(self) -> bool:
        """Return entity availability using either live advertisements or recent data.

        Some proxy stacks can stop advertisement flow while still allowing GATT
        connects and successful polls. In those cases, `self.available` may flip
        false even though data is still being updated. Keep entities available
        while we have a recent successful poll.
        """
        if self._soc_client.session_open:
            return True

        if self.available:
            return True

        if self._last_successful_poll_at is None:
            return False

        grace_seconds = max(self._poll_interval_seconds * 8, 900)
        age = datetime.now(timezone.utc) - self._last_successful_poll_at
        return age <= timedelta(seconds=grace_seconds)

    @property
    def ble_connection_mode(self) -> str:
        """Return the current BLE polling/connection mode label.

        Persistent mode means a live session is currently open and reused.
        Otherwise, polling falls back to one-shot connect/query/disconnect.
        """
        if self._persistent_session_enabled and self._soc_client.session_open:
            return "Persistent"
        return "One-shot"

    @property
    def entity_values_source(self) -> str:
        """Return whether entity values are live or cached.

        - Unknown: no successful telemetry poll yet.
        - Live: the most recent poll received fresh telemetry.
        - Cached: latest entity values come from prior successful telemetry.
        """
        if self._last_successful_poll_at is None:
            return "Unknown"
        if self._last_poll_used_cached_data:
            return "Cached"
        return "Live"

    def _freeze_state_value(self, value: Any) -> Any:
        """Convert nested mutable values to immutable values for comparisons."""
        if isinstance(value, dict):
            return tuple(
                (str(key), self._freeze_state_value(item))
                for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))
            )
        if isinstance(value, (list, tuple)):
            return tuple(self._freeze_state_value(item) for item in value)
        if isinstance(value, set):
            return tuple(sorted(self._freeze_state_value(item) for item in value))
        return value

    def _data_state_fingerprint(self) -> tuple[tuple[str, Any], ...]:
        """Return an immutable snapshot of decoded coordinator data."""
        if self.data is None:
            return ()

        return tuple(
            (name, self._freeze_state_value(value))
            for name, value in sorted(vars(self.data).items(), key=lambda item: item[0])
        )

    def _listener_state_fingerprint(self) -> tuple[Any, ...]:
        """Build a fingerprint containing all fields surfaced by entities."""
        return (
            self._data_state_fingerprint(),
            self.runtime_available,
            self.ble_connection_mode,
            self.entity_values_source,
            self._freeze_state_value(self._last_system_mode_payload_read),
            self._freeze_state_value(self._last_system_mode_write),
            self._freeze_state_value(self._last_backup_soc_write),
            self._freeze_state_value(self._last_advanced_schedule_write),
            self._freeze_state_value(self._last_peak_valley_schedule_write),
            self._freeze_state_value(self._last_buzzer_mode_write),
            self._freeze_state_value(self._last_clear_buzzer_write),
            self._freeze_state_value(self._last_pcs_reboot_write),
            self._freeze_state_value(self._last_selling_first_write),
            self._freeze_state_value(self._last_valley_charge_write),
            self._freeze_state_value(self._last_peak_power_write),
        )

    def _notify_if_state_changed(self) -> None:
        """Notify listeners only when entity-facing state changes."""
        fingerprint = self._listener_state_fingerprint()
        if fingerprint == self._last_listener_fingerprint:
            return

        self._last_listener_fingerprint = fingerprint
        self.async_update_listeners()

    async def _async_run_post_write_refresh(self) -> None:
        """Refresh coordinator state after a write without blocking the caller."""
        try:
            await self._async_poll()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("[%s] Post-write refresh failed: %s: %s", self._name, type(err).__name__, err)
        finally:
            task = asyncio.current_task()
            if self._post_write_refresh_task is task:
                self._post_write_refresh_task = None

    @callback
    def _schedule_post_write_refresh(self) -> None:
        """Queue a follow-up poll after a write, unless one is already pending."""
        if self._shutdown:
            return

        task = self._post_write_refresh_task
        if task is not None and not task.done():
            return

        self._post_write_refresh_task = self.hass.async_create_task(
            self._async_run_post_write_refresh()
        )

    async def _async_prepare_for_write(self) -> None:
        """Give writes priority over queued or active polls."""
        self._write_pending = True

        refresh_task = self._post_write_refresh_task
        if refresh_task is not None and not refresh_task.done():
            refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await refresh_task

        task = self._active_poll_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            _LOGGER.debug("[%s] Cancelling in-flight poll before write", self._name)
            task.cancel()
            with suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=SHUTDOWN_WAIT_SECONDS)

    async def async_initialize(self) -> None:
        """Schedule one-time startup version discovery outside the poll path."""
        if not STARTUP_VERSION_PROBE_ENABLED:
            return
        if self._startup_version_task is None:
            self._startup_version_task = self.hass.async_create_task(
                self._async_fetch_startup_version_info()
            )

    async def async_shutdown(self) -> None:
        """Block new BLE activity once the config entry is unloading."""
        self._shutdown = True
        startup_task = self._startup_version_task
        if startup_task is not None and not startup_task.done():
            startup_task.cancel()
            with suppress(asyncio.CancelledError):
                await startup_task
        refresh_task = self._post_write_refresh_task
        if refresh_task is not None and not refresh_task.done():
            refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await refresh_task
        task = self._active_poll_task
        if task is not None and not task.done():
            _LOGGER.debug("[%s] Cancelling in-flight poll during shutdown", self._name)
            task.cancel()
            with suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(task, timeout=SHUTDOWN_WAIT_SECONDS)
        async with self._poll_lock:
            await self._soc_client.async_close_session()
            self._last_service_info = None

    def _resolve_battery_flow_state(self, metrics) -> str | None:
        """Resolve user-facing battery flow state from live telemetry.

        Priority:
          1. Battery charging power (P1) vs discharging power (P0)
          2. Instantaneous battery current sign convention
          3. Parsed text state fallback from query metrics
        """
        p0 = float(metrics.battery_power) if metrics.battery_power is not None else None
        p1 = float(metrics.battery_charging_power) if getattr(metrics, "battery_charging_power", None) is not None else None

        if p0 is not None and p1 is not None:
            if p1 >= 5.0:
                return "Charging"
            if p0 >= 5.0:
                return "Discharging"
            return "Holding"

        if p1 is not None:
            return "Charging" if p1 >= 5.0 else "Holding"

        if p0 is not None and abs(p0) >= 5.0:
            return "Discharging" if p0 >= 0 else "Charging"

        if metrics.battery_current is not None and abs(float(metrics.battery_current)) >= 0.05:
            return "Discharging" if float(metrics.battery_current) >= 0 else "Charging"

        if p0 is not None or metrics.battery_current is not None:
            return "Holding"

        if getattr(metrics, "battery_flow_state", None) is not None:
            flow_text = str(metrics.battery_flow_state).strip().lower()
            if flow_text.startswith("discharg"):
                return "Discharging"
            if flow_text.startswith("charg"):
                return "Charging"
            if flow_text.startswith("hold") or flow_text.startswith("stand"):
                return "Holding"

        return None

    def _is_field_recently_written(self, field_name: str, grace_period_seconds: float = 5.0) -> bool:
        """Check if a field was written recently (within grace period)."""
        if field_name not in self._field_write_timestamps:
            return False
        elapsed = (datetime.now(timezone.utc) - self._field_write_timestamps[field_name]).total_seconds()
        return elapsed < grace_period_seconds

    def _resolve_ble_device(self) -> Any | None:
        """Resolve the best currently connectable BLE device for this coordinator."""
        service_info: BluetoothServiceInfoBleak | None = self._last_service_info

        if service_info is not None and service_info.connectable:
            return service_info.device

        if service_info is not None:
            return bluetooth.async_ble_device_from_address(
                self.hass,
                service_info.device.address,
                connectable=True,
            )

        return bluetooth.async_ble_device_from_address(
            self.hass,
            self._address,
            connectable=True,
        )

    def _should_run_no_device_probe(self, now: datetime) -> bool:
        """Return True when enough time elapsed to probe a non-connectable fallback."""
        if self._last_no_device_probe_at is None:
            return True
        elapsed = (now - self._last_no_device_probe_at).total_seconds()
        return elapsed >= NO_DEVICE_FALLBACK_PROBE_INTERVAL_SECONDS

    async def _async_handle_no_device_detected(self, now: datetime) -> Any | None:
        """Apply staged recovery when no connectable BLE device is available.

        1. Track no-device streak and stale SoC masking.
        2. Periodically force-close any stale session state.
        3. Probe a non-connectable cached BLEDevice as a last-resort target.
        """
        self._consecutive_no_device_polls += 1
        self._consecutive_poll_failures += 1

        should_strong_reset = (
            self._consecutive_no_device_polls >= NO_DEVICE_STRONG_RESET_THRESHOLD
            and (
                self._last_no_device_strong_reset_at is None
                or (now - self._last_no_device_strong_reset_at).total_seconds()
                >= NO_DEVICE_STRONG_RESET_COOLDOWN_SECONDS
            )
        )
        if should_strong_reset:
            _LOGGER.warning(
                "[%s] No connectable BLE device for %d polls; forcing strong BLE session reset",
                self._name,
                self._consecutive_no_device_polls,
            )
            await self._soc_client.async_close_session()
            self._last_service_info = None
            self._last_no_device_strong_reset_at = now

        if not self._should_run_no_device_probe(now):
            _LOGGER.warning("[%s] No connectable BLE device found - skipping poll", self._name)
            return None

        self._last_no_device_probe_at = now
        fallback = bluetooth.async_ble_device_from_address(
            self.hass,
            self._address,
            connectable=False,
        )
        if fallback is None:
            _LOGGER.warning("[%s] No connectable BLE device found - skipping poll", self._name)
            return None

        _LOGGER.warning(
            "[%s] Using fallback BLE cache entry for %s after no-device streak=%d",
            self._name,
            self._address,
            self._consecutive_no_device_polls,
        )
        return fallback

    def _apply_version_info(self, version_info: dict[str, str]) -> None:
        """Merge one-time version info into coordinator data."""
        if self.data is None:
            self.data = PCSData()

        if version_info.get("pcs_firmware_version") is not None:
            self.data.pcs_firmware_version = version_info["pcs_firmware_version"]
        if version_info.get("pcs_latest_firmware_version") is not None:
            self.data.pcs_latest_firmware_version = version_info["pcs_latest_firmware_version"]
        if version_info.get("pcs_software_version") is not None:
            self.data.pcs_software_version = version_info["pcs_software_version"]
        if version_info.get("pcs_hardware_version") is not None:
            self.data.pcs_hardware_version = version_info["pcs_hardware_version"]

    def _version_info_missing(self) -> bool:
        """Return True while any core version field is still missing."""
        if self.data is None:
            return True

        return any(
            not value
            for value in (
                self.data.pcs_firmware_version,
                self.data.pcs_latest_firmware_version,
                self.data.pcs_hardware_version,
            )
        )

    async def _async_fetch_startup_version_info(self) -> None:
        """Fetch firmware version once via a dedicated one-shot BLE connection.

        This runs before the persistent telemetry session is opened so that
        both queries use their own clean Blufi state machine.  The one-shot
        connection disconnects fully before the persistent session opens,
        so the PCS never sees two simultaneous connections.
        """
        if self._shutdown or self._startup_version_fetch_attempted:
            return

        self._startup_version_fetch_attempted = True

        async with self._poll_lock:
            for attempt in range(1, STARTUP_VERSION_PROBE_RETRIES + 1):
                if self._shutdown:
                    return

                ble_device = self._resolve_ble_device()
                if ble_device is None:
                    _LOGGER.debug(
                        "[%s] Startup version fetch attempt %d/%d skipped; no connectable BLE device",
                        self._name,
                        attempt,
                        STARTUP_VERSION_PROBE_RETRIES,
                    )
                else:
                    _LOGGER.debug(
                        "[%s] Fetching version info via dedicated one-shot connection (attempt %d/%d)",
                        self._name,
                        attempt,
                        STARTUP_VERSION_PROBE_RETRIES,
                    )
                    version_info: dict[str, str] = {}
                    try:
                        async with asyncio.timeout(POLL_WATCHDOG_TIMEOUT_SECONDS):
                            # One-shot: connects, queries, disconnects — no shared state with
                            # the persistent telemetry session that opens on the first poll.
                            version_info = await self._soc_client.async_query_version_info_once(
                                ble_device,
                                device_name_hint=self._name,
                            )
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug(
                            "[%s] Startup version fetch attempt %d/%d failed (non-fatal): %s: %s",
                            self._name,
                            attempt,
                            STARTUP_VERSION_PROBE_RETRIES,
                            type(err).__name__,
                            err,
                        )
                    else:
                        if version_info:
                            self._apply_version_info(version_info)
                            self._notify_if_state_changed()
                            _LOGGER.debug(
                                "[%s] Startup version info fetched on attempt %d/%d: %s",
                                self._name,
                                attempt,
                                STARTUP_VERSION_PROBE_RETRIES,
                                version_info,
                            )
                            return

                if attempt < STARTUP_VERSION_PROBE_RETRIES:
                    await asyncio.sleep(STARTUP_VERSION_PROBE_RETRY_DELAY_SECONDS)

            _LOGGER.debug(
                "[%s] Startup version fetch returned no data after %d attempts; version entities will remain Unknown",
                self._name,
                STARTUP_VERSION_PROBE_RETRIES,
            )

    # ------------------------------------------------------------------
    # ActiveBluetoothDataUpdateCoordinator callbacks
    # ------------------------------------------------------------------

    @callback
    def _needs_poll(
        self,
        service_info: BluetoothServiceInfoBleak,
        seconds_since_last_poll: float | None,
    ) -> bool:
        """Return True when GATT polling should be triggered.

        Conditions:
          1. HA is fully running (avoid polls during startup).
          2. Enough time has elapsed since the last poll.
          3. We have a connectable BLE device to use (may route through proxy).
        """
        if self._shutdown:
            return False

        if self.hass.state != CoreState.running:
            return False

        if self._write_pending:
            return False

        # Do not schedule another advertisement-triggered poll while one is
        # already running; this avoids repeated queued/skip churn.
        if self._poll_lock.locked():
            return False

        if (
            seconds_since_last_poll is not None
            and seconds_since_last_poll < self._poll_interval_seconds
        ):
            return False

        # Confirm a connectable device (local adapter or ESPHome proxy) is
        # available for the PCS's MAC address.
        return bool(
            bluetooth.async_ble_device_from_address(
                self.hass,
                service_info.device.address,
                connectable=True,
            )
        )

    async def _async_poll(self) -> None:
        """Connect to the device via GATT and update coordinator data."""
        if self._shutdown:
            _LOGGER.debug("[%s] Poll skipped because coordinator is shutting down", self._name)
            return

        if self._write_pending:
            _LOGGER.debug("[%s] Poll skipped because a write is pending", self._name)
            return

        # Skip this poll if another poll is already running.  Multiple
        # advertisement callbacks can fire in quick succession; without this
        # guard they all queue behind _poll_lock and run sequentially, blocking
        # Home Assistant's bootstrap phase for several minutes.
        if self._poll_lock.locked():
            _LOGGER.debug("[%s] Poll skipped — another poll is already in progress", self._name)
            return

        async with self._poll_lock:
            self._active_poll_task = asyncio.current_task()
            if self._shutdown:
                _LOGGER.debug("[%s] Poll aborted because coordinator is shutting down", self._name)
                self._active_poll_task = None
                return
            try:
                ble_device = self._resolve_ble_device()
                now = datetime.now(timezone.utc)
                force_one_shot = False

                if ble_device is None:
                    ble_device = await self._async_handle_no_device_detected(now)
                    if ble_device is None:
                        self._last_poll_used_cached_data = True
                        self._notify_if_state_changed()
                        return
                    force_one_shot = True
                elif self._consecutive_no_device_polls:
                    _LOGGER.debug(
                        "[%s] BLE device rediscovered after %d no-device polls",
                        self._name,
                        self._consecutive_no_device_polls,
                    )
                    self._consecutive_no_device_polls = 0

                # Start from the previous snapshot so transient query failures
                # do not force all entities to Unknown.
                previous = self.data
                _LOGGER.debug("[%s] Starting metrics poll for %s", self._name, ble_device.address)

                try:
                    async with asyncio.timeout(POLL_WATCHDOG_TIMEOUT_SECONDS):
                        if self._persistent_session_enabled and not force_one_shot:
                            persistent_failed = False
                            try:
                                # Open a persistent BLE session if not already connected.
                                # The DH handshake is only performed on (re)connect, not every poll.
                                if not self._soc_client.session_open:
                                    _LOGGER.debug(
                                        "[%s] Opening persistent BLE session to %s",
                                        self._name,
                                        ble_device.address,
                                    )
                                    await self._soc_client.async_open_session(
                                        ble_device, device_name_hint=self._name
                                    )
                                    _LOGGER.debug("[%s] BLE session established", self._name)

                                metrics = await self._soc_client.async_query_session()
                            except Exception as session_err:  # noqa: BLE001
                                _LOGGER.warning(
                                    "[%s] Persistent session failed (%s: %s); closing session and falling back to one-shot",
                                    self._name,
                                    type(session_err).__name__,
                                    session_err,
                                )
                                await self._soc_client.async_close_session()
                                persistent_failed = True
                                metrics = None

                            # Mirror the standalone script behavior: if a persistent
                            # session breaks, immediately retry via a fresh one-shot
                            # connect/query path within the same poll cycle.
                            if metrics is None and persistent_failed:
                                _LOGGER.debug(
                                    "[%s] Trying one-shot fallback after persistent failure",
                                    self._name,
                                )
                                if self._soc_client.session_open:
                                    _LOGGER.debug(
                                        "[%s] Closing persistent session before one-shot fallback",
                                        self._name,
                                    )
                                    await self._soc_client.async_close_session()
                                metrics = await self._soc_client.async_query_metrics(
                                    ble_device,
                                    device_name_hint=self._name,
                                    max_retries=ONE_SHOT_FALLBACK_MAX_RETRIES,
                                )

                            # Persistent session can remain connected yet return an
                            # empty payload intermittently; recover with one-shot.
                            if metrics is None and not persistent_failed:
                                _LOGGER.debug(
                                    "[%s] Persistent session returned empty metrics; trying one-shot fallback",
                                    self._name,
                                )
                                if self._soc_client.session_open:
                                    _LOGGER.debug(
                                        "[%s] Closing persistent session before empty-metrics fallback",
                                        self._name,
                                    )
                                    await self._soc_client.async_close_session()
                                metrics = await self._soc_client.async_query_metrics(
                                    ble_device,
                                    device_name_hint=self._name,
                                    max_retries=ONE_SHOT_FALLBACK_MAX_RETRIES,
                                )
                        else:
                            if self._soc_client.session_open:
                                _LOGGER.debug(
                                    "[%s] Closing persistent session before one-shot polling path",
                                    self._name,
                                )
                                await self._soc_client.async_close_session()
                            metrics = await self._soc_client.async_query_metrics(
                                ble_device,
                                device_name_hint=self._name,
                                max_retries=2,
                            )
                except TimeoutError:
                    self._consecutive_poll_failures += 1
                    self._last_poll_used_cached_data = True
                    _LOGGER.warning(
                        "[%s] Poll watchdog timed out after %ss; closing BLE session",
                        self._name,
                        POLL_WATCHDOG_TIMEOUT_SECONDS,
                    )
                    await self._soc_client.async_close_session()
                    self._last_service_info = None
                    self._notify_if_state_changed()
                    return
                except Exception as err:  # noqa: BLE001
                    self._consecutive_poll_failures += 1
                    self._last_poll_used_cached_data = True
                    _LOGGER.warning(
                        "[%s] Poll failed with %s: %s; closing BLE session",
                        self._name,
                        type(err).__name__,
                        err,
                    )
                    await self._soc_client.async_close_session()
                    self._last_service_info = None
                    self._notify_if_state_changed()
                    return

                if metrics is None:
                    self._consecutive_poll_failures += 1
                    self._last_poll_used_cached_data = True
                    _LOGGER.info("[%s] SoC query returned no metrics", self._name)
                    if self._consecutive_poll_failures >= POLL_FAILURE_RECONNECT_THRESHOLD:
                        _LOGGER.warning(
                            "[%s] Poll failed %d times in a row; closing BLE session to force reconnect",
                            self._name,
                            self._consecutive_poll_failures,
                        )
                        await self._soc_client.async_close_session()
                        self._last_service_info = None
                else:
                    self.data = dataclass_replace(previous) if previous is not None else PCSData()
                    self._consecutive_no_device_polls = 0
                    self._last_poll_used_cached_data = False
                    if self._consecutive_poll_failures:
                        _LOGGER.debug(
                            "[%s] Poll recovered after %d consecutive failures",
                            self._name,
                            self._consecutive_poll_failures,
                        )
                    self._consecutive_poll_failures = 0
                    self._last_successful_poll_at = datetime.now(timezone.utc)
                    _LOGGER.debug(
                        "[%s] Received metrics: soc=%s, state=%s, flow=%s",
                        self._name,
                        metrics.battery_soc,
                        metrics.system_state,
                        metrics.battery_flow_state,
                    )
                    if metrics.battery_soc is not None:
                        self.data.battery_soc = float(metrics.battery_soc)
                        _LOGGER.debug("[%s] Battery SoC: %.1f%%", self._name, self.data.battery_soc)
                    if metrics.battery_voltage is not None:
                        self.data.battery_voltage = float(metrics.battery_voltage)
                    if metrics.battery_current is not None:
                        self.data.battery_current = float(metrics.battery_current)
                    if metrics.battery_power is not None:
                        self.data.battery_power = float(metrics.battery_power)
                        _LOGGER.debug("[%s] Battery Power: %.1f W", self._name, self.data.battery_power)
                    if metrics.battery_charging_power is not None:
                        self.data.battery_charging_power = float(metrics.battery_charging_power)
                    if metrics.battery_temperature is not None:
                        self.data.battery_temperature = float(metrics.battery_temperature)
                    if metrics.system_state is not None:
                        self.data.system_state = metrics.system_state
                        _LOGGER.debug("[%s] System state: %s", self._name, metrics.system_state)
                    if metrics.system_mode is not None and not self._is_field_recently_written("system_mode"):
                        self.data.system_mode = metrics.system_mode
                    if metrics.backup_soc is not None and not self._is_field_recently_written("backup_soc"):
                        self.data.backup_soc = float(metrics.backup_soc)
                    resolved_flow_state = self._resolve_battery_flow_state(metrics)
                    if resolved_flow_state is not None:
                        self.data.battery_flow_state = resolved_flow_state
                    if metrics.grid_voltage is not None:
                        self.data.grid_voltage = float(metrics.grid_voltage)
                    if metrics.grid_current is not None:
                        self.data.grid_current = float(metrics.grid_current)
                    if metrics.grid_power is not None:
                        self.data.grid_power = float(metrics.grid_power)
                    if metrics.grid_frequency is not None:
                        self.data.grid_frequency = float(metrics.grid_frequency)
                    if metrics.pv_voltage is not None:
                        self.data.pv_voltage = float(metrics.pv_voltage)
                    if metrics.pv_current is not None:
                        self.data.pv_current = float(metrics.pv_current)
                    if metrics.pv_power is not None:
                        self.data.pv_power = float(metrics.pv_power)
                    if metrics.load_voltage is not None:
                        self.data.load_voltage = float(metrics.load_voltage)
                    if metrics.load_current is not None:
                        self.data.load_current = float(metrics.load_current)
                    if metrics.load_power is not None:
                        self.data.load_power = float(metrics.load_power)
                    if metrics.inverter_temperature is not None:
                        self.data.inverter_temperature = float(metrics.inverter_temperature)
                    if metrics.buzzer is not None and not self._is_field_recently_written("buzzer"):
                        self.data.buzzer = metrics.buzzer
                    if metrics.co2_reduction is not None:
                        self.data.co2_reduction = float(metrics.co2_reduction)
                    if metrics.total_produced is not None:
                        self.data.total_produced = float(metrics.total_produced)
                    if metrics.total_consumed is not None:
                        self.data.total_consumed = float(metrics.total_consumed)
                    if metrics.total_consumed_daily is not None:
                        self.data.total_consumed_daily = float(metrics.total_consumed_daily)
                    if metrics.pcs_firmware_version is not None:
                        self.data.pcs_firmware_version = metrics.pcs_firmware_version
                    if metrics.pcs_latest_firmware_version is not None:
                        self.data.pcs_latest_firmware_version = metrics.pcs_latest_firmware_version
                    if metrics.pcs_software_version is not None:
                        self.data.pcs_software_version = metrics.pcs_software_version
                    if metrics.pcs_hardware_version is not None:
                        self.data.pcs_hardware_version = metrics.pcs_hardware_version
                    if metrics.pv_energy_produced is not None:
                        self.data.pv_energy_produced = float(metrics.pv_energy_produced)
                    if metrics.battery_charged_energy is not None:
                        self.data.battery_charged_energy = float(metrics.battery_charged_energy)
                    if metrics.battery_discharged_energy is not None:
                        self.data.battery_discharged_energy = float(metrics.battery_discharged_energy)

                    # Attempt a deferred one-time version probe after first successful telemetry poll.
                    # This gives firmware version fields a second chance to populate if the startup
                    # probe failed (e.g., due to timing or BLE availability at boot).
                    if (
                        DEFERRED_VERSION_PROBE_ENABLED
                        and self._version_info_missing()
                        and not self._deferred_version_probe_attempted
                    ):
                        self._deferred_version_probe_attempted = True
                        _LOGGER.debug("[%s] Attempting deferred version probe after successful telemetry poll", self._name)
                        try:
                            ble_device = self._resolve_ble_device()
                            if ble_device is not None:
                                # Close the persistent session before starting the one-shot version probe
                                # to avoid Blufi state machine confusion from concurrent DH handshakes.
                                if self._soc_client.session_open:
                                    _LOGGER.debug("[%s] Closing persistent session before deferred version probe", self._name)
                                    await self._soc_client.async_close_session()
                                version_info = await self._soc_client.async_query_version_info_once(
                                    ble_device,
                                    device_name_hint=self._name,
                                )
                                if version_info:
                                    self._apply_version_info(version_info)
                                    _LOGGER.debug("[%s] Deferred version probe succeeded: %s", self._name, version_info)
                            else:
                                _LOGGER.debug("[%s] Deferred version probe skipped; no connectable BLE device", self._name)
                        except Exception as err:  # noqa: BLE001
                            _LOGGER.debug(
                                "[%s] Deferred version probe failed (non-fatal): %s: %s",
                                self._name,
                                type(err).__name__,
                                err,
                            )

                # Version polling is disabled; version entities will remain Unknown.

                # Push the update to all subscribed entities.
                self._notify_if_state_changed()
            finally:
                self._active_poll_task = None

    async def async_set_system_mode(self, mode: int) -> None:
        """Set storage system mode over BLE and refresh coordinator data.

        Mode values follow EMA app conventions:
          0 Peak-Valley, 1 Redundant, 2 Manual, 3 Mixed,
          4 Backup, 5 Peak-Shaving, 6 Intelligent.
        """
        if mode < 0 or mode > 6:
            raise ValueError(f"Invalid system mode: {mode}")

        await self._async_prepare_for_write()
        try:
            async with self._poll_lock:
                # Close the persistent poll session so the write's own BLE connection does not conflict.
                await self._soc_client.async_close_session()
                service_info: BluetoothServiceInfoBleak | None = self._last_service_info

                if service_info is not None and service_info.connectable:
                    ble_device = service_info.device
                elif service_info is not None:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        service_info.device.address,
                        connectable=True,
                    )
                else:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        self._address,
                        connectable=True,
                    )

                if ble_device is None:
                    raise RuntimeError("No connectable BLE device found for system mode write")

                _LOGGER.debug("[%s] Setting system mode to %s", self._name, mode)
                result = await self._soc_client.async_set_system_mode(
                    ble_device,
                    mode=mode,
                    device_name_hint=self._name,
                )
                self._last_system_mode_write = {
                    "ok": bool(result.get("ok", False)),
                    "code": result.get("code"),
                    "message": result.get("message"),
                    "requested_mode": str(mode),
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                if not bool(result.get("ok", False)):
                    raise RuntimeError(
                        "System mode write failed"
                        f" (code={result.get('code')}, message={result.get('message')})"
                    )

                if self.data is not None:
                    self.data.system_mode = str(mode)
                    self._field_write_timestamps["system_mode"] = datetime.now(timezone.utc)
                    self._notify_if_state_changed()
        finally:
            self._write_pending = False

        self._schedule_post_write_refresh()

    @property
    def last_system_mode_write(self) -> dict[str, Any] | None:
        """Return the most recent write attempt result for diagnostics."""
        return self._last_system_mode_write

    async def async_set_backup_soc(self, backup_soc: int) -> None:
        """Set backup SOC threshold over BLE and refresh coordinator data."""
        if backup_soc < 20 or backup_soc > 90:
            raise ValueError(f"Invalid backup SOC: {backup_soc}")

        await self._async_prepare_for_write()
        try:
            async with self._poll_lock:
                await self._soc_client.async_close_session()
                service_info: BluetoothServiceInfoBleak | None = self._last_service_info

                if service_info is not None and service_info.connectable:
                    ble_device = service_info.device
                elif service_info is not None:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        service_info.device.address,
                        connectable=True,
                    )
                else:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        self._address,
                        connectable=True,
                    )

                if ble_device is None:
                    raise RuntimeError("No connectable BLE device found for backup SOC write")

                _LOGGER.debug("[%s] Setting backup SOC to %s", self._name, backup_soc)
                result = await self._soc_client.async_set_backup_soc(
                    ble_device,
                    backup_soc=backup_soc,
                    device_name_hint=self._name,
                )
                self._last_backup_soc_write = {
                    "ok": bool(result.get("ok", False)),
                    "code": result.get("code"),
                    "message": result.get("message"),
                    "requested_backup_soc": str(backup_soc),
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                if not bool(result.get("ok", False)):
                    raise RuntimeError(
                        "Backup SOC write failed"
                        f" (code={result.get('code')}, message={result.get('message')})"
                    )

                if self.data is not None:
                    self.data.backup_soc = float(backup_soc)
                    self._field_write_timestamps["backup_soc"] = datetime.now(timezone.utc)
                    self._notify_if_state_changed()
        finally:
            self._write_pending = False

        self._schedule_post_write_refresh()

    @property
    def last_backup_soc_write(self) -> dict[str, Any] | None:
        """Return the most recent backup SOC write attempt for diagnostics."""
        return self._last_backup_soc_write

    async def async_read_system_mode_payload(self) -> dict[str, Any]:
        """Read current getsysmode payload over BLE for diagnostics/automation."""
        async with self._poll_lock:
            await self._soc_client.async_close_session()
            service_info: BluetoothServiceInfoBleak | None = self._last_service_info

            if service_info is not None and service_info.connectable:
                ble_device = service_info.device
            elif service_info is not None:
                ble_device = bluetooth.async_ble_device_from_address(
                    self.hass,
                    service_info.device.address,
                    connectable=True,
                )
            else:
                ble_device = bluetooth.async_ble_device_from_address(
                    self.hass,
                    self._address,
                    connectable=True,
                )

            if ble_device is None:
                raise RuntimeError("No connectable BLE device found for getsysmode read")

            _LOGGER.debug("[%s] Reading getsysmode payload", self._name)
            result = await self._soc_client.async_get_system_mode_payload(
                ble_device,
                device_name_hint=self._name,
            )
            self._last_system_mode_payload_read = {
                "ok": bool(result.get("ok", False)),
                "code": result.get("code"),
                "message": result.get("message"),
                "storage_id": result.get("storage_id"),
                "payload": result.get("payload"),
                "at": datetime.now(timezone.utc).isoformat(),
            }
            if not bool(result.get("ok", False)):
                raise RuntimeError(
                    "getsysmode read failed"
                    f" (code={result.get('code')}, message={result.get('message')})"
                )

            payload = result.get("payload")
            if isinstance(payload, dict) and self.data is not None:
                mode_value = payload.get("mode")
                if mode_value is not None:
                    self.data.system_mode = str(mode_value)
                    self._notify_if_state_changed()

            return result

    @property
    def last_system_mode_payload_read(self) -> dict[str, Any] | None:
        """Return the most recent getsysmode read attempt."""
        return self._last_system_mode_payload_read

    async def async_probe_version_once(self) -> dict[str, Any]:
        """Run one manual one-shot version query and update coordinator data."""
        async with self._poll_lock:
            await self._soc_client.async_close_session()
            service_info: BluetoothServiceInfoBleak | None = self._last_service_info

            if service_info is not None and service_info.connectable:
                ble_device = service_info.device
            elif service_info is not None:
                ble_device = bluetooth.async_ble_device_from_address(
                    self.hass,
                    service_info.device.address,
                    connectable=True,
                )
            else:
                ble_device = bluetooth.async_ble_device_from_address(
                    self.hass,
                    self._address,
                    connectable=True,
                )

            if ble_device is None:
                result = {
                    "ok": False,
                    "message": "No connectable BLE device found for version probe",
                    "versions": {},
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                self._last_version_probe_read = result
                raise RuntimeError(result["message"])

            _LOGGER.debug("[%s] Running one-shot version probe", self._name)
            try:
                versions = await self._soc_client.async_query_version_info_once(
                    ble_device,
                    device_name_hint=self._name,
                )
            except Exception as err:  # noqa: BLE001
                result = {
                    "ok": False,
                    "message": f"{type(err).__name__}: {err}",
                    "versions": {},
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                self._last_version_probe_read = result
                raise

            if versions:
                self._apply_version_info(versions)
                self._notify_if_state_changed()
                result = {
                    "ok": True,
                    "message": "version probe successful",
                    "versions": dict(versions),
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            else:
                result = {
                    "ok": False,
                    "message": "version probe returned no data",
                    "versions": {},
                    "at": datetime.now(timezone.utc).isoformat(),
                }

            self._last_version_probe_read = result
            return result

    @property
    def last_version_probe_read(self) -> dict[str, Any] | None:
        """Return the most recent manual version-probe attempt."""
        return self._last_version_probe_read

    async def async_set_advanced_schedule(
        self,
        *,
        peak_time: list[str],
        valley_time: list[str],
        schedule: list[Any] | None = None,
    ) -> None:
        """Set Advanced mode charge/discharge schedule over BLE.

        This maps to EMA app `setsysmode` writes with mode=3 and
        `peakTime`/`valleyTime` payload arrays.
        """
        await self._async_prepare_for_write()
        try:
            async with self._poll_lock:
                await self._soc_client.async_close_session()
                service_info: BluetoothServiceInfoBleak | None = self._last_service_info

                if service_info is not None and service_info.connectable:
                    ble_device = service_info.device
                elif service_info is not None:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        service_info.device.address,
                        connectable=True,
                    )
                else:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        self._address,
                        connectable=True,
                    )

                if ble_device is None:
                    raise RuntimeError("No connectable BLE device found for advanced schedule write")

                _LOGGER.debug(
                    "[%s] Setting advanced schedule peak=%s valley=%s schedule_items=%s",
                    self._name,
                    peak_time,
                    valley_time,
                    0 if not schedule else len(schedule),
                )
                result = await self._soc_client.async_set_advanced_schedule(
                    ble_device,
                    peak_time=peak_time,
                    valley_time=valley_time,
                    schedule=schedule,
                    device_name_hint=self._name,
                )
                self._last_advanced_schedule_write = {
                    "ok": bool(result.get("ok", False)),
                    "code": result.get("code"),
                    "message": result.get("message"),
                    "requested_peak_time": list(peak_time),
                    "requested_valley_time": list(valley_time),
                    "requested_schedule": list(schedule or []),
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                if not bool(result.get("ok", False)):
                    raise RuntimeError(
                        "Advanced schedule write failed"
                        f" (code={result.get('code')}, message={result.get('message')})"
                    )
        finally:
            self._write_pending = False

        self._schedule_post_write_refresh()

    @property
    def last_advanced_schedule_write(self) -> dict[str, Any] | None:
        """Return the most recent advanced schedule write attempt."""
        return self._last_advanced_schedule_write

    async def async_set_peak_valley_schedule(
        self,
        *,
        peak_time: list[str],
        valley_time: list[str],
    ) -> None:
        """Set Peak Valley mode schedule over BLE using setsysmode."""
        await self._async_prepare_for_write()
        try:
            async with self._poll_lock:
                await self._soc_client.async_close_session()
                service_info: BluetoothServiceInfoBleak | None = self._last_service_info

                if service_info is not None and service_info.connectable:
                    ble_device = service_info.device
                elif service_info is not None:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        service_info.device.address,
                        connectable=True,
                    )
                else:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        self._address,
                        connectable=True,
                    )

                if ble_device is None:
                    raise RuntimeError("No connectable BLE device found for peak-valley schedule write")

                _LOGGER.debug(
                    "[%s] Setting peak-valley schedule peak=%s valley=%s",
                    self._name,
                    peak_time,
                    valley_time,
                )
                result = await self._soc_client.async_set_peak_valley_schedule(
                    ble_device,
                    peak_time=peak_time,
                    valley_time=valley_time,
                    device_name_hint=self._name,
                )
                self._last_peak_valley_schedule_write = {
                    "ok": bool(result.get("ok", False)),
                    "code": result.get("code"),
                    "message": result.get("message"),
                    "requested_peak_time": list(peak_time),
                    "requested_valley_time": list(valley_time),
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                if not bool(result.get("ok", False)):
                    raise RuntimeError(
                        "Peak-valley schedule write failed"
                        f" (code={result.get('code')}, message={result.get('message')})"
                    )

                if self.data is not None:
                    self.data.system_mode = "0"
                    self._notify_if_state_changed()
        finally:
            self._write_pending = False

        self._schedule_post_write_refresh()

    @property
    def last_peak_valley_schedule_write(self) -> dict[str, Any] | None:
        """Return the most recent peak-valley schedule write attempt."""
        return self._last_peak_valley_schedule_write

    async def async_set_buzzer_mode(self, mode: int) -> None:
        """Set buzzer mode over BLE and refresh coordinator data."""
        if mode not in {0, 1}:
            raise ValueError(f"Invalid buzzer mode: {mode}")

        await self._async_prepare_for_write()
        try:
            async with self._poll_lock:
                await self._soc_client.async_close_session()
                service_info: BluetoothServiceInfoBleak | None = self._last_service_info

                if service_info is not None and service_info.connectable:
                    ble_device = service_info.device
                elif service_info is not None:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        service_info.device.address,
                        connectable=True,
                    )
                else:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        self._address,
                        connectable=True,
                    )

                if ble_device is None:
                    raise RuntimeError("No connectable BLE device found for buzzer mode write")

                _LOGGER.debug("[%s] Setting buzzer mode to %s", self._name, mode)
                result = await self._soc_client.async_set_buzzer_mode(
                    ble_device,
                    mode=mode,
                    device_name_hint=self._name,
                )
                self._last_buzzer_mode_write = {
                    "ok": bool(result.get("ok", False)),
                    "code": result.get("code"),
                    "message": result.get("message"),
                    "requested_buzzer_mode": str(mode),
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                if not bool(result.get("ok", False)):
                    raise RuntimeError(
                        "Buzzer mode write failed"
                        f" (code={result.get('code')}, message={result.get('message')})"
                    )

                if self.data is not None:
                    self.data.buzzer = int(mode)
                    self._field_write_timestamps["buzzer"] = datetime.now(timezone.utc)
                    self._notify_if_state_changed()
        finally:
            self._write_pending = False

        self._schedule_post_write_refresh()

    @property
    def last_buzzer_mode_write(self) -> dict[str, Any] | None:
        """Return the most recent buzzer mode write attempt."""
        return self._last_buzzer_mode_write

    async def async_clear_buzzer(self) -> None:
        """Clear active buzzer alarm over BLE and refresh coordinator data."""
        await self._async_prepare_for_write()
        try:
            async with self._poll_lock:
                await self._soc_client.async_close_session()
                service_info: BluetoothServiceInfoBleak | None = self._last_service_info

                if service_info is not None and service_info.connectable:
                    ble_device = service_info.device
                elif service_info is not None:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        service_info.device.address,
                        connectable=True,
                    )
                else:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        self._address,
                        connectable=True,
                    )

                if ble_device is None:
                    raise RuntimeError("No connectable BLE device found for clear buzzer")

                _LOGGER.debug("[%s] Clearing buzzer alarm", self._name)
                result = await self._soc_client.async_clear_buzzer(
                    ble_device,
                    device_name_hint=self._name,
                )
                self._last_clear_buzzer_write = {
                    "ok": bool(result.get("ok", False)),
                    "code": result.get("code"),
                    "message": result.get("message"),
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                if not bool(result.get("ok", False)):
                    raise RuntimeError(
                        "Clear buzzer failed"
                        f" (code={result.get('code')}, message={result.get('message')})"
                    )
        finally:
            self._write_pending = False

        self._schedule_post_write_refresh()

    @property
    def last_clear_buzzer_write(self) -> dict[str, Any] | None:
        """Return the most recent clear buzzer write attempt."""
        return self._last_clear_buzzer_write

    async def async_reboot_pcs(self) -> None:
        """Reboot the PCS over BLE."""
        await self._async_prepare_for_write()
        try:
            async with self._poll_lock:
                await self._soc_client.async_close_session()
                service_info: BluetoothServiceInfoBleak | None = self._last_service_info

                if service_info is not None and service_info.connectable:
                    ble_device = service_info.device
                elif service_info is not None:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        service_info.device.address,
                        connectable=True,
                    )
                else:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        self._address,
                        connectable=True,
                    )

                if ble_device is None:
                    raise RuntimeError("No connectable BLE device found for PCS reboot")

                _LOGGER.debug("[%s] Rebooting PCS", self._name)
                result = await self._soc_client.async_reboot_pcs(
                    ble_device,
                    device_name_hint=self._name,
                )
                self._last_pcs_reboot_write = {
                    "ok": bool(result.get("ok", False)),
                    "code": result.get("code"),
                    "message": result.get("message"),
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                if not bool(result.get("ok", False)):
                    raise RuntimeError(
                        "PCS reboot failed"
                        f" (code={result.get('code')}, message={result.get('message')})"
                    )
        finally:
            self._write_pending = False

    @property
    def last_pcs_reboot_write(self) -> dict[str, Any] | None:
        """Return the most recent PCS reboot write attempt."""
        return self._last_pcs_reboot_write

    async def async_set_selling_first(self, enabled: bool) -> None:
        """Set sellingFirst over BLE and refresh coordinator data."""
        await self._async_prepare_for_write()
        try:
            async with self._poll_lock:
                await self._soc_client.async_close_session()
                service_info: BluetoothServiceInfoBleak | None = self._last_service_info

                if service_info is not None and service_info.connectable:
                    ble_device = service_info.device
                elif service_info is not None:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        service_info.device.address,
                        connectable=True,
                    )
                else:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        self._address,
                        connectable=True,
                    )

                if ble_device is None:
                    raise RuntimeError("No connectable BLE device found for sellingFirst write")

                _LOGGER.debug("[%s] Setting sellingFirst to %s", self._name, enabled)
                result = await self._soc_client.async_set_selling_first(
                    ble_device,
                    enabled=enabled,
                    device_name_hint=self._name,
                )
                self._last_selling_first_write = {
                    "ok": bool(result.get("ok", False)),
                    "code": result.get("code"),
                    "message": result.get("message"),
                    "requested_selling_first": bool(enabled),
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                if not bool(result.get("ok", False)):
                    raise RuntimeError(
                        "sellingFirst write failed"
                        f" (code={result.get('code')}, message={result.get('message')})"
                    )

                if self.data is not None:
                    self.data.selling_first = bool(enabled)
                    self._notify_if_state_changed()
        finally:
            self._write_pending = False

        self._schedule_post_write_refresh()

    @property
    def last_selling_first_write(self) -> dict[str, Any] | None:
        """Return the most recent sellingFirst write attempt."""
        return self._last_selling_first_write

    async def async_set_valley_charge(self, enabled: bool) -> None:
        """Set valleycharge over BLE and refresh coordinator data."""
        await self._async_prepare_for_write()
        try:
            async with self._poll_lock:
                await self._soc_client.async_close_session()
                service_info: BluetoothServiceInfoBleak | None = self._last_service_info

                if service_info is not None and service_info.connectable:
                    ble_device = service_info.device
                elif service_info is not None:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        service_info.device.address,
                        connectable=True,
                    )
                else:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        self._address,
                        connectable=True,
                    )

                if ble_device is None:
                    raise RuntimeError("No connectable BLE device found for valleycharge write")

                _LOGGER.debug("[%s] Setting valleycharge to %s", self._name, enabled)
                result = await self._soc_client.async_set_valley_charge(
                    ble_device,
                    enabled=enabled,
                    device_name_hint=self._name,
                )
                self._last_valley_charge_write = {
                    "ok": bool(result.get("ok", False)),
                    "code": result.get("code"),
                    "message": result.get("message"),
                    "requested_valley_charge": bool(enabled),
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                if not bool(result.get("ok", False)):
                    raise RuntimeError(
                        "valleycharge write failed"
                        f" (code={result.get('code')}, message={result.get('message')})"
                    )

                if self.data is not None:
                    self.data.valley_charge = bool(enabled)
                    self._notify_if_state_changed()
        finally:
            self._write_pending = False

        self._schedule_post_write_refresh()

    @property
    def last_valley_charge_write(self) -> dict[str, Any] | None:
        """Return the most recent valleycharge write attempt."""
        return self._last_valley_charge_write

    async def async_set_peak_power(self, peak_power: int) -> None:
        """Set peakPower over BLE and refresh coordinator data."""
        if peak_power < 100 or peak_power > 50000:
            raise ValueError(f"Invalid peak power: {peak_power}")

        await self._async_prepare_for_write()
        try:
            async with self._poll_lock:
                await self._soc_client.async_close_session()
                service_info: BluetoothServiceInfoBleak | None = self._last_service_info

                if service_info is not None and service_info.connectable:
                    ble_device = service_info.device
                elif service_info is not None:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        service_info.device.address,
                        connectable=True,
                    )
                else:
                    ble_device = bluetooth.async_ble_device_from_address(
                        self.hass,
                        self._address,
                        connectable=True,
                    )

                if ble_device is None:
                    raise RuntimeError("No connectable BLE device found for peakPower write")

                _LOGGER.debug("[%s] Setting peakPower to %s", self._name, peak_power)
                result = await self._soc_client.async_set_peak_power(
                    ble_device,
                    peak_power=peak_power,
                    device_name_hint=self._name,
                )
                self._last_peak_power_write = {
                    "ok": bool(result.get("ok", False)),
                    "code": result.get("code"),
                    "message": result.get("message"),
                    "requested_peak_power": int(peak_power),
                    "at": datetime.now(timezone.utc).isoformat(),
                }
                if not bool(result.get("ok", False)):
                    raise RuntimeError(
                        "peakPower write failed"
                        f" (code={result.get('code')}, message={result.get('message')})"
                    )

                if self.data is not None:
                    self.data.peak_power = int(peak_power)
                    self._notify_if_state_changed()
        finally:
            self._write_pending = False

        self._schedule_post_write_refresh()

    @property
    def last_peak_power_write(self) -> dict[str, Any] | None:
        """Return the most recent peakPower write attempt."""
        return self._last_peak_power_write

    async def async_periodic_poll(self) -> None:
        """Run a fallback poll independent of advertisement event timing."""
        if self._shutdown:
            return

        # Fallback timer should not contend with an active advertisement poll.
        if self._poll_lock.locked():
            return

        await self._async_poll()

    @callback
    def _async_handle_bluetooth_event(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """Handle BLE advertisement events.

        The base class calls this on every advertisement.  We can optionally
        extract data from the advertisement payload here in the future if the
        PCS encodes any useful data in its manufacturer-specific data.
        """
        if self._shutdown:
            return

        _LOGGER.debug(
            "[%s] Advertisement received (RSSI %d dBm)",
            self._name,
            service_info.rssi,
        )
        # Pass through to base class which triggers _needs_poll / _async_poll.
        super()._async_handle_bluetooth_event(service_info, change)

    @callback
    def _async_handle_unavailable(
        self, service_info: BluetoothServiceInfoBleak
    ) -> None:
        """Handle the device going out of range / unavailable."""
        _LOGGER.info("[%s] Device is now unavailable", self._name)
        super()._async_handle_unavailable(service_info)
