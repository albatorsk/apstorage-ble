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


### Confirmed working entities

All entities below are confirmed working and available in the integration:

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

All entities have been tested and confirmed working on current hardware and firmware. No deprecated or unavailable sensors remain in the integration.

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

### v0.1.3

- Restored `Battery Voltage` and `Battery Current` on payload variants where these instantaneous values are reported via `DE2`/`DE3`, with plausibility validation.
- Kept `Grid Voltage`/`Grid Current` protected from accumulator-like `DE*` mappings to prevent daily-reset/day-ramp misreads.
- Added safe grid fallback behavior when explicit grid voltage/current are absent: nominal `230 V` fallback for voltage and derived current from `grid_power / grid_voltage`.
- Aligned battery flow derivation and model documentation with APstorage sign convention (positive battery current/power = `Discharging`).

### v0.1.2

- Fixed incorrect `Grid Voltage`, `Grid Current`, and `Battery Voltage` readings caused by accumulator-like fallback fields in some payloads.
- Hardened metric parsing with stricter plausibility checks for instantaneous voltage/current values.
- Removed risky `de*` fallback mappings from affected instantaneous sensors to avoid midnight-reset/day-ramp behavior.
- Added `scripts/get_grid_frequency.py` helper for direct BLE frequency checks and diagnostics.

### v0.1.1

- Fixed `Battery Flow State` in Home Assistant to reflect live APstorage battery direction.
- Corrected APstorage sign convention handling: positive battery power/current now maps to `Discharging`, negative maps to `Charging`.
- Updated coordinator flow-state resolution to prioritize live telemetry so the entity no longer stays on `Charging` while discharging.

### v0.1.0

- Fixed `Battery Flow State` reporting being stuck on `Charging` for some devices.
- Live battery telemetry (`battery_power`/`battery_current`) now takes precedence over `essStatus` when determining charge/discharge direction.
- Added debug logging when `essStatus` and live telemetry disagree, to help firmware-specific diagnostics.

### v0.0.37

- Added a new `Battery Flow State` enum sensor with values `Charging`, `Discharging`, and `Holding`.
- Implemented app-compatible battery flow mapping from `essStatus` (`0` -> `Discharging`, `1` -> `Charging`, otherwise `Holding`).
- Added fallback battery flow detection from battery power/current when `essStatus` is not present.
- `System State` now maps key numeric modes to labels (`1` -> `Self-consumption`, `3` -> `Advanced mode`).

### v0.0.36

- Improved daily energy reliability for charging/discharging counters.
- Daily counters now support partial direct totals (charged or discharged) from firmware payloads.
- Fallback integration now updates missing sides instead of skipping all daily energy updates.
- Improved direction handling when battery current and power signs disagree.
