"""BLE protocol parser for the APstorage ELT-12 PCS.

Current protocol observations indicate this device uses a Blufi-style frame:

    [type_subtype] [flags] [seq] [data_len] [data...] [optional_checksum]

Where:
    - type_subtype: low 2 bits = type, upper 6 bits = subtype
    - flags bit0: encrypted payload
    - flags bit1: checksum included (2-byte CRC16)
    - flags bit4: fragmented frame (more data follows)

Observed ACK frame in probes:
    49 04 XX 01 YY
    type=1 subtype=18 seq=XX len=1 status=YY
"""
from __future__ import annotations

import logging

from .models import PCSData

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request builder
# ---------------------------------------------------------------------------

def build_poll_request() -> bytes:
        """Return a Blufi-style Request Device Status frame.

    From observed protocol behavior:
            request status command = X(type=0, subtype=5) = 0x14
            frame format           = [cmd][flags][seq][len]

        We send seq=0 and len=0 for a single standalone poll frame.
        """
        return bytes([0x14, 0x00, 0x00, 0x00])


# ---------------------------------------------------------------------------
# Response parser
# ---------------------------------------------------------------------------

def parse_response(data: bytes) -> PCSData | None:
    """Parse a raw notification frame from the PCS into a PCSData object."""
    if len(data) < 4:
        _LOGGER.debug("Frame too short (%d bytes), ignoring: %s", len(data), data.hex())
        return None

    if not _validate_frame(data):
        _LOGGER.debug("Frame failed basic validation, ignoring: %s", data.hex())
        return None

    type_subtype = data[0]
    flags = data[1]
    seq = data[2]
    data_len = data[3]

    payload_end = 4 + data_len
    payload = data[4:payload_end]

    frame_type = type_subtype & 0x03
    subtype = (type_subtype & 0xFC) >> 2

    # Status ACK packet observed from device:
    #   type=1, subtype=18, len=1, payload[0]=status code
    if frame_type == 1 and subtype == 18 and data_len >= 1:
        status_code = payload[0]
        state = f"ack type=1 subtype=18 seq={seq} status=0x{status_code:02x}"
        _LOGGER.debug("Parsed status ACK frame: %s", state)
        return PCSData(system_state=state)

    # Device status response (observed from command 14 00 00 00):
    #   type=1 subtype=15
    # Payload layout from observed protocol behavior:
    #   byte0: op_mode
    #   byte1: sta_conn
    #   byte2: softap_conn
    #   then TLVs: [field_id][field_len][field_data...]
    if frame_type == 1 and subtype == 15 and data_len >= 3:
        op_mode = payload[0]
        sta_conn = payload[1]
        softap_conn = payload[2]

        idx = 3
        ssid: str | None = None
        bssid: str | None = None
        tlv_parts: list[str] = []

        while idx + 2 <= len(payload):
            field_id = payload[idx]
            field_len = payload[idx + 1]
            idx += 2
            if idx + field_len > len(payload):
                break
            value = payload[idx: idx + field_len]
            idx += field_len

            if field_id == 1 and field_len == 6:
                bssid = ":".join(f"{b:02x}" for b in value)
                tlv_parts.append(f"bssid={bssid}")
            elif field_id == 2:
                try:
                    ssid = value.decode("utf-8", errors="replace")
                except Exception:  # noqa: BLE001
                    ssid = value.hex()
                tlv_parts.append(f"ssid={ssid}")
            else:
                tlv_parts.append(
                    f"tlv:id={field_id} len={field_len} val={value.hex()}"
                )

        state = (
            f"status type=1 subtype=15 seq={seq} "
            f"op_mode={op_mode} sta_conn={sta_conn} softap_conn={softap_conn}"
        )
        if tlv_parts:
            state = f"{state} {' '.join(tlv_parts)}"

        _LOGGER.debug("Parsed device status frame: %s", state)
        return PCSData(system_state=state)

    # Generic decoded frame summary for protocol discovery.
    state = (
        f"frame type={frame_type} subtype={subtype} "
        f"seq={seq} flags=0x{flags:02x} len={data_len} payload={payload.hex()}"
    )
    _LOGGER.debug("Parsed generic frame: %s", state)
    return PCSData(system_state=state)


# ---------------------------------------------------------------------------
# Frame helpers
# ---------------------------------------------------------------------------

def _validate_frame(data: bytes) -> bool:
    """Validate minimal Blufi-style frame structure and expected length."""
    if len(data) < 4:
        return False

    flags = data[1]
    data_len = data[3]
    has_checksum = (flags & 0x02) != 0
    expected = 4 + data_len + (2 if has_checksum else 0)
    return len(data) >= expected
