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

# T-173: per-domain exposure modes (uniform 3-state model). Keys must match
# the app-side whitelist in iowap/stdin_listener.py (DOMAIN_MAP_KEYS) and the
# domains in iowap/ha-exec.py CAPS. Default everywhere: "readonly" (T-169
# status quo) — the app enforces the default even if the integration sends
# nothing at all.
CONF_DOMAIN_STATES = "domain_states"
DOMAIN_STATES_LIST = [
    "light", "scene", "climate", "media_player", "switch",
    "fan", "humidifier", "vacuum", "lock",
]
DOMAIN_STATE_OPTIONS = {
    "on": "On — full control (writes + reads)",
    "readonly": "Read-only — state reads only",
    "off": "Off — hidden from IOWAP entirely",
}