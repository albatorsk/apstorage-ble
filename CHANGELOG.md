# Changelog

## v0.11.2 - 2026-04-09

### Fixed
- Temperature sensors now fall back to local BLE `RT0..RT5` telemetry arrays when named temperature fields are absent.
- Battery Temperature and Inverter Temperature no longer stay `Unknown` on the common APstorage local-data payload variant.

### Notes
- `manifest.json` version bumped to `0.11.2`.

## v0.11.1 - 2026-04-09

### Added
- New telemetry entities:
  - `Battery Temperature` sensor
  - `Inverter Temperature` sensor
- New PCS maintenance control:
  - `Reboot PCS` button entity
  - `apstorage_ble.reboot_pcs` service
- New Peak Valley mode write support:
  - `apstorage_ble.set_peak_valley_schedule` service for mode 0 peak/valley windows

### Changed
- Refactored simple BLE property writes to reuse a shared command helper for buzzer clear and PCS reboot actions.
- Extended coordinator diagnostics to track peak-valley schedule and PCS reboot writes.
- Updated documentation for new sensors, services, and controls.

### Notes
- `manifest.json` version bumped to `0.11.1`.

## v0.11.0 - 2026-04-09

### Added
- New PCS control entities for key app-parity settings:
  - `Selling First` switch
  - `Valley Charge` switch
  - `Peak Power` number (W)
- New buzzer controls:
  - Writable buzzer mode select
  - Clear buzzer button
- New integration services:
  - `apstorage_ble.set_selling_first`
  - `apstorage_ble.set_valley_charge`
  - `apstorage_ble.set_peak_power`
  - `apstorage_ble.set_buzzer_mode`
  - `apstorage_ble.clear_buzzer`

### Changed
- Added mode-aware availability logic for Peak Power (mode 5 only), matching app behavior.
- Added optimistic coordinator state updates after successful writes so UI updates immediately.
- Extended model and coordinator tracking for new writable PCS fields and write diagnostics.
- Updated documentation for newly added controls and services.

### Notes
- `manifest.json` version bumped to `0.11.0`.

## v0.10.7 - 2026-04-06

### Fixed
- Mode and Backup SOC write flows now tolerate additional real-world payload shapes.
- `getsysmode` response parsing now supports both dictionary and list-based `data` payloads when building `setsysmode` writes.
- Backup SOC mode gating now normalizes mode values (e.g. `"1.0"` -> `"1"`), preventing false `not_applicable` results.

### Changed
- Added mode-code normalization in system mode select mapping so UI reflects numeric-like mode values consistently.
- Added `userId` field to property request params for better parity with app requests.

### Notes
- `manifest.json` version bumped to `0.10.7`.

## v0.10.6 - 2026-04-06

### Fixed
- System Mode dropdown writes are now more reliable.
- Tightened response success detection for `setsysmode` writes:
  - no longer treats ambiguous `code=0` as success
  - accepts explicit success indicators (`code=1/200`, `result=true`, `status=success`)

### Changed
- After a successful mode write, the coordinator now updates `system_mode` immediately so the select entity reflects the new mode without waiting for telemetry lag.
- Applied the same stricter success detection helper to system mode, backup SOC, and advanced schedule writes.

### Notes
- `manifest.json` version bumped to `0.10.6`.

## v0.10.5 - 2026-04-06

### Added
- Advanced charge/discharge schedule write support in mode 3 (Advanced).
- New integration service `apstorage_ble.set_advanced_schedule`.
- Support for two schedule input styles:
  - `peak_time` / `valley_time` arrays with `HH:MM-HH:MM` or `HHMMSSHHMMSS` ranges.
  - Optional raw `schedule` payload (mutually exclusive with `peak_time`/`valley_time`).

### Changed
- BLE write flow for advanced schedule now mirrors EMA app behavior:
  1. `getsysmode`
  2. apply `mode=3` and schedule fields
  3. `setsysmode`
- Added validation for time ranges:
  - max 5 peak ranges and max 5 valley ranges
  - start and end cannot be equal
  - overlap detection across all configured ranges, including overnight windows
- Added coordinator diagnostics for advanced schedule writes.

### Notes
- `manifest.json` version bumped to `0.10.5`.

## v0.10.0 - 2026-04-06

### Added
- Writable System Mode support via new Home Assistant select entity.
- New integration service `apstorage_ble.set_system_mode`.
- Service discovery metadata in `custom_components/apstorage_ble/services.yaml`.
- Debug attributes on System Mode select entity:
  - `last_write_ok`
  - `last_write_code`
  - `last_write_message`
  - `last_write_requested_mode`
  - `last_write_at`

### Changed
- Integration now loads the `select` platform in addition to sensors.
- BLE write path now follows EMA-compatible flow:
  1. `getsysmode`
  2. apply requested mode
  3. `setsysmode`
- Added explicit system mode field tracking in coordinator/models.
- Updated sensor mode label mappings for all known mode codes.

### Notes
- `manifest.json` version bumped to `0.10.0`.
