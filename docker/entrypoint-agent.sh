#!/bin/bash
# Entrypoint of an sd-agent container (experimental/sd-corner-cases): a logoscore
# daemon hosting libp2p_module alone -- kad-dht plus service discovery -- and
# openmetrics, then docker/sd_agent.py, which advertises, flaps and looks up
# services through libp2p_module's public API as the group's SD_* env says.
set -u
log() { echo "[harness $(hostname)] $*"; }

IP=$(hostname -i | awk '{print $1}')
P2P_PORT=${P2P_PORT:-45000}
METRICS_PORT=${METRICS_PORT:-9100}
BOOTSTRAP_ADDR=${BOOTSTRAP_ADDR:-}

# BOOTSTRAP_ADDR: comma-separated /ip4/../tcp/../p2p/<id> -> libp2p bootstrapNodes.
bn=""
IFS=, read -ra boots <<<"$BOOTSTRAP_ADDR"
for a in "${boots[@]}"; do
  [ -z "$a" ] && continue
  bn="$bn${bn:+,}{\"peerId\":\"${a##*/p2p/}\",\"addrs\":[\"${a%/p2p/*}\"]}"
done
privkey=""
[ -n "${LIBP2P_PRIVKEY:-}" ] && privkey=",\"privKey\":\"${LIBP2P_PRIVKEY}\""
conns=${P2P_MAX_CONNS:-100}
export LIBP2P_MODULE_CONFIG="{\"addrs\":[\"/ip4/${IP}/tcp/${P2P_PORT}\"],\"transport\":\"tcp\",\"maxConnections\":${conns},\"maxInConnections\":$((conns / 2)),\"maxOutConnections\":$((conns / 2)),\"mountGossipsub\":false,\"bootstrapNodes\":[${bn}]${privkey}}"
mkdir -p /data /traces

if [ "${START_JITTER:-0}" -gt 0 ]; then
  sleep $(( $(od -An -N2 -tu2 /dev/urandom) % START_JITTER ))
fi

log "sd-agent starting (group ${GROUP_NAME:-?} #${GROUP_INDEX:-?}, ip $IP, libp2p tcp $P2P_PORT, bootstrap ${BOOTSTRAP_ADDR:-none: this node starts the DHT})"
logoscore daemon -m /opt/modules --persistence-path /data &
DAEMON=$!
AGENT= WATCHDOG=
trap 'log "stopping (SIGTERM)"; [ -n "$AGENT" ] && kill $AGENT; [ -n "$WATCHDOG" ] && kill $WATCHDOG; kill -TERM $DAEMON' TERM

n=0
until logoscore list-modules >/dev/null 2>&1; do
  sleep 0.2; n=$((n + 1))
  if [ "$n" -gt 300 ]; then log "daemon never answered"; kill $DAEMON; exit 1; fi
done

ok() { # the CLI exits 0 on a module failure too: the reply decides
  ! grep -qiE '"?success"?[[:space:]]*[:=][[:space:]]*false|^error|failed' "$1"
}
retry() { # <label> <cmd...>: idempotent calls only
  local label=$1; shift; local i
  for i in 1 2 3; do
    if "$@" >/tmp/cli.out 2>&1 && ok /tmp/cli.out; then log "$label OK"; return 0; fi
    sleep 1
  done
  log "$label FAILED: $(tr '\n' ' ' </tmp/cli.out | cut -c1-300)"; return 1
}

retry "load-module libp2p_module" logoscore load-module libp2p_module || { kill $DAEMON; exit 1; }

# start exactly once: a start re-issued while the first is still bringing the
# switch up runs two accept loops (see libp2p-double-start-accept-crash). Its
# reply may time out while the bring-up continues, so readiness is judged by
# getNodeInfo afterwards, not by this reply.
t0=$(date +%s)
logoscore call libp2p_module start >/tmp/start.out 2>&1
log "libp2p start reply: $(tr '\n' ' ' </tmp/start.out | cut -c1-200)"
until logoscore call libp2p_module getNodeInfo PeerId --json >/tmp/pid.out 2>&1 && grep -q '"success":true' /tmp/pid.out \
      && logoscore call libp2p_module discoStart >/tmp/disco.out 2>&1 && ok /tmp/disco.out; do
  if [ $(( $(date +%s) - t0 )) -ge "${START_TIMEOUT:-180}" ]; then
    log "start FAILED: libp2p/discovery not up after ${START_TIMEOUT:-180}s: $(tr '\n' ' ' </tmp/disco.out | cut -c1-300)"
    kill $DAEMON; exit 1
  fi
  sleep 2
done
log "start OK ($(( $(date +%s) - t0 ))s) $(grep -oE '(16Uiu2|12D3KooW)[A-Za-z0-9]*' /tmp/pid.out | head -1)"

retry "load-module openmetrics" logoscore load-module openmetrics
retry "openmetrics start" logoscore call openmetrics start \
  "{\"port\":${METRICS_PORT},\"modules\":[{\"name\":\"libp2p_module\",\"format\":\"data\"}]}"

python3 -u /opt/sim/sd_agent.py 2>&1 | sed -u "s/^/[agent] /" &
AGENT=$!

watch_modules() { # as entrypoint-module.sh: a module host can die under a live daemon
  local lost=" " m st
  while sleep "${WATCHDOG_INTERVAL:-5}"; do
    st=$(logoscore status --json 2>/dev/null) || continue
    echo "$st" | grep -q '"daemon":{[^}]*"status":"running"' || continue
    for m in libp2p_module openmetrics; do
      case "$lost" in *" $m "*) continue ;; esac
      echo "$st" | grep -q "\"name\":\"$m\",\"status\":\"loaded\"" && continue
      lost="$lost$m "
      log "module lost: $m at $(date -u +%H:%M:%S)"
    done
  done
}
watch_modules &
WATCHDOG=$!

wait $DAEMON
wait $DAEMON 2>/dev/null
