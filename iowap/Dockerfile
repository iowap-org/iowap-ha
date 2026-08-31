ARG BUILD_FROM
FROM $BUILD_FROM

# curl: HTTP client for ha-exec (zero pip deps in the handler path)
RUN apk add --no-cache curl jq python3 py3-pip

# T-169 reference handler + bootstrap helpers -> /usr/local/bin
COPY ha-exec.py /usr/local/bin/ha-exec.py
COPY stdin_listener.py /usr/local/bin/stdin_listener.py
COPY gen_profile.py /usr/local/bin/gen_profile.py

# iowap-node is installed on first start (pinned in run.sh) to keep the
# base image generic; see T-170/P2. Node config lives on /data (survives
# app updates).
WORKDIR /data

COPY run.sh /
RUN chmod a+x /run.sh 2>/dev/null; chmod +x /run.sh
CMD [ "/run.sh" ]