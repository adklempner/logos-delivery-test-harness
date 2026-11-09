# Service-discovery corner cases: test plan

Branch `experimental/sd-corner-cases` of the test harness. It stays local and
is not pushed. Everything runs on the local docker harness: no logos.dev or
logos.test fleet, no public bootstraps.

Goal: find the points where kad-dht service discovery (nim-libp2p `ServiceDiscovery`,
hosted by libp2p_module) degrades or breaks, using 50–100 node fleets that run
for 45–55 minutes each. That is long enough for three advert lifetimes
(E = 900 s) and two advertiser maintenance rounds (10 min).

## 1. What runs

| Component | Revision | Source |
|---|---|---|
| logos-delivery-module | `master` at build time (sha recorded in `resolved.json` per run) | manifest |
| libp2p_module | `33e3c7a7` | module lock |
| nim-libp2p (inside libp2p_module) | `1b893898` | libp2p_module lock |
| logos-delivery (delivery-module bystanders) | `6b1f3a09` | module lock |
| logos-logoscore-cli | `665ac28b` | harness override |

The code review in §2 is against nim-libp2p `1b893898`, the code the fleet runs.

Known issue avoided by design: at this pin KadDHT `maintainLiveness` can
busy-loop once its 1 h grace period ends (see the earlier `mix-80min` run).
Every node in these cases starts at or after T0 and every run ends by T0+55 min,
so no node reaches the grace period. A spin would still be caught: the CPU
sampler runs in every case.

## 2. Code review: where the thresholds are

All references are to nim-libp2p `1b893898`, under `libp2p/protocols/`.

### 2.1 Defaults (`service_discovery/types.nim:26-38`, `kademlia/types.nim:24-30`)

| Parameter | Value | Meaning |
|---|---|---|
| `K_register` | 3 | registrars per bucket of the service's own routing table (≤ 16 × 3 = 48 per service) |
| `K_lookup` | 5 | registrars queried per bucket during a lookup |
| `F_lookup` | 30 | a lookup stops once it holds 30 distinct ads |
| `F_return` | 10 | a registrar returns at most 10 ads per getAds request |
| `E` (advertExpiry) | 900 s | advert lifetime; also the cap on a ticket wait |
| `C` (advertCacheCap) | 1000 | ads per registrar, across all services |
| `P_occ` | 10 | occupancy exponent |
| `Delta` (registrationWindow) | **1 s** | how late a ticket retry may arrive |
| kad `bucketRefreshTime` | 10 min | advertiser maintenance and service-table refresh period |
| kad `timeout` | 5 s | per-bucket deadline in lookups |

### 2.2 Registrar waiting time (`service_discovery/registrar.nim:84-137`)

```
W = E * occupancy * (c_s/C + ipSim + G)      occupancy = 1/(1 - c/C)^10
```

- **ipSim** (`utils/iptree.nim:118-144`) counts the prefix bits of the advertiser's IP
  whose subtree already holds more than its share of the registrar's cached IPs.
  - An unrelated IP in our `/8` scatter scores about `(8 + log2(#ads))/32`,
    roughly 0.4, so W ≈ 6 min.
  - **An IP the registrar already holds in any ad scores 32/32 = 1.0, so
    W ≥ E = 15 min.** The IP tree is shared by all services at the registrar.
- **Service and IP lower bounds** (`registrar.nim:117-136`, `updateLowerBounds`
  `:139-160`): once a registrar issues a ticket with wait W for service S, or
  for IP x, every later registrant of S, or from x, waits at least the
  remaining part of W. A single expensive registrant therefore raises the
  floor for **everyone** advertising the same service at that registrar.
- Ticket retry (`registrar.nim:205-217`): a retry must arrive inside
  `[tMod + tWait, tMod + tWait + 1 s]`, timed in whole seconds. If it arrives
  later, the registrar rejects it. The advertiser then drops the task
  (`advertiser.nim:334`), so the slot stays empty until the next maintenance
  round (≤ 10 min) and the next attempt starts again without a ticket.

### 2.3 Advertiser lifecycle (`service_discovery/advertiser.nim`)

- `startAdvertising` → `scheduleRegistrations` (`:362`): 3 random registrars
  per non-empty bucket of the service table, all at once.
- On `Confirmed` (`:303-314`), the task sleeps E and **returns**. The
  replacement starts at the next `maintainRegistrations` tick (`:119`, every
  10 min), from scratch, with no ticket.
- `stopAdvertising` → `removeProvidedService` (`:440`) cancels tasks and
  **tells no registrar**: the ad stays at every registrar until it expires.
- Only the node's first service is registered locally (`:98`).

### 2.4 Registrar expiry (`service_discovery.nim:74-77`, `advertisement_cache.nim:107-120`)

- The prune loop runs **every E (900 s)**, not continuously. An expired ad is
  kept and served until the next prune, so an ad lives **E to 2E (15–30 min)**
  after its last registration. The README's "up to 900 s" understates this.
- Renewal: when an advertiser comes back, its previous ad is usually still
  cached (expired but not yet pruned). Its own IP then scores ipSim = 1, so the
  renewal waits about E. `put` (`advertisement_cache.nim:77`) refreshes the
  entry in place only once that wait is over.

### 2.5 Lookup (`service_discovery/discoverer.nim:183-218`, `registrar.nim:390-406`)

- A lookup walks the service table's buckets in order and queries up to 5 random
  registrars per bucket in parallel, each bucket under the 5 s deadline. It
  stops at 30 ads.
- A registrar answers from `Table` iteration order (`advertisement_cache.nim:39`):
  **the same ≤ 10 ads every time**. For a popular service, some advertisers may
  be returned rarely or never.
- A rare service never reaches 30 ads, so every lookup for one walks every
  non-empty bucket. A dead registrar costs the full 5 s deadline for its bucket.

### 2.6 Corner cases the code predicts

| # | Prediction | Case |
|---|---|---|
| P1 | A node advertising k services gets services 2..k accepted at each registrar only after ~E. | C1 (hubs) |
| P2 | A hub advertising service S as well raises the service floor for S, delaying S's sole advertiser at shared registrars by up to E. | C1 |
| P3 | A flapper (stop/start) re-registers into its own still-cached ad: ipSim = 1, a 900 s ticket, and the service floor raised again on every flap. Honest late advertisers of the same service wait up to E. | C2 |
| P4 | Registrars answer a popular service from a fixed ≤ 10-ad subset, so cumulative coverage of its advertisers stays below 100 %. | C2, C3 |
| P5 | A late ticket retry (> 1 s) under host CPU load is rejected, and the slot is lost until maintenance. | C3 |
| P6 | A departed advertiser's ad is served for E to 2E (15–30 min). | C4 |
| P7 | After losing many registrars, lookups for rare services slow down (5 s per dead bucket) and miss services whose ads sat on the dead nodes, until the next maintenance round plus wait. | C4 |
| P8 | Renewal after E plus up to 10 min of maintenance delay, at about E wait, leaves per-registrar gaps. Rare services may flicker after T0+15 min. | C1, C4 (long tails) |

## 3. Harness changes (this branch)

1. **New node kind `sd-agent`** (`lib/manifest.py`, `lib/gen_compose.py`).
   - A logoscore daemon hosting **only libp2p_module** (kad + service discovery)
     and openmetrics, plus a Python driver. This is the "fresh code that uses
     just libp2p_module": it calls the module's public API
     (`discoStartAdvertising`, `discoStopAdvertising`, `discoLookup`) through
     `logoscore call`, the same surface delivery_module uses.
   - It may be a **bootstrap** too (fixed `nodekey` → libp2p `privKey`), so the
     whole DHT runs one nim-libp2p.
   - Each sd-agent has a Prometheus target (`:9100`, libp2p_module registry: `cd_*`, `kad_*`, `libp2p_*`).
   - Behaviour is set in the group's `env`:

     | Variable | Meaning |
     |---|---|
     | `SD_ADVERTISE` | comma list of service ids advertised from start; `{i}` = index in group (1-based) |
     | `SD_FLAP` | `<service>:<on_s>:<off_s>`: advertise, stop, repeat |
     | `SD_LOOKUP` | service ids looked up every cycle |
     | `SD_LOOKUP_ROTATE` | ids looked up in rotation, `prefix[a-b]` ranges allowed |
     | `SD_LOOKUP_PER_CYCLE` | how many rotating ids per cycle (default 4) |
     | `SD_LOOKUP_INTERVAL` | seconds per cycle (default 20) |
     | `SD_LOOKUP_START` | seconds after the agent's start before the first lookup |

2. **`docker/entrypoint-agent.sh` + `docker/sd_agent.py`**.
   - The entrypoint starts the daemon, loads libp2p_module with
     `LIBP2P_MODULE_CONFIG` (container address, `bootstrapNodes` from
     `BOOTSTRAP_ADDR`, privKey for a bootstrap), then calls `start` and
     `discoStart` and starts openmetrics.
   - The driver writes JSON lines to `/traces/<host>.sd.jsonl`: `self` (peer
     id), `adv` / `unadv` (with result), and `lookup` (service, latency, ok,
     peer ids returned).
   - The image gains `python3-minimal`.

3. **`lib/sd_report.py`**: per-case analysis of the `.sd.jsonl` files.
   - Maps peer ids to nodes.
   - Per service: time from the advertiser's start to its first appearance in
     any lookup (time-to-findable), split by who returned it (sole advertiser
     vs hub).
   - Visibility over time in 1-min bins, providers per lookup, cumulative
     coverage, lookup latency (p50/p95/max) per phase, lookup failures.
   - Ghost lifetime: how long a stopped node keeps appearing.
   - Flap counters.

4. **Sampler** (`out/sampler/`, from the previous session, run next to each case):
   docker stats, per-module CPU seconds, a libp2p responsiveness probe every
   60 s, and the Prometheus `cd_*` series saved before teardown.

5. **CPU burner** (C3 only): a helper container (`out/sd/burn.sh`) that saturates
   the docker VM for a fixed window, to provoke P5 on purpose.

Nothing in logos-delivery, logos-delivery-module, libp2p_module or nim-libp2p is modified.

## 4. Cases

Shared for all cases:
- one `sd-agent` bootstrap at `10.0.0.10`;
- members scattered over `10.0.0.0/8` (harness default);
- cluster 42, 1 shard;
- service ids prefixed `sd/`.

"Lookers" are sd-agents that advertise nothing and only look up.

### C1: hub shadow (one or two common advertisers, many sole advertisers)

How fast can a particular service be found when one or two "common" advertisers
also advertise it next to its sole advertiser?

| Group | Kind | Count | Advertises | Start |
|---|---|---|---|---|
| boot | sd-agent bootstrap | 1 | – | 0 |
| hub | sd-agent | 2 | `sd/common`, `sd/solo-1..40`, `sd/late-solo-1..2` (43 services each) | 0 |
| solo | sd-agent | 40 | `sd/solo-{i}` | 0 |
| ctrl | sd-agent | 20 | `sd/ctrl-{i}` (no hub) | 0 |
| late-solo | sd-agent | 2 | `sd/late-solo-{i}` (hub-shadowed) | +20 min |
| late-ctrl | sd-agent | 2 | `sd/late-ctrl-{i}` | +20 min |
| lookers | sd-agent | 10 | – ; lookup `sd/common` + 4 rotating of all 64 ids, every 20 s | 0 |
| delivery | delivery-module | 10 | `/logos/delivery` (bystanders) | 0 |

**87 nodes**, run **45 min**.

Measured:
- time-to-findable of each `solo-i` via its sole advertiser vs each `ctrl-i`
  (median, p90), and via any provider;
- when each hub service first appears;
- `sd/common` providers per lookup (expect 2);
- late joiners' time-to-findable in a formed network;
- visibility flicker after T0+15/30 min;
- `cd_register_requests{status}` at registrars;
- hub CPU and pending actions;
- delivery bystanders' discovery report (do they still see each other).

Pass/fail threshold, recorded rather than enforced: a sole service should be
findable within 10 min. Shadowed vs control median difference > 3 min confirms P2.

### C2: unreliable advertiser (flapping) among many common advertisers

| Group | Kind | Count | Behaviour | Start |
|---|---|---|---|---|
| boot | sd-agent bootstrap | 1 | – | 0 |
| common-a | sd-agent | 35 | advertise `sd/common-a` | 0 |
| common-b | sd-agent | 35 | advertise `sd/common-b` (control: no flapper) | 0 |
| flap-fast | sd-agent | 1 | `sd/common-a` 5 s on / 5 s off | 0 |
| flap-mid | sd-agent | 1 | `sd/common-a` 30 s on / 10 s off | 0 |
| flap-slow | sd-agent | 1 | `sd/common-a` 120 s on / 30 s off | 0 |
| late-a-12 / late-b-12 | sd-agent | 3 + 3 | `sd/common-a` / `sd/common-b` | +12 min |
| late-a-25 / late-b-25 | sd-agent | 3 + 3 | same | +25 min |
| lookers | sd-agent | 8 | lookup `sd/common-a`, `sd/common-b` every 15 s | 0 |
| delivery | delivery-module | 6 | bystanders | 0 |

**100 nodes**, run **45 min**.

Measured:
- late joiners' time-to-findable, a vs b (P3);
- distinct providers per lookup and cumulative coverage, a vs b (P3, P4);
- flappers' own visibility (stale ads);
- flapper resource use: CPU, pending advertiser actions, memory trend (task or
  table leak);
- register status mix.

### C3: scale ramp and the ticket window

| Group | Kind | Count | Start |
|---|---|---|---|
| boot | sd-agent bootstrap | 1 | 0 |
| wave-0 | sd-agent | 30 | 0 |
| wave-10 | sd-agent | 20 | +10 min |
| wave-20 | sd-agent | 20 | +20 min |
| wave-30 | sd-agent | 21 | +30 min |
| lookers | sd-agent | 8 | 0 |

**100 nodes** at +30 min, run **55 min**.

Every wave member advertises its own `sd/u-<host>` id and the shared `sd/all`.
The second service is deliberate: it puts P1 under scale. Lookers look up
`sd/all` plus 4 rotating unique ids every 20 s.

CPU burner from +40 to +48 min: busy loops on all VM CPUs.

Measured per wave:
- time-to-findable (unique and `sd/all`) vs fleet size;
- lookup latency vs fleet size;
- `Rejected` register responses (ticket window) before, during and after the burn (P5);
- probe latency during the burn;
- `sd/all` cumulative coverage vs advertisers alive (P4 at scale).

### C4: departures, ghost ads and dead registrars

| Group | Kind | Count | Behaviour | Start / stop |
|---|---|---|---|---|
| boot | sd-agent bootstrap | 1 | – | 0 |
| stay | sd-agent | 50 | advertise `sd/s-{i}` | 0 / – |
| leave-12 | sd-agent | 15 | advertise `sd/g-{i}` | 0 / +12 min (graceful) |
| drop-25 | sd-agent | 24 | advertise `sd/d-{i}` | 0 / +25 min (30 % of the rest at once) |
| lookers | sd-agent | 10 | rotate over all `s-*`, `g-*`, `d-*` (4 per 15 s) | 0 |

**100 nodes**, run **50 min**.

Measured:
- ghost lifetime of `g-*` after +12 min, and of `d-*` after +25 min (P6:
  expect 15–30 min, not ≤ 15);
- `s-*` findability and lookup latency before and after +25 min (P7);
- recovery time back to the pre-drop findability.

## 5. Procedure per case

1. `HARNESS_MANIFEST=experiments/sd-corner-cases/manifests/cN.json ./harness.sh gen`,
   then check the schedule printout against the table above.
2. Start the sampler in the background
   (`out/sampler/sampler.sh out/collect/sd-cN/sampler <duration>`).
3. `./harness.sh run <duration> out/collect/sd-cN` in the background (with
   `COLLECT_LOGS=all` only for small cases: 100 logs at DEBUG are large).
4. `python3 lib/sd_report.py out/collect/sd-cN > out/collect/sd-cN/sd_report.txt`.
5. Write `experiments/sd-corner-cases/reports/CN.md`:
   - verdict;
   - pins;
   - topology and schedule;
   - results table;
   - which predictions were confirmed or refuted, with evidence;
   - anomalies;
   - comparison with the other cases where relevant.

Build once after the harness changes (`./harness.sh build`). Then run a
**5-minute smoke** (1 bootstrap, 3 sd-agents: 2 advertisers, 1 looker) to
validate the driver before C1.

## 6. Risks

- **Memory:** OrbStack VM 12.6 GB / 10 CPU. sd-agents carry no delivery_module, so they
  should take well under 100 MB each. If the smoke run shows otherwise,
  delivery bystanders are dropped first.
- **Seconds granularity and the 1 s window (P5):** host load from 100 containers may
  already trigger rejections before the burn. C3's waves separate the two.
- **Expected failures:** a long ticket wait or a rejection is the *finding*, not a
  harness fault. The driver logs every call, including failures.
