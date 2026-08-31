#!/usr/bin/env bashio
# IOWAP Node app entrypoint.
# 1. Waits for the HA core API (Supervisor proxy) to come up.
# 2. Bootstraps relay_config.json + registration (via registration secret from
#    the app options / data dir) if no token exists yet.
# 3. Starts the node-daemon (heartbeat/claim loops, handler_runner) AND the
#    stdin-listener (hassio.app_stdin consumer → /data/outbox.jsonl → relay).
#    ha-exec.py is registered as the handler for the ha.* capabilities.

set -e

CONFIG_DIR=/data/.relay
mkdir -p "$CONFIG_DIR"
export HOME=/data

log() { echo "[iowap] $*"; }

# --- wait for HA core API -------------------------------------------------
wait_for_ha() {
    local i=0
    until curl -sS -m 5 -o /dev/null \
        -H "Authorization: Bearer ${SUPERVISOR_TOKEN}" \
        http://supervisor/core/api/config >/dev/null 2>&1; do
        i=$((i + 1))
        if [ "$i" -gt 60 ]; then
            log "HA core API not reachable after 5 min, giving up"
            exit 1
        fi
        sleep 5
    done
    log "HA core API reachable"
}

wait_for_ha

# 3. relay config: write default if missing (daemon + CLI read it)
if [ ! -f "$CONFIG_DIR/relay_config.json" ]; then
    RELAY_URL=$(jq -r '.relay_url // empty' /data/options.json 2>/dev/null || true)
    if [ -z "$RELAY_URL" ]; then
        log "ERROR: no relay_url configured (set it in the app options)"
        exit 1
    fi
    jq -n --arg u "$RELAY_URL" \
        '{base_url: $arg, heartbeat_interval: 8, claim_interval: 5,
          status_interval: 7200, request_timeout: 10, task_timeout: 600,
          load_cap: 1.0, log_level: "INFO"}' > "$CONFIG_DIR/relay_config.json"
    log "relay config written (base_url=$RELAY_URL)"
fi

# 4. pip-pinned node framework (T-170 P2: version pinned, not floating)
if ! python3 -c "import nodes" 2>/dev/null; then
    log "installing iowap-node==2.0.0 (pinned)"
    pip3 install --no-cache-dir "iowap-node==2.0.0" \
        || { log "pip install of iowap-node failed"; exit 1; }
fi

# 5. handlers: ha-exec.py is the security boundary (capability table inside).
#    Installed where the node profile's handler table points at it.
mkdir -p /data/handlers
install -m 0755 /usr/local/bin/ha-exec.py /data/handlers/ha-exec.py

# 6. node profile: generate node.yaml from ha-exec.py's CAPS table (single
#    source of truth — the published list can't drift from the security
#    boundary). Regenerated every boot; daemon publishes it on registration
#    and via publish-diff at runtime.
mkdir -p "$CONFIG_DIR"
python3 /usr/local/bin/ha-exec.py --caps-json \
    | python3 /usr/local/bin/gen_profile.py > "$CONFIG_DIR/node.yaml"
log "node profile generated ($(grep -c 'name:' "$CONFIG_DIR/node.yaml") capabilities)"

# 7. node-daemon (bg) + stdin-listener (fg, PID 1 via exec). hassio.app_stdin
#    targets THIS container's stdin, so the listener must be the process
#    holding it. The listener registers the node on first boot (pending until
#    admin approval); the daemon heartbeats regardless. Supervisor restarts
#    the whole app if the listener exits; on stop, SIGTERM ends the listener
#    and container teardown reaps the daemon.
log "starting node-daemon (WorkingDir=$CONFIG_DIR)"
cd "$CONFIG_DIR"
node-daemon --foreground &
DAEMON_PID=$!

shutdown() {
    kill "$DAEMON_PID" 2>/dev/null || true
}
trap shutdown TERM INT

exec python3 /usr/local/bin/stdin_listener.py