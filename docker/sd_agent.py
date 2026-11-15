#!/usr/bin/env python3
"""sd-agent driver (experimental/sd-corner-cases).

Drives libp2p_module's service discovery through `logoscore call`, as the
group's environment says, and writes one JSON line per event to
/traces/<host>.sd.jsonl:

  self    {peerId, ip}                       once, at start
  adv     {svc, ok, ms, err}                 discoStartAdvertising
  unadv   {svc, ok, ms, err}                 discoStopAdvertising
  lookup  {svc, ok, ms, n, peers, err}       discoLookup; peers = peer ids returned

Environment ({i} = the node's index in its group, {host} = its hostname):
  SD_ADVERTISE         comma list, advertised once at start
  SD_FLAP              <svc>:<on_s>:<off_s>  advertise, stop, repeat
  SD_LOOKUP            comma list looked up every cycle
  SD_LOOKUP_ROTATE     comma list looked up in rotation; prefix[a-b] expands
  SD_LOOKUP_PER_CYCLE  rotating ids per cycle (default 4)
  SD_LOOKUP_INTERVAL   seconds per cycle (default 20)
  SD_LOOKUP_START      seconds before the first cycle (default 0)
  SD_LOOKUP_PARALLEL   1 (default): a cycle's lookups run concurrently; 0: one by one
"""
import json
import os
import re
import socket
import subprocess
import threading
import time

HOST = socket.gethostname()
OUT = f"/traces/{HOST}.sd.jsonl"
IDX = os.environ.get("GROUP_INDEX", "1")
# SD_LOOKUP_PARALLEL=0: one CLI call at a time (a crash isolation knob).
PARALLEL = os.environ.get("SD_LOOKUP_PARALLEL", "1") != "0"
_lock = threading.Lock()
_PEER = re.compile(r"(?:16Uiu2|12D3KooW)[1-9A-HJ-NP-Za-km-z]{40,}")


def emit(ev, **kw):
    rec = {"ts": round(time.time(), 3), "node": HOST, "ev": ev, **kw}
    with _lock, open(OUT, "a") as f:
        f.write(json.dumps(rec) + "\n")


def expand(spec):
    out = []
    for item in (s.strip() for s in spec.split(",")):
        if not item:
            continue
        item = item.replace("{i}", IDX).replace("{host}", HOST)
        m = re.fullmatch(r"(.*)\[(\d+)-(\d+)\](.*)", item)
        if m:
            out += [f"{m[1]}{k}{m[4]}" for k in range(int(m[2]), int(m[3]) + 1)]
        else:
            out.append(item)
    return out


def call(method, *args, timeout=90):
    """(ok, ms, result-or-None, err). The CLI exits 0 on a module failure, so
    the JSON body decides."""
    t = time.time()
    try:
        p = subprocess.run(
            ["logoscore", "call", "libp2p_module", method, *args, "--json"],
            capture_output=True, text=True, timeout=timeout,
        )
        out = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return False, round((time.time() - t) * 1000), None, "cli-timeout"
    ms = round((time.time() - t) * 1000)
    body = None
    i = out.find("{")
    if i >= 0:
        try:
            body = json.loads(out[i:])
        except ValueError:
            body = None
    # {"method","module","result":{"error","success","value"},"status"}
    res = body.get("result") if isinstance(body, dict) else None
    if not isinstance(res, dict):
        return False, ms, None, out.strip()[:300] or f"rc={p.returncode}"
    if res.get("success") is not True:
        return False, ms, res, str(res.get("error") or body)[:300]
    return True, ms, res.get("value"), ""


def advertise(svc, attempts=10):
    for _ in range(attempts):
        ok, ms, _, err = call("discoStartAdvertising", svc, "", "")
        emit("adv", svc=svc, ok=ok, ms=ms, err=err)
        if ok:
            return True
        time.sleep(3)
    return False


def unadvertise(svc):
    ok, ms, _, err = call("discoStopAdvertising", svc)
    emit("unadv", svc=svc, ok=ok, ms=ms, err=err)


def lookup(svc):
    ok, ms, body, err = call("discoLookup", svc, "")
    peers = []
    if ok and isinstance(body, list):
        peers = sorted({r.get("peerId") for r in body if isinstance(r, dict) and r.get("peerId")})
    emit("lookup", svc=svc, ok=ok, ms=ms, n=len(peers), peers=peers, err=err)


def flap(spec):
    svc, on, off = spec.rsplit(":", 2)
    while True:
        if advertise(svc, attempts=1):
            time.sleep(float(on))
            unadvertise(svc)
        time.sleep(float(off))


def lookups():
    fixed = expand(os.environ.get("SD_LOOKUP", ""))
    rotate = expand(os.environ.get("SD_LOOKUP_ROTATE", ""))
    per = int(os.environ.get("SD_LOOKUP_PER_CYCLE", "4"))
    interval = float(os.environ.get("SD_LOOKUP_INTERVAL", "20"))
    if not (fixed or rotate):
        return
    time.sleep(float(os.environ.get("SD_LOOKUP_START", "0")))
    # Lookers start at different points of the rotation, so between them every
    # id is looked up about every len(rotate) / (lookers * per) cycles.
    pos = ((int(IDX) - 1) * per) % len(rotate) if rotate else 0
    while True:
        start = time.time()
        batch = list(fixed)
        for _ in range(min(per, len(rotate))):
            batch.append(rotate[pos])
            pos = (pos + 1) % len(rotate)
        if PARALLEL:
            threads = [threading.Thread(target=lookup, args=(s,), daemon=True) for s in batch]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=interval * 3)
        else:
            for s in batch:
                lookup(s)
        time.sleep(max(0.0, interval - (time.time() - start)))


def main():
    ok, _, body, err = call("getNodeInfo", "PeerId")
    pid = _PEER.findall(str(body)) if ok else []
    emit("self", peerId=pid[0] if pid else None, ip=socket.gethostbyname(HOST),
         group=os.environ.get("GROUP_NAME"), index=int(IDX), err=err)
    workers = []
    adv = expand(os.environ.get("SD_ADVERTISE", ""))
    # Advertise in parallel: a hub's 40-odd services would otherwise trickle in
    # one CLI round trip at a time.
    for svc in adv:
        workers.append(threading.Thread(target=advertise, args=(svc,), daemon=True))
    if os.environ.get("SD_FLAP"):
        workers.append(threading.Thread(target=flap, args=(os.environ["SD_FLAP"],), daemon=True))
    workers.append(threading.Thread(target=lookups, daemon=True))
    for w in workers:
        w.start()
    print(f"advertising {len(adv)} service(s), flap={os.environ.get('SD_FLAP', '-')}, "
          f"lookup={os.environ.get('SD_LOOKUP', '-')} rotate={len(expand(os.environ.get('SD_LOOKUP_ROTATE', '')))}",
          flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
