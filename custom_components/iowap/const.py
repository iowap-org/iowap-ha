"""Constants for the IOWAP integration."""

DOMAIN = "iowap"

# App slug — everything core-side goes through this app (stdin, later states).
APP_SLUG = "iowap"

# Options keys mirrored into the app container (schema in iowap/config.yaml).
CONF_CAPABILITIES = "capabilities"  # v1: list of capability names enabled
CONF_RELAY_URL = "relay_url"