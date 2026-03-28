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
| Blufi handshake + encrypted local-data query | ✅ **Working** |
| Active + fallback periodic polling | ✅ **Working** |
| Sensor entity platform | ✅ **Working** |

---

## Implemented Sensors

The following sensors are implemented in the integration.

### Confirmed working on current test device

- ✅ **Battery State of Charge** (`%`)
- ✅ **Battery Voltage** (`V`)
- ✅ **Battery Current** (`A`)
- ✅ **Battery Power** (`W`)
- ✅ **Battery Temperature** (`°C`)
- ✅ **Inverter Temperature** (`°C`)
- ✅ **System State** (raw state code/string)
- ✅ **Grid Power** (`W`)
- ✅ **PV Power** (`W`)
- ✅ **Load Power** (`W`)

### Implemented with fallback behavior

- ✅ **Battery Charged Energy** (`kWh`)
- ✅ **Battery Discharged Energy** (`kWh`)

Notes:
- If the device payload exposes true cumulative totals, those are used directly.
- If totals are missing (common on some firmware), the integration derives estimated kWh from `battery_power × elapsed_time`.
- Estimated counters reset on HA/integration restart.

### Defined but may remain unavailable (firmware/payload dependent)

- ⚠️ **Grid Voltage** (`V`)
- ⚠️ **Grid Current** (`A`)
- ⚠️ **Grid Frequency** (`Hz`)
- ⚠️ **PV Voltage** (`V`)
- ⚠️ **PV Current** (`A`)
- ⚠️ **Load Voltage** (`V`)
- ⚠️ **Load Current** (`A`)

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

## Protocol Notes

The integration currently polls the PCS using a Blufi-based encrypted local-data flow.
Current implementation status is best reflected in the sensor matrix above.

If you are working with a different PCS firmware variant, capture logs and compare payload keys before assuming parity.

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

---

## Release Notes

### v0.0.36

- Improved daily energy reliability for charging/discharging counters.
- Daily counters now support partial direct totals (charged or discharged) from firmware payloads.
- Fallback integration now updates missing sides instead of skipping all daily energy updates.
- Improved direction handling when battery current and power signs disagree.
