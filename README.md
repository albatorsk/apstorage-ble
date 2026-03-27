# APstorage BLE — Home Assistant Custom Integration

A Home Assistant custom integration for the **APstorage ELT-12 PCS** communicating over **Bluetooth Low Energy (BLE)**.

Designed to work with an **ESPHome Bluetooth proxy** placed near the PCS.

---

## Status

| Component | Status |
|---|---|
| HA integration structure | ✅ Complete |
| Config flow (auto-discover + manual MAC) | ✅ Complete |
| BLE connection / GATT session | ✅ Complete |
| Battery SoC query protocol | ✅ **Working** |
| Battery SoC sensor | ✅ **Working** |
| Standard device poll protocol | ⚠️ Under development |

---

## Working Sensors

- ✅ **Battery State of Charge (%)** – via custom Blufi encrypted protocol

Additional sensors are defined in the integration but require protocol implementation:
- Battery Voltage, Current, Temperature
- Grid and PV data
- Inverter data
- System state

---

## Installation

### Via HACS (recommended)

1. Add this repository as a custom repository in HACS.
2. Install "APstorage BLE".
3. Restart Home Assistant.

### Manual

1. Copy `custom_components/apstorage_ble/` into your HA `config/custom_components/` directory.
2. Restart Home Assistant.

---

## Setup

1. Make sure your ESPHome Bluetooth proxy is configured and connected to HA.
2. Power on the PCS (`PCS_B050XXXXXXXX`, MAC `AA:BB:CC:DD:EE:FF`).
3. In HA go to **Settings → Devices & Services**.
4. The PCS should be auto-discovered.  Click **Configure** and confirm.
5. If not auto-discovered, click **Add Integration**, search for **APstorage BLE**, and enter the MAC address manually.

---

## Filling in the protocol

The integration is fully wired up but the BLE protocol layer contains **placeholders** that must be updated from your device communication data before any real data flows.

### Step 1 — Identify GATT characteristics

Use **nRF Connect** (Android/iOS) or `bluetoothctl` / `gatttool` to connect to the PCS and browse its GATT services.

Note down:
- The **Service UUID** of the custom service
- The **Write** characteristic UUID (you'll write request frames here)
- The **Notify** characteristic UUID (the device sends responses here)

Update these in [`custom_components/apstorage_ble/const.py`](custom_components/apstorage_ble/const.py):

```python
BLE_SERVICE_UUID    = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
BLE_WRITE_CHAR_UUID = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
BLE_NOTIFY_CHAR_UUID = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
```

### Step 2 — Decode the request frame

In your device communication logs, filter for **ATT Write** packets sent to the write characteristic.
These are the poll requests the companion app sends.

Update `build_poll_request()` in [`parser.py`](custom_components/apstorage_ble/parser.py).

### Step 3 — Decode the response frame

Filter for **ATT Handle Value Notification** packets on the notify characteristic.
Map the byte offsets to the sensor fields.

Update `parse_response()` in [`parser.py`](custom_components/apstorage_ble/parser.py).

Key things to determine:
- Frame header / start-of-frame magic bytes
- Length field location and meaning (total length vs payload length)
- Command/response code byte
- Byte offsets of each value (voltage, current, power, SoC, …)
- Scale factors (e.g., divide by 10 for 0.1 V resolution)
- Signed vs unsigned integers (current direction)
- Checksum algorithm (XOR, sum, CRC-8, CRC-16/Modbus, …)

### Step 4 — Multi-packet frames

If the device sends large frames split across multiple BLE notification packets
(MTU fragmentation), implement proper frame-complete detection in
`_on_notification()` inside [`ble_client.py`](custom_components/apstorage_ble/ble_client.py).

---

## Device info

| Field | Value |
|---|---|
| Device name | `PCS_B050XXXXXXXX` |
| Bluetooth MAC | `AA:BB:CC:DD:EE:FF` |
| Protocol | BLE GATT (custom service) |
| Connection | Active (polled every 30 s) |

---

## Architecture

```
custom_components/apstorage_ble/
├── __init__.py        # Entry setup / teardown
├── manifest.json      # Integration metadata & BLE matchers
├── const.py           # GATT UUIDs, constants          ← update UUIDs here
├── config_flow.py     # Auto-discovery + manual MAC entry
├── coordinator.py     # ActiveBluetoothDataUpdateCoordinator
├── ble_client.py      # GATT session (connect / write / notify)
├── parser.py          # Protocol framing & parsing      ← main edit target
├── models.py          # PCSData dataclass
├── sensor.py          # Sensor entities
├── strings.json       # UI strings
└── translations/
    └── en.json
```
