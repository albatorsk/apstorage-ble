# Changelog

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
