# Debug Guide: Version 0.23.9-debug DH Handshake Logging

## Overview
This debug build (`0.23.9-debug`) adds comprehensive logging to track why the one-shot DH handshake in the deferred version probe times out with `observed=['none']` (no frames captured).

## Key Logging Points Added

### BLE Connection Phase
- `[BLE] Starting one-shot BLE connection for version probe` - marks start
- `[BLE] One-shot BLE connection established in X.Xs` - shows connection duration
- This helps identify if `establish_connection()` is slow or hanging

### DH Handshake Phase
- `[BLE] _establish_blufi_session: Starting DH handshake` - marks start
- `[BLE] _establish_blufi_session: Blufi state reset; parsed_frames cleared` - confirms state reset
- `[BLE] _establish_blufi_session: Protocol profile selected: PCS` - confirms profile picked
- This helps identify if state setup is the issue

### Notification Registration
- `[BLE] Registering notification callback for DH handshake` - before registration
- `[BLE] Notification callback registered; waiting for settle delay` - after success
- `[BLE] Failed to register notification callback: <error>` - if it fails
- This identifies if notification subscription itself is the problem

### DH Packet Exchange
- `[BLE] Sending DH negotiation packets` - marks when packets are sent
- This helps confirm packets are being written to device

### Frame Reception and Parsing
- `[BLE] Waiting for DH response frame (1,0) with 30.0s timeout` - marks wait start
- `[BLE] Notification received: <N> bytes` - logs each notification callback invocation
- `[BLE] Frame parsed: type=<T> subtype=<S> payload=<N> bytes` - successful parse
- `[BLE] Parse returned None for <N> byte notification` - parser returned None
- `[BLE] Exception parsing notification: <error>` - parsing raised exception
- `[BLE] _wait_frame: timeout after 30.0s; total frames collected: <N>; observed: [<frames>]` - final timeout

### Success/Completion
- `[BLE] DH response frame received: <N> bytes` - after frame received
- `[BLE] Sending security setup packet` - after DH response processed
- `[BLE] _establish_blufi_session: DH handshake complete` - marks completion

### Timing Summary (from coordinator)
- `[BLE] Starting Blufi DH handshake for version probe` - marks start
- `[BLE] Blufi DH handshake completed in X.Xs` - shows total duration
- This shows if DH itself is taking too long (should be <35s, not 68s)

## Collecting Logs

### In Home Assistant Web UI:
1. Settings → System → Logs
2. Set filter: `[BLE]` to show only Bluetooth debug logs
3. Optionally set logger to `DEBUG` level for more detail

### Via configuration.yaml (for all logs):
```yaml
logger:
  default: info
  logs:
    homeassistant.components.apstorage_ble: debug
```

### Via Home Assistant UI:
1. Settings → Developer Tools → Services
2. Service: `logger.set_level`
3. Data: 
   ```yaml
   logger: homeassistant.components.apstorage_ble
   level: DEBUG
   ```

## Interpreting Results

### Expected Success Path (Persistent Session)
```
[BLE] Starting DH handshake
[BLE] Blufi state reset; parsed_frames cleared
[BLE] Protocol profile selected: PCS
[BLE] Registering notification callback for DH handshake
[BLE] Notification callback registered; waiting for settle delay
[BLE] Sending DH negotiation packets
[BLE] Waiting for DH response frame (1,0) with 30.0s timeout
[BLE] Notification received: <N> bytes     # <-- Callback was invoked!
[BLE] Frame parsed: type=1 subtype=0 payload=<N> bytes  # <-- Frame parsed!
[BLE] DH response frame received: <N> bytes
[BLE] _wait_frame: timeout after X.Xs; total frames collected: 1; observed: ['1/0']  # Should say 1 frame, not 0
[BLE] Sending security setup packet
[BLE] _establish_blufi_session: DH handshake complete
```

### Problem Scenarios

#### Scenario A: Notifications Never Registered
```
[BLE] Registering notification callback for DH handshake
[BLE] Failed to register notification callback: <error message>
```
**Action**: Check error message - might be permission/hardware issue

#### Scenario B: Notifications Registered but Never Invoked
```
[BLE] Notification callback registered; waiting for settle delay
[BLE] Sending DH negotiation packets
[BLE] Waiting for DH response frame (1,0) with 30.0s timeout
# ... 30 seconds later ...
[BLE] _wait_frame: timeout after 30.0s; total frames collected: 0; observed: ['none']
```
**Symptoms**: No `[BLE] Notification received` messages
**Likely Cause**: Device not sending responses, or connection dropped silently

#### Scenario C: Notifications Received but Parsing Fails
```
[BLE] Notification received: <N> bytes
[BLE] Exception parsing notification: Encrypted notify received but AES key is not set
```
**Symptoms**: Exception instead of parsed frame
**Likely Cause**: DH response is encrypted, but we're parsing with `session_key=None`

#### Scenario D: Notifications Received, Parsing Works, But Wrong Frame Type
```
[BLE] Notification received: <N> bytes
[BLE] Frame parsed: type=0 subtype=1 payload=<N> bytes
# ... repeats ...
[BLE] _wait_frame: timeout after 30.0s; total frames collected: 2; observed: ['0/1', '0/1']
```
**Symptoms**: Frames being received but not matching type=1 subtype=0
**Likely Cause**: Device sending different response type than expected

#### Scenario E: Connection Slow or Hanging (68s total)
```
[BLE] Starting one-shot BLE connection for version probe
# ... 40-50 seconds elapse ...
[BLE] One-shot BLE connection established in 45.0s
```
**Symptoms**: Connection takes way longer than expected
**Likely Cause**: BLE adapter timeout or proxy connection issues

## Steps for User

1. **Update integration** to v0.23.9-debug
2. **Enable DEBUG logging** (see "Collecting Logs" section)
3. **Restart Home Assistant** to reload integration
4. **Wait for first poll** after startup (or restart to trigger immediately)
5. **Capture all `[BLE]` prefixed log lines** from the deferred version probe (should appear ~30-70 seconds after first successful telemetry poll)
6. **Provide logs** so we can analyze which scenario matches

## Next Steps Based on Logs

Once we see the logs, we can:
- If (A): Debug notification permission/registration issue
- If (B): Check if device is responding, or if one-shot connection drops
- If (C): Understand why DH response is encrypted when it shouldn't be
- If (D): Adjust frame type/subtype expectations
- If (E): Investigate BLE adapter or proxy connection timing

## Questions to Help Interpret

When sharing logs, please also provide:
1. Does telemetry polling work consistently? (Check earlier log for SoC/power readings)
2. Did you restart HA or is this after some uptime?
3. Any errors in HA logs before the deferred probe timeout?
4. Is the device visible in HA Settings → Devices & Services → Bluetooth during the probe?
