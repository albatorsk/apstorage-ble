# APstorage BLE — Home Assistant Custom Integration

A Home Assistant custom integration for the **APsystems APstorage ELT-12 PCS** communicating over **Bluetooth Low Energy (BLE)**.

> **AI authorship notice:** This integration was written by AI (GitHub Copilot).

Compatibility note:
- Fully tested on **ELT-12**.
- **ELS-11.4** and **ELS-5K** may also work (autodiscovery and model mapping are implemented), but they have **not been tested** yet.

Designed to work with an **ESPHome Bluetooth proxy** placed near the PCS.

---

## Screenshot

![APstorage BLE device page in Home Assistant](docs/screenshots/dashboard.png)

---

## Status

Most core integration features are working in day-to-day use on ELT-12.

| Component | Status |
|---|---|
| HA integration structure | ✅ Complete |
| Config flow (auto-discover + manual MAC) | ✅ Complete |
| BLE client (BluFi custom data + app-layer AES, no BluFi security) | ✅ Rewritten in 2.0 |
| Polling (one short connection per poll) | ✅ Rewritten in 2.0 |
| Sensor entity platform | ✅ **Working** |
| System mode select (write) | ✅ **Working** |
| Buzzer mode select (write) | ✅ **Working** |
| Clear buzzer alarm (button/service) | ✅ **Working** |
| PCS reboot (button/service) | ✅ **Working** |
| Peak Valley schedule service (mode 0) | ✅ **Working** |
| sellingFirst/valleycharge/peakPower services | ✅ **Working** |
| Service call for mode changes | ✅ **Working** |

---

## Entities Created

The integration creates the following entities.

#### Battery
- Battery State of Charge (`%`)
- Battery Power (`W`, positive = discharging, negative = charging)
- Battery Discharging Power (`W`)
- Battery Charging Power (`W`)
- Daily Charged Energy (`kWh`)
- Daily Discharged Energy (`kWh`)
- Battery Flow State (`enum`: Charging, Discharging, Holding)
- Battery Capacity (`kWh`, diagnostic): the configured capacity from the storage configuration, as in the EMA app; unknown if none is set
- Minimum SOC (`%`, diagnostic)
- Backup SOC Level (`%`, diagnostic)

#### PV / Solar
- PV Power (`W`)
- Daily PV Energy Produced (`kWh`)

#### Grid
- Grid Power (`W`, positive = import, negative = export)
- Grid Import Power (`W`)
- Grid Export Power (`W`)
- Daily Grid Import (`kWh`)
- Daily Grid Export (`kWh`)
- Grid Frequency (`Hz`, when the PCS reports it)
- Grid Status (`enum`: On-grid, Off-grid)
- Generator Power (`W`, disabled by default)

#### Load / Output
- Load Power (`W`)
- Daily Total Consumed (`kWh`)

#### System
- System Mode (`enum`)
- Buzzer Alarm (`enum`: Active, None)
- CO2 Reduction (`kg`)
- Total Produced (`kWh`)
- Total Consumed (`kWh`)

#### Diagnostics
- PCS Firmware 1 / 2 / 3 (module versions)
- Communication Module Firmware
- Cloud Connection
- Price Area
- Wi-Fi Network, Wi-Fi Signal, Wi-Fi IP Address (disabled by default)
- System Mode Payload (last `getsysmode` read) and the Read System Mode Payload button (disabled by default)
- Last Update, Connection Quality, BLE MTU (disabled by default)

#### Firmware (update entity)
- Firmware (`update` entity): the installed PCS firmware and the latest version the PCS reports.
    - Shows "Update available" when the PCS has found newer firmware.
    - Once a day the integration asks the PCS to check the APsystems cloud (`set/checkPcsVersion`;
      the PCS needs Wi-Fi for this). The integration itself never contacts APsystems.
    - No Install button: install updates from the EMA app (`pcsUpdate` is never sent).

#### Schedule (calendar)
- Schedule (`calendar` entity): the charge (valley) and discharge (peak) windows the PCS follows, as calendar events.
    - Shows daily `peakTime`/`valleyTime` windows and weekly plans (`schedule`), including the plan the PCS holds in AI mode.
    - Empty in modes that don't use a schedule (Self-Consumption, Manual Control, Backup Power, Peak Shaving).
    - Covers the coming week (and the past day); the state is `on` during a window.
    - Attribute `windows` lists them, e.g. `Charge 02:45-12:15`.

#### Controls
- System Mode (`select` entity)
    - Self-Consumption
    - Time-Based Control
    - Backup Power
    - AI Mode
    - Peak Valley, Manual Control and Peak Shaving are not offered; if the PCS is put in one of them
      from the EMA app, the System Mode sensor shows its name and the select shows unknown.
- Backup SOC (`select` entity)
- Buzzer Mode (`select` entity)
    - Silent
    - Normal
- Selling First (`switch` entity)
- Valley Charge (`switch` entity)
- Peak Power (`number` entity, mode 5 only)
- Clear Buzzer Alarm (`button` entity)
- Reboot PCS (`button` entity)

Service:
- `apstorage_ble.set_system_mode`
    - `mode`: required: `1` Self-Consumption, `3` Time-Based Control, `4` Backup Power, `6` AI Mode (code or name)
    - `entry_id`: optional (target config entry)
    - `address`: optional (target device MAC)
- `apstorage_ble.get_system_mode_payload`
    - reads current `getsysmode` payload from battery over BLE
    - fires event `apstorage_ble_system_mode_payload` with fields:
      `address`, `storage_id`, `code`, `message`, `payload`
    - `entry_id`: optional (target config entry)
    - `address`: optional (target device MAC)
- `apstorage_ble.set_peak_valley_schedule`
    - `peak_time`: optional (`HH:MM-HH:MM` or `HHMMSSHHMMSS`, max 5)
    - `valley_time`: optional (`HH:MM-HH:MM` or `HHMMSSHHMMSS`, max 5)
    - at least one of `peak_time` / `valley_time` is required
    - `entry_id`: optional (target config entry)
    - `address`: optional (target device MAC)
- `apstorage_ble.set_advanced_schedule`
    - `schedule`: optional list of weekly plans, each with
        - `weekdays`: `mon`..`sun` or `1`..`7` (1 = Monday); each weekday in at most one plan
        - `charge`: charge (valley) windows, `HH:MM-HH:MM` or `HHMMSSHHMMSS`, max 5
        - `discharge`: discharge (peak) windows, same format, max 5
        - windows within a plan must not overlap; the EMA app uses 15-minute steps
        - the PCS-native form `{W, valleyTime, peakTime}` is accepted too
    - `peak_time`: optional daily peak/discharge ranges (`HH:MM-HH:MM` or `HHMMSSHHMMSS`, max 5)
    - `valley_time`: optional daily valley/charge ranges (`HH:MM-HH:MM` or `HHMMSSHHMMSS`, max 5)
    - use either `schedule` or `peak_time`/`valley_time`
    - example:
      ```yaml
      action: apstorage_ble.set_advanced_schedule
      data:
        schedule:
          - weekdays: [mon, tue, wed, thu, fri]
            charge: ["01:00-05:00"]
            discharge: ["17:00-21:00"]
          - weekdays: [sat, sun]
            charge: ["10:00-14:00"]
      ```
    - `entry_id`: optional (target config entry)
    - `address`: optional (target device MAC)
- `apstorage_ble.set_buzzer_mode`
    - `buzzer_mode`: required (`0/1` or label `Silent/Normal`)
    - `entry_id`: optional (target config entry)
    - `address`: optional (target device MAC)
- `apstorage_ble.clear_buzzer`
    - `entry_id`: optional (target config entry)
    - `address`: optional (target device MAC)
- `apstorage_ble.reboot_pcs`
    - `entry_id`: optional (target config entry)
    - `address`: optional (target device MAC)
- `apstorage_ble.set_selling_first`
    - `enabled`: required (`true/false`)
    - `entry_id`: optional (target config entry)
    - `address`: optional (target device MAC)
- `apstorage_ble.set_valley_charge`
    - `enabled`: required (`true/false`)
    - `entry_id`: optional (target config entry)
    - `address`: optional (target device MAC)
- `apstorage_ble.set_peak_power`
    - `peak_power`: required (`100..50000`, mode 5 only)
    - `entry_id`: optional (target config entry)
    - `address`: optional (target device MAC)

---

## Plan a one-off charge or discharge

The PCS schedule repeats by weekday; it has no dates. To charge or discharge at a specific date and
time, let Home Assistant handle the date and send the window to the PCS just before it starts.

1. Add a calendar you can edit: **Settings → Devices & Services → Add Integration → Local Calendar**,
   named e.g. *Battery plan*.
2. Add an event titled **Discharge** (or **Charge**) at the date and time you want.
3. Create this automation (**Settings → Automations → Create → Edit in YAML**). Five minutes before
   each event it sends the window to the PCS (Time-Based Control mode); when the event ends it switches the PCS
   back to your normal mode.

```yaml
alias: Battery plan
triggers:
  - trigger: calendar
    event: start
    entity_id: calendar.battery_plan
    offset: "-0:05:00"
    id: start
  - trigger: calendar
    event: end
    entity_id: calendar.battery_plan
    id: end
conditions:
  - condition: template
    value_template: "{{ trigger.calendar_event.summary in ['Charge', 'Discharge'] }}"
actions:
  - choose:
      - conditions:
          - condition: trigger
            id: start
        sequence:
          - variables:
              weekday: "{{ as_datetime(trigger.calendar_event.start).isoweekday() }}"
              window: >-
                {{ as_datetime(trigger.calendar_event.start).strftime('%H:%M') }}-{{
                as_datetime(trigger.calendar_event.end).strftime('%H:%M') }}
              kind: "{{ trigger.calendar_event.summary | lower }}"
          - action: apstorage_ble.set_advanced_schedule
            data:
              schedule:
                - weekdays: ["{{ weekday }}"]
                  charge: "{{ [window] if kind == 'charge' else [] }}"
                  discharge: "{{ [window] if kind == 'discharge' else [] }}"
      - conditions:
          - condition: trigger
            id: end
        sequence:
          - action: apstorage_ble.set_system_mode
            data:
              mode: "6"   # your normal mode; 6 = AI Mode
```

Notes:
- Set `mode` in the last action to the mode you normally run (`1`, `3`, `4` or `6`, see System Mode above). In AI mode
  the AI writes its own plan again once it is back.
- While the plan is active the PCS runs Time-Based Control: self-consumption outside the window.
- Each event replaces the PCS schedule with its own window, so don't let Charge and Discharge events
  overlap; the later one wins.
- The integration's **Schedule** calendar shows the window once it has been sent.

---

## Installation

### Via HACS (recommended)

[![Open your Home Assistant instance and show the add custom repository dialog with a specific repository URL pre-filled.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=albatorsk&repository=apstorage-ble&category=integration)

1. Add this repository as a custom repository in HACS using the Integration category.
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
6. Setup finishes once the first poll has read the PCS, so all entities have values right away.

---

## Protocol Notes

The BLE client follows the protocol in `APSTORAGE_BLE_SPEC.md`, verified on an ELT-12 (firmware dcmVer 0.1.66.0):
Espressif BluFi custom-data frames on service `0xFFEC` (write `0xFF07`, notify `0xFF06`), without BluFi
security negotiation, carrying JSON encrypted with the app's static AES-128-CBC key.

Each poll opens one connection, sends `getDeviceLastDataLocal` and `getsysmode` (plus `deviceInfo` and the
storage configuration, `getStorageConfigurationInfo` or `storageBasicInfo`, and the firmware versions,
`pcsVersion`, on the first poll and every 6 hours), and disconnects. Once a day a poll also sends
`set/checkPcsVersion` so the PCS checks for new firmware; `pcsVersion` is read again a minute later, so the phone app can connect between polls. A failed poll
keeps the last values; entities become unavailable after 3 failed polls in a row. Settings changes read
`getsysmode`, change only the requested fields, write `setsysmode` and read the result back.

To see the raw traffic, enable debug logging for `custom_components.apstorage_ble`.

---

## Device info

| Field | Value |
|---|---|
| Device name | `PCS_B050XXXXXXXX` |
| Bluetooth MAC | `AA:BB:CC:DD:EE:FF` |
| Protocol | BLE GATT (custom service) |
| Connection | One short connection per poll (every 30 s by default) |

---

## Architecture

```
custom_components/apstorage_ble/
├── __init__.py        # Entry setup / teardown
├── manifest.json      # Integration metadata & BLE matchers
├── const.py           # Constants, model lookup
├── config_flow.py     # Auto-discovery + manual MAC entry
├── coordinator.py     # Polling and write commands (DataUpdateCoordinator)
├── pcs_client.py      # BLE client: BluFi framing, AES, request/reply
├── models.py          # PCSData and reply parsing
├── protocol.py        # Operating-mode helpers
├── schedule.py        # Charge/discharge windows from getsysmode
├── calendar.py        # Schedule calendar entity
├── update.py          # Firmware update entity
├── sensor.py / select.py / switch.py / number.py / button.py
├── strings.json       # UI strings
├── services.yaml      # Service definitions
└── translations/
    └── en.json
```

---

