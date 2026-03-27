"""Low-level BLE/GATT client for the APstorage ELT-12 PCS.

This module owns the Bleak connection lifecycle.  It:
  1. Opens a GATT connection via bleak-retry-connector (works transparently
     with an ESPHome Bluetooth proxy).
  2. Subscribes to the notify characteristic.
  3. Writes the poll request to the write characteristic.
  4. Collects the response notification(s) and returns raw bytes.

The coordinator calls `async_fetch_data()` on every poll cycle; it should not
be called concurrently.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from .const import (
    BLE_NOTIFY_CHAR_UUID,
    BLE_WRITE_CHAR_UUID,
    RESPONSE_TIMEOUT_SECONDS,
)
from .models import PCSData
from .parser import build_poll_request, parse_response

_LOGGER = logging.getLogger(__name__)


class APstorageBLEClient:
    """Manages a single BLE session with the APstorage ELT-12 PCS."""

    def __init__(self, name: str) -> None:
        """Initialise.

        Args:
            name: Human-readable device name, used for logging only.
        """
        self._name = name
        # Accumulates raw bytes as notifications arrive (some devices split
        # their response across multiple notification packets).
        self._rx_buf: bytearray = bytearray()
        self._rx_event: asyncio.Event = asyncio.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def async_fetch_data(self, ble_device: BLEDevice) -> PCSData | None:
        """Connect to the device, send a poll request, return parsed data.

        A fresh connection is established for every poll cycle.  This is
        intentional: re-using a BleakClient between polls is less reliable,
        especially across ESPHome proxies.

        Returns None if the connection or parsing fails.
        """
        _LOGGER.debug("[%s] Connecting for poll", self._name)

        client: BleakClient | None = None
        try:
            client = await establish_connection(
                BleakClientWithServiceCache,
                ble_device,
                ble_device.address,
                disconnected_callback=self._on_disconnect,
                max_attempts=3,
                use_services_cache=True,
            )
            return await self._poll(client)

        except TimeoutError:
            _LOGGER.warning(
                "[%s] Timed out waiting for response from device", self._name
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("[%s] Unexpected error during poll: %s", self._name, exc)
        finally:
            if client and client.is_connected:
                await client.disconnect()
                _LOGGER.debug("[%s] Disconnected", self._name)

        return None

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _poll(self, client: BleakClient) -> PCSData | None:
        """Send request and collect the notification response."""
        self._rx_buf = bytearray()
        self._rx_event.clear()

        # Subscribe to incoming notifications
        await client.start_notify(BLE_NOTIFY_CHAR_UUID, self._on_notification)
        _LOGGER.debug("[%s] Subscribed to notifications", self._name)

        # Send poll request
        request = build_poll_request()
        _LOGGER.debug("[%s] Sending request: %s", self._name, request.hex())
        await client.write_gatt_char(
            BLE_WRITE_CHAR_UUID,
            request,
            response=True,
        )

        # Wait for the device to respond
        try:
            await asyncio.wait_for(
                self._rx_event.wait(),
                timeout=RESPONSE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"[{self._name}] No notification received within "
                f"{RESPONSE_TIMEOUT_SECONDS} s"
            ) from exc
        finally:
            try:
                await client.stop_notify(BLE_NOTIFY_CHAR_UUID)
            except Exception:  # noqa: BLE001
                pass  # Already disconnected — safe to ignore

        raw = bytes(self._rx_buf)
        _LOGGER.debug("[%s] Received %d bytes: %s", self._name, len(raw), raw.hex())

        return parse_response(raw)

    def _on_notification(self, _sender: Any, data: bytearray) -> None:
        """Handle an incoming notification packet.

        Some devices spread a single logical frame across multiple BLE
        notification packets (MTU fragmentation).  This method accumulates
        bytes; the event is set when we believe we have a complete frame.

        Blufi-style frame format:
          [0] type_subtype
          [1] flags
          [2] sequence
          [3] data_len
          [4..] data
          [+2] optional checksum when flags bit1 is set

        We only signal completion once a full frame has been accumulated.
        """
        _LOGGER.debug("Notification (%d bytes): %s", len(data), data.hex())
        self._rx_buf.extend(data)

        if len(self._rx_buf) >= 4:
            flags = int(self._rx_buf[1])
            data_len = int(self._rx_buf[3])
            has_checksum = (flags & 0x02) != 0
            expected_total = 4 + data_len + (2 if has_checksum else 0)
            if len(self._rx_buf) >= expected_total:
                self._rx_event.set()
            return

        # Fallback for unknown frame types: wake the waiter on first data.
        self._rx_event.set()

    def _on_disconnect(self, client: BleakClient) -> None:  # type: ignore[override]
        """Called by bleak when the connection drops unexpectedly."""
        _LOGGER.debug("[%s] Device disconnected", self._name)
        # Wake up any waiting _rx_event so the poll does not hang forever.
        self._rx_event.set()
