"""Constants for the APstorage BLE integration."""

DOMAIN = "apstorage_ble"

MANUFACTURER = "APsystems"


def get_model(address: str) -> str:
    """Return the model name based on the device address.
    
    Args:
        address: Bluetooth MAC address (format: AA:BB:CC:DD:EE:FF or AABBCCDDEEFF)
    
    Returns:
        Model name string based on the device ID prefix.
    """
    # Extract the device prefix from the MAC address (first two bytes after colon removal)
    mac_clean = address.replace(":", "").upper()
    
    if mac_clean.startswith("215"):
        return "APstorage ELS-5K"
    elif mac_clean.startswith("B040"):
        return "APstorage ELS-11.4"
    elif mac_clean.startswith("B050"):
        return "APstorage ELT-12"
    else:
        # Default to ELT-12 for unknown devices
        return "APstorage ELT-12"

# ---------------------------------------------------------------------------
# GATT UUIDs — UPDATE THESE FOR YOUR DEVICE
# ---------------------------------------------------------------------------
# Many Chinese inverter/PCS devices use Nordic UART Service (NUS) as their
# BLE transport. Verify these in your environment (e.g., in Wireshark or
# the nRF Connect app) before trusting any parsed data.
#
# To find the correct UUIDs:
#   1. Connect to the device with nRF Connect or similar
#   2. Browse GATT services and note which characteristics have Write and
#      Notify properties
#   3. Replace the values below with the actual UUIDs
# ---------------------------------------------------------------------------

# Characteristic to WRITE requests to (write-with-response)
BLE_WRITE_CHAR_UUID = "0000ff07-0000-1000-8000-00805f9b34fb"

# Characteristic to subscribe to for NOTIFICATIONS (read + notify)
BLE_NOTIFY_CHAR_UUID = "0000ff06-0000-1000-8000-00805f9b34fb"

# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------
# How often (seconds) to poll the device via an active GATT connection when
# its advertisement is seen.  30 s is a sensible default for a storage system.
POLL_INTERVAL_SECONDS = 30
POLL_INTERVAL_MIN_SECONDS = 10
POLL_INTERVAL_MAX_SECONDS = 300

# Maximum seconds to wait for a response notification after sending a request
RESPONSE_TIMEOUT_SECONDS = 10

# ---------------------------------------------------------------------------
# Config-entry keys
# ---------------------------------------------------------------------------
CONF_ADDRESS = "address"
CONF_POLL_INTERVAL_SECONDS = "poll_interval_seconds"
