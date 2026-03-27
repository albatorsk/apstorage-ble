"""Config flow for the APstorage BLE integration.

Supports two setup paths:
  1. Automatic discovery — HA's Bluetooth integration sees a BLE advertisement
    matching the ``bluetooth`` matchers in manifest.json (local_name "PCS_B050*")
     and calls ``async_step_bluetooth``.
  2. Manual entry — The user opens the integration and types the MAC address.
     Useful if the device is behind an ESPHome proxy that is already paired.
"""
from __future__ import annotations

import logging
import re

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import DOMAIN, MANUFACTURER, MODEL

_LOGGER = logging.getLogger(__name__)

_MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


def _is_apstorage_device(service_info: BluetoothServiceInfoBleak) -> bool:
    """Return True if the advertisement looks like an APstorage PCS."""
    name = service_info.name or ""
    return name.startswith("PCS_B050")


class APstorageConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for APstorage BLE."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None

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
    # Manual entry path
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> ConfigFlowResult:
        """Handle the initial user-triggered setup step.

        If there are already-discovered APstorage devices that haven't been
        added yet, we present a picker; otherwise we ask for a MAC address.
        """
        # Collect any PCS devices seen by HA's Bluetooth stack that are not
        # yet configured.
        already_configured = {
            entry.unique_id
            for entry in self._async_current_entries()
            if entry.unique_id
        }
        discovered = {
            service_info.address.upper(): service_info
            for service_info in async_discovered_service_info(self.hass)
            if _is_apstorage_device(service_info)
            and service_info.address.upper() not in already_configured
        }

        if discovered:
            # Offer a picker of known devices.
            if user_input is not None:
                address = user_input[CONF_ADDRESS].upper()
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                info = discovered[address]
                return self.async_create_entry(
                    title=info.name or address,
                    data={CONF_ADDRESS: address},
                )

            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema(
                    {
                        vol.Required(CONF_ADDRESS): vol.In(
                            {
                                addr: f"{info.name} ({addr})"
                                for addr, info in discovered.items()
                            }
                        )
                    }
                ),
            )

        # No device discovered — fall back to free-form MAC address entry.
        return await self.async_step_manual()

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
