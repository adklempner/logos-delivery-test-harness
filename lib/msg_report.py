#!/usr/bin/env python3
"""Message delivery report over a collect directory.

  msg_report.py <dir>

Reads what docker/entrypoint-module.sh leaves per node:
  <node>.send.jsonl  one line per send call: {"t": ns, "payload": "<node>-<n>", "reply": {...}}
  <node>.msg.jsonl   every delivery_module event the node saw

A message is named by its payload. The sender's reply gives the request id its
own messagePropagated / messageSent / messageError events carry; a receiver's
messageReceived carries the payload back. Every node with a .msg.jsonl other
than the sender is expected to receive every message.

Exit status 0 when every message was accepted, did not error, and reached every
receiver; 1 otherwise.
"""
import base64
import glob
import json
import os
import sys
from collections import Counter, defaultdict


def lines(path):
    with open(path, errors="replace") as f:
        for line in f:
            line = line.strip()
            if line.startswith("{"):
                try:
                    yield json.loads(line)
                except ValueError:
                    pass


def payload_text(arg):
    """messageReceived carries the payload as {"_bytes": <base64, unpadded>}."""
    b = arg.get("_bytes", "") if isinstance(arg, dict) else ""
    try:
        return base64.b64decode(b + "=" * (-len(b) % 4)).decode(errors="replace")
    except ValueError:
        return ""


def ms(ns):
    return f"{ns / 1e6:.0f}"


def spread(values):
    if not values:
        return "-"
    v = sorted(values)
    return f"min {ms(v[0])} / median {ms(v[len(v) // 2])} / max {ms(v[-1])} ms"


def main(d):
    node = lambda p, suffix: os.path.basename(p)[: -len(suffix)]
    received = defaultdict(Counter)  # node -> payload -> times
    sources = Counter()
    own = defaultdict(dict)  # sender -> request id -> {event: (ns, error)}
    for path in sorted(glob.glob(os.path.join(d, "*.msg.jsonl"))):
        n = node(path, ".msg.jsonl")
        received[n]  # a node that saw no event is still a receiver
        for e in lines(path):
            a = e.get("data", {})
            ev = e.get("event")
            if ev == "messageReceived":
                received[n][payload_text(a.get("arg2"))] += 1
                sources[a.get("arg3")] += 1
            elif ev in ("messagePropagated", "messageSent"):
                own[n].setdefault(a.get("arg0"), {})[ev] = (a.get("arg2"), None)
            elif ev == "messageError":
                own[n].setdefault(a.get("arg0"), {})[ev] = (a.get("arg3"), a.get("arg2"))

    sends = []  # (sender, payload, t, request id or None, refusal)
    for path in sorted(glob.glob(os.path.join(d, "*.send.jsonl"))):
        n = node(path, ".send.jsonl")
        for s in lines(path):
            res = s.get("reply", {}).get("result") or {}
            ok = isinstance(res, dict) and res.get("success")
            sends.append((n, s["payload"], s["t"], res.get("value") if ok else None,
                          None if ok else json.dumps(s.get("reply"))[:200]))
    if not sends:
        print("no sends recorded (no *.send.jsonl): nothing to report")
        return 1

    receivers = sorted(received)
    print(f"senders: {', '.join(sorted({s[0] for s in sends}))}; "
          f"nodes with an event log: {len(receivers)}")
    print(f"messages: {len(sends)}")

    refused = [s for s in sends if s[3] is None]
    errors, to_prop, to_sent, undelivered = [], [], [], []
    n_prop = n_sent = n_full = 0
    missed_by = Counter()
    per = defaultdict(Counter)  # sender -> outcome counts
    for sender, payload, t, rid, _ in sends:
        ev = own[sender].get(rid, {}) if rid else {}
        per[sender]["messages"] += 1
        per[sender]["sent"] += "messageSent" in ev
        per[sender]["error"] += "messageError" in ev
        per[sender]["delivered"] += all(
            payload in received[r] for r in receivers if r != sender)
        if "messageError" in ev:
            errors.append((payload, ev["messageError"][1]))
        if "messagePropagated" in ev:
            n_prop += 1
            to_prop.append(ev["messagePropagated"][0] - t)
        if "messageSent" in ev:
            n_sent += 1
            to_sent.append(ev["messageSent"][0] - t)
        missing = [r for r in receivers if r != sender and payload not in received[r]]
        if missing:
            undelivered.append((payload, missing))
            missed_by.update(missing)
        else:
            n_full += 1

    total = len(sends)
    print(f"  accepted by send():   {total - len(refused)}/{total}")
    print(f"  messagePropagated:    {n_prop}/{total}   send->propagated {spread(to_prop)}")
    print(f"  messageSent:          {n_sent}/{total}   send->sent {spread(to_sent)}"
          "   (needs a store node to validate against)")
    print(f"  messageError:         {len(errors)}/{total}")
    print(f"  reached all receivers: {n_full}/{total}")
    # Only the sent payloads: a node also reports what else it relays, such as
    # Mix RLN coordination frames.
    sent = {s[1] for s in sends}
    dup = sum(c - 1 for r in received.values() for p, c in r.items() if p in sent and c > 1)
    other = sum(c for r in received.values() for p, c in r.items() if p not in sent)
    print(f"  duplicate receptions: {dup}; by source: "
          + (", ".join(f"{k} {v}" for k, v in sorted(sources.items(), key=str)) or "-")
          + (f"; receptions of other messages: {other}" if other else ""))

    if len(per) > 1:
        for s, c in sorted(per.items()):
            print(f"  sender {s}: {c['messages']} messages, messageSent {c['sent']}, "
                  f"messageError {c['error']}, reached all receivers {c['delivered']}")

    for payload, reply in ((s[1], s[4]) for s in refused):
        print(f"REFUSED {payload[:24]}: {reply}")
    for reason, count in Counter(e[1] for e in errors).most_common():
        print(f"ERROR x{count}: {reason}")
    for payload, missing in undelivered[:20]:
        print(f"UNDELIVERED {payload[:24]}: missing at {len(missing)} "
              f"({', '.join(missing[:8])}{' ...' if len(missing) > 8 else ''})")
    if len(undelivered) > 20:
        print(f"... and {len(undelivered) - 20} more undelivered")
    for r, count in missed_by.most_common(10):
        print(f"receiver {r} missed {count}/{total}")

    ok = not refused and not errors and n_full == total
    print("verdict:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
