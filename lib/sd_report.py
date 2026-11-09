#!/usr/bin/env python3
"""Service-discovery report over the sd-agents' event logs (experimental/sd-corner-cases).

usage: sd_report.py <collect-dir> [window-seconds]

Reads <dir>/*.sd.jsonl (docker/sd_agent.py), plan.json and t0. Times are
seconds from T0. A (provider, service) pair is "found" when a lookup for the
service returns the provider's peer id.
"""
import collections
import glob
import json
import os
import statistics
import sys

d = sys.argv[1]
W = int(sys.argv[2]) if len(sys.argv) > 2 else 300

plan = json.load(open(os.path.join(d, "plan.json")))
t0 = float(open(os.path.join(d, "t0")).read().strip())
group_of = {s: g["name"] for g in plan for s in g["services"]}
gplan = {g["name"]: g for g in plan}
stop_at = {s: g["stopAfter"] for g in plan if "stopAfter" in g for s in g["services"]}

# SD_EXCLUDE=m1,m4,...: drop these nodes entirely (e.g. nodes isolated from the DHT).
EXCLUDE = set(filter(None, os.environ.get("SD_EXCLUDE", "").split(",")))
ev = []
for f in sorted(glob.glob(os.path.join(d, "*.sd.jsonl"))):
    if os.path.basename(f).split(".")[0] in EXCLUDE:
        continue
    for line in open(f):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        e["t"] = e["ts"] - t0
        ev.append(e)
ev.sort(key=lambda e: e["t"])

peer_node = {e["peerId"]: e["node"] for e in ev if e["ev"] == "self" and e.get("peerId")}
node_peer = {v: k for k, v in peer_node.items()}
lookups = [e for e in ev if e["ev"] == "lookup"]
by_svc = collections.defaultdict(list)
for e in lookups:
    by_svc[e["svc"]].append(e)

# Advertised intervals per (node, svc): [start, end) from adv ok / unadv / stop.
spans = collections.defaultdict(list)
open_ = {}
for e in ev:
    k = (e["node"], e.get("svc"))
    if e["ev"] == "adv" and e["ok"] and k not in open_:
        open_[k] = e["t"]
    elif e["ev"] == "unadv" and k in open_:
        spans[k].append((open_.pop(k), e["t"]))
for k, s in open_.items():
    spans[k].append((s, stop_at.get(k[0], float("inf"))))
first_adv = {k: v[0][0] for k, v in spans.items()}
flappers = {n for (n, _), v in spans.items() if len(v) > 1}


def pct(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def fmt(x):
    return "-" if x != x else f"{x:.0f}"


def mmss(x):
    return "-" if x != x or x == float("inf") else f"{int(x) // 60}:{int(x) % 60:02d}"


def table(rows, head):
    w = [max(len(str(r[i])) for r in [head] + rows) for i in range(len(head))]
    out = ["  ".join(str(h).ljust(w[i]) for i, h in enumerate(head))]
    out += ["  ".join(str(c).ljust(w[i]) for i, c in enumerate(r)) for r in rows]
    return "\n".join(out)


print(f"sd report: {d}" + (f"  (excluding {len(EXCLUDE)} nodes: {','.join(sorted(EXCLUDE))})" if EXCLUDE else ""))
print(f"T0 {t0:.0f}; {len(peer_node)} agents reported a peer id; {len(lookups)} lookups; "
      f"{len(spans)} advertised (node, service) pairs; {len(flappers)} flapper(s)\n")

# 1. Lookups per window: latency and failures.
print(f"== lookups per {W // 60}-min window (all services)")
rows = []
end = max((e["t"] for e in ev), default=0)
for w0 in range(0, int(end) + 1, W):
    xs = [e for e in lookups if w0 <= e["t"] < w0 + W]
    if not xs:
        continue
    ok = [e for e in xs if e["ok"]]
    ms = [e["ms"] for e in ok]
    errs = collections.Counter(e["err"][:40] for e in xs if not e["ok"])
    rows.append([f"{mmss(w0)}-{mmss(w0 + W)}", len(xs), len(xs) - len(ok),
                 fmt(pct(ms, 50)), fmt(pct(ms, 95)), fmt(max(ms) if ms else float("nan")),
                 sum(1 for e in ok if e["n"] == 0),
                 "; ".join(f"{k}x{v}" for k, v in errs.most_common(2))])
print(table(rows, ["window", "lookups", "failed", "p50ms", "p95ms", "maxms", "empty", "errors"]))
print()


def found_times(node, svc):
    pid = node_peer.get(node)
    return [e["t"] for e in by_svc.get(svc, []) if e["ok"] and pid in e["peers"]]


# 2. Time-to-findable per advertiser group.
print("== time-to-findable: first lookup returning the provider, from its first successful advertise")
print("   (lookups0 = lookups of the service in that span that missed it)")
rows = []
ttf_by_group = collections.defaultdict(list)
never = collections.defaultdict(list)
for (node, svc), s in sorted(spans.items()):
    if node in flappers:
        continue
    start = s[0][0]
    hits = [t for t in found_times(node, svc) if t >= start]
    asked = [e for e in by_svc.get(svc, []) if e["t"] >= start]
    g = group_of.get(node, "?")
    if hits:
        ttf_by_group[(g, svc.split("-")[0] if g == "hub" else "")].append(hits[0] - start)
    elif asked:
        never[(g, svc.split("-")[0] if g == "hub" else "")].append(f"{node}:{svc}")
for key in sorted(set(ttf_by_group) | set(never)):
    xs = ttf_by_group.get(key, [])
    label = key[0] + (f" ({key[1]}*)" if key[1] else "")
    rows.append([label, len(xs), len(never.get(key, [])), mmss(pct(xs, 0)), mmss(pct(xs, 50)),
                 mmss(pct(xs, 90)), mmss(max(xs) if xs else float("nan"))])
print(table(rows, ["group", "found", "never", "min", "median", "p90", "max"]))
for key, v in sorted(never.items()):
    print(f"   never found ({key[0]}): {' '.join(v[:12])}{' ...' if len(v) > 12 else ''}")
print()

providers = collections.defaultdict(set)
for (node, svc) in spans:
    providers[svc].add(node)

# 2a. Per service family (id without its trailing -N): time from the service's
#     earliest advertise to the first non-empty lookup, any provider.
print("== time-to-findable per service family, any provider (from the earliest advertise of the service)")
fam = collections.defaultdict(list)
fam_never = collections.defaultdict(list)
for svc, provs in providers.items():
    if svc not in by_svc:
        continue
    start = min(first_adv[(n, svc)] for n in provs)
    pids = {node_peer.get(n) for n in provs}
    hits = [e["t"] for e in by_svc[svc] if e["ok"] and e["t"] >= start and pids & set(e["peers"])]
    f = svc.rsplit("-", 1)[0] if svc.rsplit("-", 1)[-1].isdigit() else svc
    if hits:
        fam[f].append(hits[0] - start)
    else:
        fam_never[f].append(svc)
rows = []
for f in sorted(set(fam) | set(fam_never)):
    xs = fam.get(f, [])
    rows.append([f, len(xs), len(fam_never.get(f, [])), mmss(pct(xs, 0)), mmss(pct(xs, 50)),
                 mmss(pct(xs, 90)), mmss(max(xs) if xs else float("nan"))])
print(table(rows, ["family", "found", "never", "min", "median", "p90", "max"]))
print()

# 2b. Per service: time-to-findable via any provider, for services with >1 provider.
multi = sorted(s for s, p in providers.items() if 1 < len(p) <= 4 and s in by_svc)
if multi:
    print("== services with 2-4 providers: who is found first")
    rows = []
    agg = collections.defaultdict(list)
    for svc in multi:
        first_any = min((first_adv[(n, svc)] for n in providers[svc]), default=0)
        per = []
        for n in sorted(providers[svc]):
            hits = [t for t in found_times(n, svc) if t >= first_adv[(n, svc)]]
            per.append((group_of.get(n, "?"), (hits[0] - first_adv[(n, svc)]) if hits else float("inf")))
        for g, x in per:
            agg[g].append(x)
        if len(multi) <= 12:
            rows.append([svc] + [f"{g}:{mmss(x)}" for g, x in per])
    if rows:
        print(table(rows, ["service"] + [f"provider{i + 1}" for i in range(max(len(r) - 1 for r in rows))]))
    for g, xs in sorted(agg.items()):
        f = [x for x in xs if x != float("inf")]
        print(f"   {g}: found {len(f)}/{len(xs)}, median {mmss(pct(f, 50))}, p90 {mmss(pct(f, 90))}")
    print()

# 3. Hit rate per group per window: of the lookups for a provider's service while
#    it is advertised, how many return it.
print(f"== hit rate per {W // 60}-min window: lookups of the service that returned the provider, while advertised")
groups = sorted({group_of.get(n, "?") for (n, _) in spans})
cols, rowsd = [], collections.OrderedDict()
for w0 in range(0, int(end) + 1, W):
    rowsd[w0] = {}
for g in groups:
    for w0 in rowsd:
        hit = tot = 0
        for (n, svc), s in spans.items():
            if group_of.get(n) != g:
                continue
            pid = node_peer.get(n)
            for e in by_svc.get(svc, []):
                if not (w0 <= e["t"] < w0 + W) or not e["ok"]:
                    continue
                if any(a <= e["t"] < b for a, b in s):
                    tot += 1
                    hit += pid in e["peers"]
        rowsd[w0][g] = f"{100 * hit / tot:.0f}% /{tot}" if tot else "-"
print(table([[f"{mmss(w0)}"] + [rowsd[w0][g] for g in groups] for w0 in rowsd], ["window"] + groups))
print()

# 4. Popular services: providers per lookup and cumulative coverage.
popular = sorted(s for s, p in providers.items() if len(p) >= 5 and s in by_svc)
for svc in popular:
    print(f"== {svc}: {len(providers[svc])} providers; per {W // 60}-min window")
    rows = []
    seen = set()
    for w0 in range(0, int(end) + 1, W):
        xs = [e for e in by_svc[svc] if w0 <= e["t"] < w0 + W and e["ok"]]
        if not xs:
            continue
        for e in xs:
            seen.update(peer_node.get(p, p) for p in e["peers"])
        alive = sum(1 for n in providers[svc]
                    if any(a <= w0 + W and b > w0 for a, b in spans[(n, svc)]))
        ns = [e["n"] for e in xs]
        rows.append([mmss(w0), len(xs), f"{statistics.mean(ns):.1f}", max(ns),
                     alive, len(seen & providers[svc])])
    print(table(rows, ["window", "lookups", "mean/lookup", "max/lookup", "advertising", "cum.distinct"]))
    never_seen = sorted(providers[svc] - seen, key=lambda n: int(n[1:]) if n[1:].isdigit() else 0)
    print(f"   never returned: {len(never_seen)} {' '.join(never_seen[:20])}")
    print()

# 5. Ghosts: stopped or flapping providers still returned after they stopped.
print("== ghosts: last time a provider was returned after it stopped advertising")
rows = []
for (n, svc), s in sorted(spans.items()):
    stop = s[-1][1]
    if stop == float("inf"):
        continue
    hits = [t for t in found_times(n, svc) if t > stop]
    asked = [e for e in by_svc.get(svc, []) if e["t"] > stop]
    if n in flappers:
        continue
    rows.append((group_of.get(n, "?"), n, svc, stop, hits[-1] - stop if hits else 0, len(asked)))
by_g = collections.defaultdict(list)
for g, n, svc, stop, ghost, asked in rows:
    by_g[g].append(ghost)
for g, xs in sorted(by_g.items()):
    print(f"   {g}: {len(xs)} providers; ghost lifetime median {mmss(pct(xs, 50))}, "
          f"p90 {mmss(pct(xs, 90))}, max {mmss(max(xs))}; still returned at the end: "
          f"{sum(1 for r in rows if r[0] == g and r[4] > 0 and r[3] + r[4] > end - 120)}")
print()

# 6. Flappers.
if flappers:
    print("== flappers")
    rows = []
    for n in sorted(flappers):
        for (nn, svc), s in spans.items():
            if nn != n:
                continue
            advs = [e for e in ev if e["node"] == n and e["ev"] == "adv"]
            fails = [e for e in advs if not e["ok"]]
            hits = found_times(n, svc)
            asked = by_svc.get(svc, [])
            rows.append([n, group_of.get(n), svc, len(s), len(fails),
                         f"{len(hits)}/{len([e for e in asked if e['ok']])}",
                         mmss(hits[0]) if hits else "-", mmss(hits[-1]) if hits else "-"])
    print(table(rows, ["node", "group", "service", "adv-cycles", "adv-fail", "returned/lookups",
                       "first", "last"]))
    print()

# 7. Advertise call failures.
fails = collections.Counter((group_of.get(e["node"]), e["err"][:60]) for e in ev
                            if e["ev"] == "adv" and not e["ok"])
if fails:
    print("== advertise call failures")
    for (g, err), c in fails.most_common(10):
        print(f"   {g}: {c}x {err}")
