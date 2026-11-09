# Service-discovery corner cases: campaign summary (2026-11-09)

Six fleet runs of 87–100 nodes, 35–55 min each, all on the local docker harness.
- No public fleet was involved.
- Pins: logos-delivery-module `586e1a38`, libp2p_module `33e3c7a7` (nim-libp2p
  `1b893898`), logos-delivery `6b1f3a09`, logoscore-cli `665ac28b`.
- The plan is in [`PLAN.md`](PLAN.md); one report per case is in [`reports/`](reports/).

## Findings, most severe first

| # | Finding | Threshold / numbers | Evidence |
|---|---|---|---|
| 1 | **A libp2p_module bootstrap admits only `maxInConnections` joiners. Every later joiner is isolated for good, and silently.** Nothing re-bootstraps, and every API call (`start`, `discoStart`, advertise, lookup) reports success. Lookups return 0 records in 12 ms. | 50 inbound (`maxConnections` 100) → 34 of 87 nodes isolated, including 7 of 10 delivery-module bystanders, which saw 1–3 of 10 peers. libp2p_module's own default is 25 inbound. | C1 §3 |
| 2 | **Ticket retries miss the registrar's 1 s window, and a miss throws away all accumulated wait.** Rejected tasks are dropped until the 10-min maintenance round and restart without a ticket. | 100 % of rejections are `ticket outside valid time window`, 1–17 s late (median 3 s). 51 % of retries rejected in C5; 0 % below about 60 nodes, 57–76 % at 90–99 nodes on a saturated host. | C5 §2, C3 §3.2, C1b |
| 3 | **The logoscore daemon segfaults when discovery calls overrun their timeouts.** The whole node is lost. | 7/10 lookers (C1 first attempt), 4/4 parallel lookers (C1b), 2 sequential lookers (C3), a hub firing 43 concurrent adverts after an 89 s libp2p start (C5). Every crash was at 87–99 nodes under > 800 % CPU. | C1 §5, C1b, C3 §4, C5 §5 |
| 4 | **A node advertising k services waits about E (15 min) per extra service at each registrar.** The registrar's IP tree is shared across services, so an already-cached IP scores 1.0. | Ticket 957 s vs 319 s (C5). A hub's 43 services: 100 % visible after 40 min at 53 nodes (C1), only 30 of 86 (hub, service) pairs after 45 min at 87 nodes (C1b). Affects mix nodes (`/logos/delivery` + `/mix/1.0.0`). | C5 §3, C1 §4, C1b |
| 5 | **A popular service gets slower to register as it gets more popular** (service lower bound). | `sd/all` median time-to-findable 4:30 → 15:06 from 30 to 90 advertisers; unique ids flat at about 7 min. | C3 §3.1 |
| 6 | **A flapping advertiser (stop/start) is never re-registered once its first ads age out.** `stopAdvertising` cancels the pending tickets. | flap 5/5 s and 30/10 s visible only until about 17 min. flap 120/30 s **never** visible (0 of 1,334 lookups). No resource leak. | C2 §2.3 |
| 7 | **A flapper also delays honest new advertisers of the same service.** | Late joiners of the flapped service: 5:04–12:40. Control service: 0:09–5:22 for four of six. n = 3 + 3 per wave. | C2 §2.2 |
| 8 | **Ghost ads live 19–23 min after a provider leaves, removed only at the next prune tick (every E).** Lifetime is between E and 2E; the README says ≤ 900 s. | leave at +12 min → gone at 31–32 min; drop at +25 min → gone at 46–48 min. | C4 §2 |
| 9 | **Rare-service lookups run into libp2p_module's 10 s synchronous call budget past about 70 nodes on a saturated host.** | 6–20 % of lookups fail with `Failed to lookup: timeout` (10.0–11.5 s); p50 0.47 → 2.8 s from 30 to 90 nodes. | C3 §2, C1b, C4 §3 |
| 10 | **A libp2p start can hang for good during a 100-node boot storm.** The switch is not started after 180 s; m34 stayed down. A start that did complete took up to 89 s. | 1 node each in C3 and C4. | C3 §5, C4 §1, C5 §5 |

## Predictions from the code review (`PLAN.md` §2.6)

| Prediction | Outcome |
|---|---|
| P1: extra services wait about E | **Confirmed**, measured (finding 4) |
| P2: a hub delays the sole advertiser of a service it shares | **Refuted** twice: shadowed and control sole advertisers become findable on the same scale (C1, C1b) |
| P3: a flapper raises the service floor for honest advertisers | **Supported** (finding 7), small n |
| P4: a registrar's fixed 10-ad answer limits coverage of a popular service | **Refuted**: lookups combine registrars up to the 30-record cap; 43 of 44 providers seen (C2) |
| P5: the 1 s ticket window rejects late retries under load | **Proven** (finding 2) |
| P6: ghost ads live E to 2E | **Confirmed** (finding 8) |
| P7: losing registrars hurts rare-service lookups | **Mostly refuted** at 28 % loss: a 5-min blip, findability 99–100 % (C4 §3) |
| P8: renewal gaps cause flicker after E | **Not seen**: unpruned stale ads cover the gap (C1) |

Not predicted, found: findings 1, 3, 5, 9 and 10.

## Deviations from the plan

- **C1 ran twice.** The first full run exposed finding 1, which masked everything else.
  - C1b reran C1 with `P2P_MAX_CONNS=400` on the bootstrap.
  - All later cases kept that, so findings 2–9 are measured on a fully connected fleet.
- **The first C1 attempt was aborted at 12 min.** 7 of 10 lookers had crashed (finding 3).
  - A short isolation run (`sd-iso`, 47 nodes, 11 min) did not reproduce the crash.
  - From C2 on, lookers make one call at a time (`SD_LOOKUP_PARALLEL=0`).
- **C5 was added:** a 35-min C1b replay on a libp2p_module whose registrar logs its
  decisions and rejection reasons, to turn P5 from an inference into a measurement.
- **C3's burner** ran from 40 to 48 min as planned. At 99 nodes the host was already
  saturated without it, so the burn adds load rather than causing it.

## Where things are

**Harness repo** (branch `experimental/sd-corner-cases`, local; nothing committed or pushed):
- `lib/manifest.py`, `lib/gen_compose.py`: the `sd-agent` kind.
- `docker/entrypoint-agent.sh`, `docker/sd_agent.py`, `docker/Dockerfile` (adds python3).
- `harness.sh`: collect copies `*.sd.jsonl` and `t0`.
- `lib/sd_report.py`, `lib/sd_prom.py`: analysis.
- `experiments/sd-corner-cases/`:
  - `PLAN.md`, `SUMMARY.md`, `reports/C1–C5.md`;
  - `gen_manifests.py`, `manifests/*.json`.

**nim-libp2p** (branch `experimental/sd-registrar-reasons`): a scratch worktree of
`out/src/nim-libp2p` at `1b893898`, logging-only patch to
`service_discovery/registrar.nim`, uncommitted. Source copy in `out/nim-libp2p-reglog`.

**Data**, under `out/collect/`:
- `sd-smoke2`, `sd-c1-aborted`, `sd-iso`, `sd-c1`, `sd-c1b`, `sd-c2`, `sd-c3`, `sd-c4`, `sd-c5`;
- each run directory holds its `sampler/` and its `sd_report*.txt` / `sd_prom.txt`.

**Helpers**, under `out/`:
- `out/sampler/` (extended Prometheus dump);
- `out/sd/burn.sh`, `out/sd/harvest.sh`;
- `out/build-libp2p-reglog.sh`.

**State left behind:**
- `logos-sim:local` is the stock image (also tagged `logos-sim:stock-sd`);
- `logos-sim:reglog` is the instrumented one;
- **`out/modules/libp2p_module` holds the instrumented build**: the next `./harness.sh build` restores the stock one.

## Follow-ups worth considering (not done)

1. **Bootstrap saturation (finding 1):**
   - re-bootstrap from the peer store or from other known peers when the routing table stays empty;
   - surface "no DHT peers" as an error or event instead of success;
   - a connection manager that trims idle kad connections on entry nodes;
   - document multiple bootstraps (`joinAll`) as required.
2. **Ticket window (finding 2):** widen `registrationWindow`, or let a late
   retry keep its accumulated wait instead of being rejected outright. Or have the
   advertiser aim early and retry within the window.
3. **Daemon segfault (finding 3):** reproduce with core dumps enabled (`ulimit -c`
   in the entrypoint plus a compose `ulimits`) on the C5 hub pattern: concurrent
   calls into a libp2p_module whose start is still in flight.
4. **Multi-service penalty (finding 4):** key the IP similarity per service, or
   exempt an IP's own existing ads from its score.
5. **README fix:** "a stopped node's id can keep coming back for up to 900 s" →
   up to 2 × advertExpiry (30 min).
