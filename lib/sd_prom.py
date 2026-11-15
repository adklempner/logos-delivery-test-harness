#!/usr/bin/env python3
"""Summarise the sampler's Prometheus dumps (experimental/sd-corner-cases).

usage: sd_prom.py <sampler-dir> <t0> [window-seconds]

prom_q1: registrar side, register requests per status (increase per 1m step)
prom_q2: advertiser side, register responses per status
prom_q3: ads cached per registrar      prom_q4: advertiser pending actions
prom_q5: resident memory               prom_q6: IPs in the registrar's ip tree
"""
import collections
import json
import os
import sys

d, t0 = sys.argv[1], float(sys.argv[2])
W = int(sys.argv[3]) if len(sys.argv) > 3 else 300


def load(name):
    p = os.path.join(d, name)
    if not os.path.exists(p):
        return None, []
    lines = open(p).read().split("\n", 1)
    q = lines[0].lstrip("# ")
    try:
        data = json.loads(lines[1])["data"]["result"]
    except (ValueError, KeyError, IndexError):
        return q, []
    return q, data


def mmss(x):
    return f"{int(x) // 60}:{int(x) % 60:02d}"


for name, title in (("prom_q1.json", "registrar: register requests by status"),
                    ("prom_q2.json", "advertiser: register responses by status")):
    q, data = load(name)
    if not data:
        print(f"== {title}: no data")
        continue
    agg = collections.defaultdict(lambda: collections.Counter())
    for s in data:
        st = s["metric"].get("status", "?")
        for ts, v in s["values"]:
            w = int((float(ts) - t0) // W) * W
            agg[w][st] += float(v)
    sts = sorted({k for c in agg.values() for k in c})
    print(f"== {title} (sum over nodes, per {W // 60}-min window)")
    print("window   " + "  ".join(f"{s:>10}" for s in sts))
    for w in sorted(agg):
        print(f"{mmss(w):<8} " + "  ".join(f"{agg[w][s]:>10.0f}" for s in sts))
    print()

for name, title, scale in (("prom_q3.json", "ads cached per registrar", 1),
                           ("prom_q4.json", "advertiser pending actions", 1),
                           ("prom_q5.json", "resident memory MiB (libp2p_module host)", 1 / 2**20),
                           ("prom_q6.json", "ip-tree IPs per registrar", 1)):
    q, data = load(name)
    if not data:
        print(f"== {title}: no data")
        continue
    byw = collections.defaultdict(list)
    for s in data:
        perw = {}
        for ts, v in s["values"]:
            perw[int((float(ts) - t0) // W) * W] = float(v) * scale
        for w, v in perw.items():
            byw[w].append((v, s["metric"].get("node", "?"), s["metric"].get("group", "?")))
    print(f"== {title}: per window, median / max (node, group)")
    for w in sorted(byw):
        xs = sorted(byw[w])
        med = xs[len(xs) // 2][0]
        mx = xs[-1]
        print(f"{mmss(w):<8} n={len(xs):<3} median {med:8.1f}   max {mx[0]:8.1f} ({mx[1]}, {mx[2]})")
    print()
