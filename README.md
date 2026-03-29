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



### Entities

#### Battery
- Battery State of Charge (`%`)
- Battery Discharging Power (`W`)
- Battery Charging Power (`W`)
- Daily Charged Energy (`kWh`)
- Daily Discharged Energy (`kWh`)

#### PV / Solar
- Daily PV Energy Produced (`kWh`)
- PV Power (`W`)

#### Grid
- Grid Current (`A`)
- Grid Power (`W`)
- Grid Frequency (`Hz`)

#### Load / Output
- Load Voltage (`V`)
- Load Current (`A`)
- Load Power (`W`)

#### System
- System State (enum)
- Battery Flow State (enum: Charging, Discharging, Holding)
- Buzzer (enum: Silent, Normal)
- CO2 Reduction (`kg`)
- Total Produced (`kWh`)
- Total Consumed (`kWh`)
- Daily Total Consumed (`kWh`)

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

