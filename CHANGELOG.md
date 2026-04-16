# Changelog

## v0.12.2 - 2026-04-16

### Changed
- Removed the built-in Battery Power sensor entity from the integration.
- Kept only Battery Charging Power and Battery Discharging Power for battery power reporting.
- Added entity-registry cleanup for older Battery Power entries on upgrade.

### Notes
- manifest.json version bumped to 0.12.2.

## v0.12.1 - 2026-04-16

### Fixed
- Corrected the signed Battery Power calculation so charging now shows negative watt values instead of 0.
- Kept Battery Charging Power and Battery Discharging Power as separate entities.

### Notes
- manifest.json version bumped to 0.12.1.

## v0.12.0 - 2026-04-16

### Fixed
- Restored a separate signed Battery Power sensor entity.
- Renamed Battery Discharging Power to use its own unique entity key so it no longer conflicts with Battery Power.
- Removed broken temperature sensor references from the documentation.

### Notes
- manifest.json version bumped to 0.12.0.

## v0.11.8 - 2026-04-12

### Changed
- Removed unused legacy BLE modules that were no longer used by the integration runtime.
- Updated the probe-all-attributes helper script to use the active soc_client implementation path.
- Removed unused internal symbols from coordinator and constants modules as part of dead-code cleanup.

## v0.11.7 - 2026-04-12

### Fixed
- Prevented coordinator deadlocks after writable BLE actions (including `System Mode`) by avoiding lock re-entry when forcing post-write polls.
- Restored continuous sensor updates after mode/setting writes so entities like `Battery Flow State` no longer get stuck until restart.

## v0.11.6 - 2026-04-10

### Changed
- Removed `Battery Temperature` and `Inverter Temperature` entities from the integration sensor platform.
- Improved Backup SOC select robustness by normalizing mode-label variants (including `Self-Consumption` forms) when checking mode-gated availability.
- Improved Backup SOC current-value handling by normalizing/snapping parsed values to supported option steps instead of exposing `Unknown` for minor format/value mismatches.

### Notes
- `manifest.json` version bumped to `0.11.6`.

## v0.11.5 - 2026-04-09

### Fixed
- Removed `T2` and `T3` as battery and inverter temperature aliases.
- Reserved `T2` and `T3` for their energy-counter meanings so temperature entities are no longer populated from the wrong fields.
- Temperature extraction now relies on explicit temperature keys and `RT*` runtime temperature channels instead of overloaded total-energy keys.

### Notes
- `manifest.json` version bumped to `0.11.5`.

## v0.11.4 - 2026-04-09

### Fixed
- Temperature extraction now supports scalar and historical-array payload variants through a unified Celsius conversion path.
- Added direct key aliases for temperature fields observed in firmware variants (`T2`/`T3`, `RT0`/`RT1`/`RT2`/`RT3`, and PCS-specific aliases).
- Added support for offset-encoded temperature values (e.g. raw minus 100) to avoid `Unknown` temperature sensors.

### Notes
- `manifest.json` version bumped to `0.11.4`.

## v0.11.3 - 2026-04-09

### Fixed
- Improved temperature parsing for firmware variants with non-standard key names and scales.
- Added bounded fallback inference for battery/inverter temperature from temperature-like numeric fields when explicit keys are absent.
- Improved Celsius normalization by evaluating multiple scale factors and selecting the most plausible value.

### Notes
- `manifest.json` version bumped to `0.11.3`.

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
