# Changelog

## v0.19.9 - 2026-05-06

### Added
- Firmware version info (current, latest, software, hardware) is now fetched once on the first successful poll after integration startup, using the already-open BLE connection. It is never queried again until the integration restarts, so it cannot interfere with normal SoC polling.

### Notes
- `manifest.json` version bumped to `0.19.9`.

## v0.19.8 - 2026-05-06

### Changed
- Disabled version and alarm enrichment during normal polling so periodic reads now only perform the base local-data query.
- This isolates secondary diagnostic requests from the main SoC update path to reduce `Unknown` regressions under unstable BLE links.

### Notes
- `manifest.json` version bumped to `0.19.8`.

## v0.19.7 - 2026-05-06

### Fixed
- Improved BLE compatibility by selecting protocol profile parameters dynamically from device naming hints before session setup.
- Added support for additional protocol families with profile-specific write/notify UUID pairs and request `productKey` handling.
- Updated request payload encryption/decryption to use the active profile AES parameters during query flows.

### Notes
- `manifest.json` version bumped to `0.19.7`.

## v0.19.6 - 2026-05-06

### Changed
- Switched default polling strategy to one-shot connect/query/disconnect for shared ESPHome proxy stability.
- Persistent-session mode is no longer enabled by default and will only be used when explicitly enabled in code defaults.
- Increased one-shot poll retries from 1 to 2 attempts per poll cycle to improve success probability on noisy BLE links.

### Notes
- `manifest.json` version bumped to `0.19.6`.

## v0.19.5 - 2026-05-06

### Fixed
- Fixed entities staying `Unknown` despite successful metric reads when Bluetooth advertisement-based availability temporarily dropped.
- Coordinator now tracks recent successful polls and exposes runtime availability that remains true for a grace period after good data.
- Sensor/select/number/switch/button entities now use runtime availability instead of raw advertisement availability.

### Notes
- `manifest.json` version bumped to `0.19.5`.

## v0.19.4 - 2026-05-06

### Fixed
- Fixed a regression where successful local-data reads could still end up as `Unknown` in Home Assistant because slow version/alarm enrichment exhausted the overall query timeout before metrics were returned.
- Version and alarm enrichment are now strictly best-effort with a small time budget, so successful SoC/data payloads are returned immediately even when secondary diagnostic endpoints are slow or unresponsive.

### Notes
- `manifest.json` version bumped to `0.19.4`.

## v0.19.3 - 2026-05-06

### Fixed
- Fixed repeated polling stalls on busy/shared BLE proxy environments where persistent session open/query could hang and trigger `Poll watchdog timed out` loops.
- Coordinator now auto-disables persistent-session mode after the first persistent-session failure/timeout and continues polling via one-shot connect/query/disconnect for the rest of the runtime.

### Notes
- `manifest.json` version bumped to `0.19.3`.

## v0.19.2 - 2026-05-06

### Fixed
- Improved resilience when persistent BLE sessions become unstable on shared ESPHome proxy environments: polling now falls back to a one-shot connect/query/disconnect cycle in the same poll cycle instead of failing the entire update.
- This specifically addresses repeated `Poll watchdog timed out` and `BleakError: Not connected` loops where entities could remain `Unknown` for extended periods.

### Notes
- `manifest.json` version bumped to `0.19.2`.

## v0.19.1 - 2026-05-06

### Fixed
- Fixed a persistent-session regression where non-timeout BLE exceptions during polling could escape `_async_poll()` and leave entities stuck as `Unknown`.
- Poll failures now consistently close the active BLE session and recover on the next cycle.

### Changed
- Reduced alarm endpoint query frequency from once per minute to once every 10 minutes to lower timeout noise and reduce pressure on the BLE session.

### Notes
- `manifest.json` version bumped to `0.19.1`.

## v0.19.0 - 2026-05-06

### Changed
- **Persistent BLE session**: The coordinator now keeps the BLE connection open between polls and reuses the established Blufi DH session for every data read. Previously each 30-second poll cycle reconnected and repeated the full DH key exchange, which left the device's Blufi state machine in a partial state and caused most poll attempts to fail until the device internally timed out and reset (~10–30 s). The new approach matches how the EMA app behaves and makes every poll succeed reliably.
- On connection failure or watchdog timeout the session is closed and re-established on the next cycle.
- All write operations (system mode, backup SoC, schedules, buzzer, etc.) close the persistent poll session before opening their own connection, preventing conflicts.
- Integration unload now explicitly closes the persistent session, cleanly releasing the ESPHome Bluetooth proxy slot.

### Notes
- `manifest.json` version bumped to `0.19.0`.

## v0.18.6 - 2026-05-06

### Fixed
- Prevented redundant poll triggering while a poll is already in progress by gating both advertisement and periodic fallback paths behind the poll lock.
- Reduced BLE stall amplification by running a single query attempt per poll cycle, allowing faster recovery on subsequent cycles instead of spending one cycle in long retry loops.
- Tightened disconnect timeout behavior to reduce lock hold time when ESPHome proxy disconnect requests are unresponsive.

### Notes
- `manifest.json` version bumped to `0.18.6`.

## v0.18.5 - 2026-05-06

### Fixed
- Improved BLE recovery under ESPHome proxy contention: reduced per-poll retry pressure and bounded query/connect timing to avoid long in-progress poll stalls.
- Added explicit handling for proxy connection-slot exhaustion so poll attempts fail fast and retry on the next scheduled cycle.
- Improved disconnect robustness with a longer timeout and one retry path to better release busy proxy sessions.

### Notes
- `manifest.json` version bumped to `0.18.5`.

## v0.18.4 - 2026-05-06

### Fixed
- Fixed BLE teardown when disabling the integration: in-flight polls are now cancelled during coordinator shutdown so ESPHome Bluetooth proxy connections are released promptly.
- Added bounded shutdown wait for poll cancellation to avoid long unload hangs while still allowing clean disconnect handling.

### Notes
- `manifest.json` version bumped to `0.18.4`.

## v0.18.3 - 2026-05-06

### Fixed
- Fixed integration becoming unresponsive after Home Assistant restart: BLE services cache is now cleared on the first connection attempt to avoid stale cache entries that cause connection timeouts.
- Improved error logging: BLE connection failures now show exception type and detailed error message instead of empty error strings, making debugging easier.
- Added BlueZ stack stabilization delay before first BLE connection attempt to improve reliability immediately after Home Assistant startup.

### Notes
- `manifest.json` version bumped to `0.18.3`.

## v0.18.2 - 2026-05-04

### Fixed
- Reduced misleading State of Charge history artifacts during temporary BLE polling outages: after repeated consecutive poll failures, SoC is now marked unavailable instead of holding a stale percentage for extended periods.
- Applied the stale-SoC guard to both no-metrics polls and watchdog timeout paths so nightly polling gaps no longer appear as long flat-line plateaus followed by sudden drops.

### Notes
- `manifest.json` version bumped to `0.18.2`.

## v0.18.0 - 2026-04-26

### Fixed
- Fixed false failure reporting when changing System Mode: if the PCS applies the write but does not return a successful `setsysmode` acknowledgement, the integration now performs a `getsysmode` read-back and treats the write as successful when the requested mode is confirmed.

### Notes
- `manifest.json` version bumped to `0.18.0`.

## v0.17.7 - 2026-04-26

### Fixed
- Fixed startup hang: `_async_poll` now skips immediately if another poll is already in progress, preventing multiple advertisement-triggered polls from queuing behind the BLE lock and blocking Home Assistant bootstrap for several minutes.
- Raised poll watchdog timeout from 60 s to 120 s so it no longer fires before the natural BLE failure path (2×30 s frame wait + connection overhead ≈75 s) completes, which was causing an infinite kill-restart cycle with no data ever arriving.
- Fixed `services.yaml` selector value types for `set_buzzer_mode` (strings instead of integers).

### Notes
- `manifest.json` version bumped to `0.17.7`.

## v0.17.6 - 2026-04-26

### Fixed
- Reverted BLE response timeout reduction (10s → 30s) from v0.17.5 that caused all entities to become Unknown; the device requires the full 30s window to respond.
- Removed premature early-exit on mismatched BLE frames; the device legitimately sends `1/18` notification frames before sending the expected `1/19` response.
- The startup hang fix from v0.17.5 is retained: poll watchdog reduced from 300s to 60s.

### Notes
- `manifest.json` version bumped to `0.17.6`.

## v0.17.5 - 2026-04-26

### Fixed
- Fixed Home Assistant service schema parsing for `set_system_mode` by using string option values in `services.yaml`.
- Reduced SoC response timeout from 30s to 10s to avoid long stalls when the device does not return the expected response frame.
- Added early-exit handling when repeated mismatched BLE frames are received instead of the expected `1/19` response frame.
- Reduced coordinator poll watchdog timeout from 300s to 60s so stalled polls recover faster during startup.

### Notes
- `manifest.json` version bumped to `0.17.5`.

## v0.17.4 - 2026-04-25

### Fixed
- Added a coordinator poll watchdog (300s) that aborts an overlong poll and rebuilds BLE client state automatically.
- Prevented indefinitely stuck poll tasks from blocking all future updates until Home Assistant restart.

### Notes
- `manifest.json` version bumped to `0.17.4`.

## v0.17.3 - 2026-04-25

### Changed
- Increased BLE connection retry attempts from 4 to 8 to improve recovery on unreliable links.

### Notes
- `manifest.json` version bumped to `0.17.3`.

## v0.17.2 - 2026-04-25

### Fixed
- Reverted BLE response/diagnostic timeout reductions from v0.17.1 that caused all entities to become Unknown; the device genuinely requires the full original timeout window to respond.
- Retained the bounded disconnect helper so a hanging BLE disconnect still cannot block coordinator polling indefinitely.

### Notes
- `manifest.json` version bumped to `0.17.2`.

## v0.17.1 - 2026-04-25

### Fixed
- Reduced BLE polling stall time by lowering connection and response timeout budgets and reducing retry backoff.
- Added bounded disconnect handling so a hanging BLE disconnect cannot block coordinator polling for extended periods.

### Notes
- `manifest.json` version bumped to `0.17.1`.

## v0.17.0 - 2026-04-23

### Added
- Added support for APstorage ELS-5K (device ID prefix `215`).
- Added support for APstorage ELS-11.4 (device ID prefix `B040`).
- Device model is now dynamically detected based on Bluetooth MAC address prefix.

### Changed
- Manufacturer field now shows "APsystems" (previously "APstorage").
- Updated Bluetooth autodiscovery to support all three device models:
  - `PCS_B050*` for ELT-12
  - `PCS_B040*` for ELS-11.4
  - `PCS_215*` for ELS-5K

### Notes
- `manifest.json` version bumped to `0.17.0`.
- Bluetooth matchers now include patterns for all supported models.

### Removed
- Removed `Battery Current` sensor — DC battery current (`SI0`–`SI5`) is not available over the local BLE path.
- Removed `point` field filter from `getDeviceLastDataLocal` request (device ignores it locally).

### Notes
- `manifest.json` version bumped to `0.16.2`.

## v0.16.1 - 2026-04-22

### Fixed
- Added `point` field list to `getDeviceLastDataLocal` BLE request so the device returns per-module `SI0`–`SI5` current fields (and other fields it may otherwise omit).
- Fixed `SI#` extraction to handle both array and scalar response values.
- Expanded `point` list to include all primary field keys used by existing sensors (`GF`, `GV`, `P2`, `P3`, `BV`, `BUZ`, etc.) so existing sensors are not affected if the device filters on `point`.

### Notes
- `manifest.json` version bumped to `0.16.1`.

## v0.16.0 - 2026-04-22

### Added
- Added `Battery Current` sensor reporting aggregate DC battery current (A) summed across all active battery modules.
- Added extraction of per-module `SI0`–`SI5` current fields from `getDeviceLastDataLocal` BLE responses.

### Notes
- `manifest.json` version bumped to `0.16.0`.

## v0.15.0 - 2026-04-22

### Fixed
- Restored `PCS Hardware Version` so it no longer remains `Unknown` when firmware metadata is partially available.
- Continued version discovery retries until firmware current/latest and hardware version metadata are all populated.
- Added support for the `HV` hardware-version alias observed in app payloads.

### Notes
- `manifest.json` version bumped to `0.15.0`.

## v0.14.2 - 2026-04-22

### Changed
- "Valley Charge" and "Selling First" switches are now unavailable (greyed out) when Peak-Valley mode is not active.

### Notes
- `manifest.json` version bumped to `0.14.2`.

## v0.14.1 - 2026-04-22

### Fixed
- Cleared transient Blufi session state between BLE polls so a bad encrypted session no longer survives until Home Assistant restart.
- Rebuilt the APstorage BLE client after repeated no-metrics polls to recover from stalled protocol state without restarting Home Assistant.

### Notes
- `manifest.json` version bumped to `0.14.1`.

## v0.14.0 - 2026-04-21

### Fixed
- Restored the APstorage BLE client class structure and recovered the class-level query methods after a malformed section in `soc_client.py`.
- Restored service-discovery readiness checks before BLE characteristic access to avoid intermittent connection-time attribute errors.
- Improved long-run BLE reliability by adding a short post-security settle delay before encrypted local-data requests.
- Added a one-time retry for local-data frame `type=1/subtype=19` timeouts to recover from transient missed replies without requiring restart.

### Notes
- `manifest.json` version bumped to `0.14.0`.

## v0.13.5 - 2026-04-20

### Fixed
- Stopped aggressive repeated PCS firmware-version retries once any useful version metadata has been captured, reducing long-running BLE stalls.

### Notes
- manifest.json version bumped to 0.13.5.

## v0.13.4 - 2026-04-18

### Fixed
- Reduced the impact of firmware-version diagnostics on the main BLE telemetry poll.
- Stopped redundant version endpoint requests after the first useful response.
- Added shorter diagnostic timeouts so sensors no longer drift to Unknown during intermittent firmware lookups.

### Notes
- manifest.json version bumped to 0.13.4.

## v0.13.3 - 2026-04-18

### Fixed
- Stopped new BLE polls from being scheduled after the APstorage config entry is disabled or unloaded.
- Added a shutdown guard so stale advertisement or timer callbacks no longer trigger proxy connections.

### Notes
- manifest.json version bumped to 0.13.3.

## v0.13.2 - 2026-04-18

### Fixed
- Restored PCS firmware-version entities on payload variants that return stringified JSON and camelCase version keys.
- Retried version discovery more quickly when the first BLE version lookup returns no usable data.

### Notes
- manifest.json version bumped to 0.13.2.

## v0.13.1 - 2026-04-18

### Fixed
- Restored BLE polling reliability on setups where the PCS stopped replying after the Blufi handshake.
- Kept the notification subscription active through the secure request flow and aligned packet pacing with the known-good probe timing.

### Notes
- manifest.json version bumped to 0.13.1.

## v0.13.0 - 2026-04-16

### Added
- Added new diagnostic entities for Alarm Summary, PCS Alarm, and Battery Alarm.
- Added alarm parsing support for app-style storage, inverter, and ESS alarm payloads.

### Notes
- manifest.json version bumped to 0.13.0.

## v0.12.5 - 2026-04-16

### Changed
- Split underscore-delimited PCS firmware strings into separate entities for Firmware 1/2/3 and Latest Firmware 1/2/3.
- Simplified the diagnostic entity names by removing the word Version from the firmware labels.
- Removed the redundant PCS Software Version entity.

### Notes
- manifest.json version bumped to 0.12.5.

## v0.12.4 - 2026-04-16

### Fixed
- Improved PCS software-version detection by accepting additional app-observed fields and falling back to the reported firmware version when only one version value is available.
- Reduced the retry interval for version discovery so version entities populate sooner after reload.

### Notes
- manifest.json version bumped to 0.12.4.

## v0.12.3 - 2026-04-16

### Added
- Added PCS version diagnostics to expose firmware, latest firmware, software, and hardware version information.
- Populated Home Assistant device metadata with reported PCS software and hardware versions when available.

### Notes
- manifest.json version bumped to 0.12.3.

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
