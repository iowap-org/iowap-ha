"""Constants for the IOWAP integration."""

DOMAIN = "iowap"

# App slug — everything core-side goes through this app (stdin, later states).
# NOTE: local app repos get a `<hash>_` prefix in HAOS (observed: 16b71320_iowap),
# so the bare repo name is NOT the runtime slug. The slug is resolved at setup
# time from the device registry (identifier domain "hassio"); this constant is
# only a fallback match pattern for the registry lookup.
APP_SLUG = "iowap"

# Options keys mirrored into the app container (schema in iowap/config.yaml).
CONF_CAPABILITIES = "capabilities"  # v1: list of capability names enabled
CONF_RELAY_URL = "relay_url"