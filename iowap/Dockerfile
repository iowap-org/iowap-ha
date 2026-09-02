ARG BUILD_FROM
FROM $BUILD_FROM

# curl: HTTP client for ha-exec (zero pip deps in the handler path)
RUN apk add --no-cache curl jq python3

# iowap-node (pinned) into a venv at build time, from the vendored wheel.
# 1) PEP 668 ("externally-managed-environment") blocks system-wide pip on
#    Alpine 3.20+ — a venv is the supported path.
# 2) iowap-node has no PyPI release; the wheel is vendored in iowap-ha
#    (iowap/vendor/) and must be refreshed when the framework bumps.
# 3) httpx is installed explicitly even though iowap-node already declares
#    it: stdin_listener.py (_notify, telemetry push) imports httpx directly,
#    and an implicit transitive dep would break at runtime (not build time)
#    if a future iowap-node wheel dropped it.
# /opt/venv/bin is prepended to PATH so `node-daemon`, `import nodes` and the
# framework deps resolve in every run.sh call.
COPY vendor/iowap_node-2.1.0-py3-none-any.whl /tmp/
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir /tmp/iowap_node-2.1.0-py3-none-any.whl \
    && /opt/venv/bin/pip install --no-cache-dir "httpx>=0.27" \
    && rm /tmp/iowap_node-2.1.0-py3-none-any.whl
ENV PATH="/opt/venv/bin:$PATH"

# T-169 reference handler + bootstrap helpers -> /usr/local/bin
COPY ha-exec.py /usr/local/bin/ha-exec.py
COPY stdin_listener.py /usr/local/bin/stdin_listener.py
COPY gen_profile.py /usr/local/bin/gen_profile.py

# Node config + runtime token live on /data (persistent app storage, survives
# app updates). run.sh exports HOME=/data so node_utils writes
# /data/.relay/iowap-agent.token (chmod 600) — registration secret and token
# survive reboots; no admin re-approval after container restart.
WORKDIR /data

COPY run.sh /
RUN chmod a+x /run.sh 2>/dev/null; chmod +x /run.sh
CMD [ "/run.sh" ]