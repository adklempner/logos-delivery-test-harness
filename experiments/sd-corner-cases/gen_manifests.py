#!/usr/bin/env python3
"""Writes the case manifests (PLAN.md §4) into ./manifests/.

One generator, so every case shares the same header (module pin, overrides,
network) as harness.json and the same sd-agent bootstrap.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = json.load(open(os.path.join(HERE, "..", "..", "harness.json")))

BOOT = {
    "name": "boot", "role": "bootstrap", "kind": "sd-agent", "count": 1,
    "startAfter": 0, "ip": "10.0.0.10",
    "nodekey": "9999999999999999999999999999999999999999999999999999999999999999",
    # C1 (first run): with the default 100 connections (50 inbound) the bootstrap
    # refused joiner 51+ for good, and each of them stayed isolated. Every later
    # case gives it room, so the corner case under test is not masked by that one.
    "env": {"P2P_MAX_CONNS": "400"},
}


def agent(name, count, start=0, stop=None, jitter=20, **env):
    g = {"name": name, "role": "member", "kind": "sd-agent", "count": count,
         "startAfter": start, "jitter": jitter, "env": env}
    if stop is not None:
        g["stopAfter"] = stop
    return g


def delivery(name, count, start=0):
    return {"name": name, "role": "member", "kind": "delivery-module", "count": count,
            "startAfter": start, "jitter": 20, "config": "member.json.tpl",
            "env": {"LOOKUP_INTERVAL": "15"}}


def manifest(comment, groups):
    return {
        "_comment": comment,
        "module": BASE["module"],
        "overrides": BASE["overrides"],
        "network": BASE["network"],
        "groups": [BOOT] + groups,
    }


CASES = {
    "smoke": manifest("5-min driver smoke: 2 advertisers, 1 looker, 1 delivery bystander", [
        agent("adv", 2, SD_ADVERTISE="sd/smoke-{i},sd/smoke-common"),
        agent("looker", 1, SD_LOOKUP="sd/smoke-common", SD_LOOKUP_ROTATE="sd/smoke-[1-2]",
              SD_LOOKUP_PER_CYCLE="2", SD_LOOKUP_INTERVAL="15"),
        delivery("delivery", 1),
    ]),
    "iso": manifest("crash isolation: same lookup load, parallel vs sequential CLI calls", [
        agent("adv", 40, SD_ADVERTISE="sd/iso-{i}"),
        agent("look-par", 3, SD_LOOKUP_ROTATE="sd/iso-[1-40]", SD_LOOKUP_PER_CYCLE="5",
              SD_LOOKUP_INTERVAL="20"),
        agent("look-seq", 3, SD_LOOKUP_ROTATE="sd/iso-[1-40]", SD_LOOKUP_PER_CYCLE="5",
              SD_LOOKUP_INTERVAL="20", SD_LOOKUP_PARALLEL="0"),
    ]),
    "c1": manifest("C1 hub shadow: 2 hubs advertise common + every solo + late-solo id", [
        agent("hub", 2, SD_ADVERTISE="sd/common,sd/solo-[1-40],sd/late-solo-[1-2]"),
        agent("solo", 40, SD_ADVERTISE="sd/solo-{i}"),
        agent("ctrl", 20, SD_ADVERTISE="sd/ctrl-{i}"),
        agent("late-solo", 2, start=1200, jitter=0, SD_ADVERTISE="sd/late-solo-{i}"),
        agent("late-ctrl", 2, start=1200, jitter=0, SD_ADVERTISE="sd/late-ctrl-{i}"),
        # Sequential lookers carry the measurement; parallel ones are the crash
        # control from the aborted first attempt (daemon segfaults on 7/10).
        agent("lookers", 6, SD_LOOKUP="sd/common",
              SD_LOOKUP_ROTATE="sd/solo-[1-40],sd/ctrl-[1-20],sd/late-solo-[1-2],sd/late-ctrl-[1-2]",
              SD_LOOKUP_PER_CYCLE="4", SD_LOOKUP_INTERVAL="20", SD_LOOKUP_PARALLEL="0"),
        agent("lookers-par", 4, SD_LOOKUP="sd/common",
              SD_LOOKUP_ROTATE="sd/ctrl-[1-20],sd/solo-[1-40],sd/late-ctrl-[1-2],sd/late-solo-[1-2]",
              SD_LOOKUP_PER_CYCLE="4", SD_LOOKUP_INTERVAL="20"),
        delivery("delivery", 10),
    ]),
    "c2": manifest("C2 flapping advertisers of common-a; common-b is the in-run control", [
        agent("common-a", 35, SD_ADVERTISE="sd/common-a"),
        agent("common-b", 35, SD_ADVERTISE="sd/common-b"),
        agent("flap-fast", 1, SD_FLAP="sd/common-a:5:5"),
        agent("flap-mid", 1, SD_FLAP="sd/common-a:30:10"),
        agent("flap-slow", 1, SD_FLAP="sd/common-a:120:30"),
        agent("late-a-12", 3, start=720, jitter=0, SD_ADVERTISE="sd/common-a"),
        agent("late-b-12", 3, start=720, jitter=0, SD_ADVERTISE="sd/common-b"),
        agent("late-a-25", 3, start=1500, jitter=0, SD_ADVERTISE="sd/common-a"),
        agent("late-b-25", 3, start=1500, jitter=0, SD_ADVERTISE="sd/common-b"),
        agent("lookers", 8, SD_LOOKUP="sd/common-a,sd/common-b", SD_LOOKUP_INTERVAL="15",
              SD_LOOKUP_PARALLEL="0"),
        delivery("delivery", 6),
    ]),
    "c3": manifest("C3 scale ramp 30 -> 99 agents; every agent: own id + shared sd/all", [
        agent("wave-0", 30, SD_ADVERTISE="sd/u-{host},sd/all"),
        agent("wave-10", 20, start=600, SD_ADVERTISE="sd/u-{host},sd/all"),
        agent("wave-20", 20, start=1200, SD_ADVERTISE="sd/u-{host},sd/all"),
        agent("wave-30", 21, start=1800, SD_ADVERTISE="sd/u-{host},sd/all"),
        # m1..m91 are the wave members, in group order.
        agent("lookers", 8, SD_LOOKUP="sd/all", SD_LOOKUP_ROTATE="sd/u-m[1-91]",
              SD_LOOKUP_PER_CYCLE="4", SD_LOOKUP_INTERVAL="20", SD_LOOKUP_PARALLEL="0"),
    ]),
    "c4": manifest("C4 departures: 15 leave at +12 min, 24 drop at +25 min", [
        agent("stay", 50, SD_ADVERTISE="sd/s-{i}"),
        agent("leave-12", 15, stop=720, SD_ADVERTISE="sd/g-{i}"),
        agent("drop-25", 24, stop=1500, SD_ADVERTISE="sd/d-{i}"),
        agent("lookers", 10, SD_LOOKUP_ROTATE="sd/s-[1-50],sd/g-[1-15],sd/d-[1-24]",
              SD_LOOKUP_PER_CYCLE="4", SD_LOOKUP_INTERVAL="15", SD_LOOKUP_PARALLEL="0"),
    ]),
}

# C5: C1b's workload again, on a libp2p_module whose registrar logs every
# decision and rejection reason (nim-libp2p experimental/sd-registrar-reasons).
CASES["c5"] = dict(CASES["c1"], _comment="C5: C1b replay with registrar reason logging; sequential lookers only")
CASES["c5"]["groups"] = [g for g in CASES["c1"]["groups"] if g["name"] != "lookers-par"]
CASES["c5"]["groups"] = [dict(g, count=10) if g["name"] == "lookers" else g for g in CASES["c5"]["groups"]]

if __name__ == "__main__":
    os.makedirs(os.path.join(HERE, "manifests"), exist_ok=True)
    for name, m in CASES.items():
        with open(os.path.join(HERE, "manifests", f"{name}.json"), "w") as f:
            json.dump(m, f, indent=1)
            f.write("\n")
        n = sum(g["count"] for g in m["groups"])
        print(f"{name}: {n} nodes")
