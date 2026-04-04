"""Config flow for the APstorage BLE integration.

Supports three setup paths:
  1. Automatic discovery — HA's Bluetooth integration sees a BLE advertisement
     matching the ``bluetooth`` matchers in manifest.json (local_name "PCS_B050*")
     and calls ``async_step_bluetooth``.
  2. Scan — User opens the integration manually; the flow checks the Bluetooth
     cache first, then runs an active scan for up to SCAN_TIMEOUT seconds
     looking for a device whose name starts with "PCS_B050".  Found devices
     are shown in a picker.
  3. Manual entry — If no device is found after scanning, the user is asked to
     type the Bluetooth MAC address directly.
"""
from __future__ import annotations

import asyncio
import logging
import re

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
    async_process_advertisements,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import callback

from .const import (
    CONF_POLL_INTERVAL_SECONDS,
    DOMAIN,
    MANUFACTURER,
    MODEL,
    POLL_INTERVAL_MAX_SECONDS,
    POLL_INTERVAL_MIN_SECONDS,
    POLL_INTERVAL_SECONDS,
)

_LOGGER = logging.getLogger(__name__)

_MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")

# How long (seconds) to run an active BLE scan when no cached device exists.
SCAN_TIMEOUT = 10


def _is_apstorage_device(service_info: BluetoothServiceInfoBleak) -> bool:
    """Return True if the advertisement looks like an APstorage ELT-12 PCS."""
    name = service_info.name or ""
    return name.startswith("PCS_B050")


class APstorageConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for APstorage BLE."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> APstorageOptionsFlow:
        """Get the options flow for this handler."""
        flow = APstorageOptionsFlow()
        flow._config_entry = config_entry
        return flow

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        # Devices found during the user-triggered scan, keyed by upper-case MAC.
        self._discovered_devices: dict[str, BluetoothServiceInfoBleak] = {}

    # ------------------------------------------------------------------
    # Auto-discovery path (triggered by manifest.json bluetooth matchers)
    # ------------------------------------------------------------------

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle discovery via the Bluetooth integration."""
        _LOGGER.debug(
            "Bluetooth discovery: name=%s address=%s",
            discovery_info.name,
            discovery_info.address,
        )
        await self.async_set_unique_id(discovery_info.address.upper())
        self._abort_if_unique_id_configured()

        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address,
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Show a confirmation dialog for the auto-discovered device."""
        assert self._discovery_info is not None
        name = self._discovery_info.name or self._discovery_info.address

        if user_input is not None:
            return self.async_create_entry(
                title=name,
                data={CONF_ADDRESS: self._discovery_info.address.upper()},
            )

        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": name},
        )

    # ------------------------------------------------------------------
    # User-triggered path: scan first, picker if found, manual fallback
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Entry point when the user adds the integration manually.

        1. Check HA's Bluetooth cache for already-seen PCS_B050* devices.
        2. If none cached, show a prompt so the user can trigger an active scan.
        3. After scanning (or immediately if cached devices exist), show a device
           picker.
        4. If scanning times out with no result, fall back to manual MAC entry.
        """
        already_configured = {
            entry.unique_id
            for entry in self._async_current_entries()
            if entry.unique_id
        }

        # Check HA's Bluetooth advertisement cache.
        self._discovered_devices = {
            si.address.upper(): si
            for si in async_discovered_service_info(self.hass)
            if _is_apstorage_device(si)
            and si.address.upper() not in already_configured
        }

        if self._discovered_devices:
            # Skip straight to the picker — no scan needed.
            return await self.async_step_pick_device()

        if user_input is not None:
            # User clicked Submit on the scan prompt — run an active scan.
            _LOGGER.debug("Starting active BLE scan for PCS_B050* devices")
            try:
                found = await async_process_advertisements(
                    self.hass,
                    _is_apstorage_device,
                    {"local_name_pattern": "PCS_B050*"},
                    BluetoothScanningMode.ACTIVE,
                    SCAN_TIMEOUT,
                )
                if found.address.upper() not in already_configured:
                    self._discovered_devices[found.address.upper()] = found
            except asyncio.TimeoutError:
                _LOGGER.debug("BLE scan timed out — no ELT-12 found")

            if self._discovered_devices:
                return await self.async_step_pick_device()

            # Nothing found after scan → manual entry.
            return await self.async_step_manual()

        # Show the scan prompt (empty form — Submit starts the scan).
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    async def async_step_pick_device(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Present a picker of discovered ELT-12 devices."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS].upper()
            info = self._discovered_devices[address]
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=info.name or address,
                data={CONF_ADDRESS: address},
            )

        return self.async_show_form(
            step_id="pick_device",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            addr: f"{info.name} ({addr})"
                            for addr, info in self._discovered_devices.items()
                        }
                    )
                }
            ),
        )

    async def async_step_manual(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Accept a manually entered MAC address."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS].upper().strip()
            if not _MAC_RE.match(address):
                errors[CONF_ADDRESS] = "invalid_address"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"{MANUFACTURER} {MODEL} {address}",
                    data={CONF_ADDRESS: address},
                )

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ADDRESS,
                        description={"suggested_value": "AA:BB:CC:DD:EE:FF"},
                    ): str,
                }
            ),
            errors=errors,
        )


class APstorageOptionsFlow(OptionsFlow):
    """Handle APstorage BLE options."""

    def __init__(self) -> None:
        """Initialize the options flow."""
        self._config_entry: ConfigEntry | None = None

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Manage integration options."""
        if self._config_entry is None:
            raise RuntimeError("Options flow missing config entry")

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = int(
            self._config_entry.options.get(
                CONF_POLL_INTERVAL_SECONDS,
                POLL_INTERVAL_SECONDS,
            )
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_POLL_INTERVAL_SECONDS,
                        default=current_interval,
                    ): vol.All(
                        vol.Coerce(int),
                        vol.Range(
                            min=POLL_INTERVAL_MIN_SECONDS,
                            max=POLL_INTERVAL_MAX_SECONDS,
                        ),
                    )
                }
            ),
        )
