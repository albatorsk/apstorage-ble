# Changelog

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
