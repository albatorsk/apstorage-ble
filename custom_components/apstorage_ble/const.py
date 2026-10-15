"""Constants for the APstorage BLE integration."""

DOMAIN = "apstorage_ble"

MANUFACTURER = "APsystems"


def get_model(name: str | None) -> str:
    """Return the model name based on the device's Bluetooth broadcast name.

    Args:
        name: BLE advertised local name (e.g. "PCS_B050XXXXXXXX"), or None if
            the name is not known.

    Returns:
        Model name string based on the broadcast name prefix.
    """
    name_clean = (name or "").strip().upper()

    if name_clean.startswith("PCS_215"):
        return "APstorage ELS-5K"
    elif name_clean.startswith("PCS_B040"):
        return "APstorage ELS-11.4"
    elif name_clean.startswith("PCS_B050"):
        return "APstorage ELT-12"
    else:
        # Default to ELT-12 for unknown devices
        return "APstorage ELT-12"

# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------
# How often (seconds) to poll the PCS. Each poll is one short BLE connection.
POLL_INTERVAL_SECONDS = 30
POLL_INTERVAL_MIN_SECONDS = 10
POLL_INTERVAL_MAX_SECONDS = 300

# ---------------------------------------------------------------------------
# Config-entry keys
# ---------------------------------------------------------------------------
CONF_ADDRESS = "address"
# Bluetooth broadcast (advertised local) name, used to determine the model
CONF_BLE_NAME = "ble_name"
CONF_POLL_INTERVAL_SECONDS = "poll_interval_seconds"
