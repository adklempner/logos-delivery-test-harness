# Sourced by every entrypoint that runs delivery_module (delivery-module and
# delivery-demo nodes): the node's addresses, its libp2p_module config, its
# discovery trace, and the node config rendered from the group's profile into
# /data/member.json. One copy, so a demo node joins exactly like a module member.
IP=$(hostname -i | awk '{print $1}')
P2P_PORT=${P2P_PORT:-45000}
TCP_PORT=${TCP_PORT:-44000}
METRICS_PORT=${METRICS_PORT:-9100}
MESH_CONTENT_TOPIC=${MESH_CONTENT_TOPIC:-/sim/1/mesh/proto}
LOOKUP=${LOOKUP_INTERVAL:-15}
CLUSTER_ID=${CLUSTER_ID:-42}
NUM_SHARDS=${NUM_SHARDS:-1}
MEMBER_CONFIG=${MEMBER_CONFIG:-member.json.tpl}
# The DHT peer to join through (/ip4/../tcp/../p2p/..). Empty for the first
# bootstrap, which is where the DHT starts: libp2p then takes no bootstrap peer.
BOOTSTRAP_ADDR=${BOOTSTRAP_ADDR:-}

# libp2p_module defaults to /ip4/127.0.0.1/tcp/0; bind the container address so
# the records it publishes are dialable from the other containers.
# A bootstrap also gets a fixed key (LIBP2P_PRIVKEY), so the DHT address its
# members join through is known before it starts.
privkey=""
[ -n "${LIBP2P_PRIVKEY:-}" ] && privkey=",\"privKey\":\"${LIBP2P_PRIVKEY}\""
export LIBP2P_MODULE_CONFIG="{\"addrs\":[\"/ip4/${IP}/tcp/${P2P_PORT}\"],\"transport\":\"tcp\",\"maxConnections\":${P2P_MAX_CONNS:-100},\"maxInConnections\":$(( ${P2P_MAX_CONNS:-100} / 2 )),\"maxOutConnections\":$(( ${P2P_MAX_CONNS:-100} / 2 ))${privkey}}"
export LD_DISCO_TRACE=${LD_DISCO_TRACE:-/traces/$(hostname).trace}
mkdir -p /data "$(dirname "$LD_DISCO_TRACE")"

tpl=/opt/sim/conf/$MEMBER_CONFIG
[ -f "$tpl" ] || { log "no such node profile: $MEMBER_CONFIG"; exit 1; }
bootstraps=""
# BOOTSTRAP_ADDR may list several, comma-separated: each becomes a JSON string.
[ -n "$BOOTSTRAP_ADDR" ] && bootstraps="\"${BOOTSTRAP_ADDR//,/\",\"}\""
# MIXNODES: the manifest's fixed-key mix nodes (<multiaddr>:<mix pubkey>,...).
mixnodes=""
[ -n "${MIXNODES:-}" ] && mixnodes="\"${MIXNODES//,/\",\"}\""
# MIX_ALLOW_ALL=1: let mix use private addresses, which this network is made of.
# Only for a logos-delivery that has the option (logos-delivery#4631, #4729);
# an older one refuses the unknown key.
mixallow=""
[ "${MIX_ALLOW_ALL:-0}" = 1 ] && mixallow='"mix-allow-all-addresses": true,'
# RLN=1: Relay RLN on the registry the lez-setup service deployed on the local
# LEZ chain (a manifest with an "rln" section). RLN has no config key: it comes
# from the presets file, whose one entry replaces the module's built-in settings
# for this node's preset -- "" for a profile that names none. The daemon reads
# LOGOS_DELIVERY_RLN_PRESETS and LEZ_RLN_SEQUENCER, so both are set before it starts.
rlnregistry=""
if [ "${RLN:-0}" = 1 ]; then
  n=0
  until [ -s /lez/registry-id ]; do
    sleep 1; n=$((n + 1))
    if [ "$n" -ge "${RLN_SETUP_TIMEOUT:-600}" ]; then
      log "RLN FAILED: no registry from lez-setup within ${RLN_SETUP_TIMEOUT:-600}s"; exit 1
    fi
  done
  rlnregistry=$(cat /lez/registry-id)
  validate=true; [ "${RLN_VALIDATE:-1}" = 1 ] || validate=false
  export LOGOS_DELIVERY_RLN_PRESETS=/data/rln-presets.json
  printf '{"%s": {"enabled": true, "registry-id": "%s", "epoch-size-sec": %s, "enable-validation": %s}}\n' \
    "${RLN_PRESET:-}" "$rlnregistry" "${RLN_EPOCH:-10}" "$validate" > "$LOGOS_DELIVERY_RLN_PRESETS"
fi
sed -e "s|@IP@|$IP|g" -e "s|@BOOTSTRAPS@|$bootstraps|g" -e "s|@LOOKUP@|$LOOKUP|g" \
    -e "s|@MIX_ALLOW_ALL@|$mixallow|g" -e "s|@RLN_REGISTRY@|$rlnregistry|g" \
    -e "s|@MIXNODES@|$mixnodes|g" -e "s|@ANONYMITY@|${ANONYMITY_LEVEL:-Preferred}|g" \
    -e "s|@CLUSTER@|$CLUSTER_ID|g" -e "s|@SHARDS@|$NUM_SHARDS|g" \
    -e "s|@TCP_PORT@|$TCP_PORT|g" \
    "$tpl" > /data/member.json

# Spread simultaneous replica starts a little: N daemons loading plugins at the
# same instant trip logos-core's 10 s "plugin never reported loading" timeout.
if [ "${START_JITTER:-0}" -gt 0 ]; then
  sleep $(( $(od -An -N2 -tu2 /dev/urandom) % START_JITTER ))
fi
