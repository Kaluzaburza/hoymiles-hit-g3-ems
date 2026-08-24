# Automation simulation and safety report

Baseline date: 2026-08-13 (Europe/Warsaw)
Final RC6 live audit date: 2026-08-14 (Europe/Warsaw)
Latest shared-bus hardware/protocol observation: 2026-08-15 (Europe/Warsaw)
Final v1.5.6 release date: 2026-08-21 (Europe/Warsaw)
v1.5.7 GCF cohort hotfix candidate date: 2026-08-22 (Europe/Warsaw)
v1.5.8 RCE lifecycle hotfix candidate date: 2026-08-24 (Europe/Warsaw)
Last completed offline baseline in this report:
**v1.5.8 pre-commit candidate worktree — PASS**
Last accepted live runtime merge:
**`ce4afc614a691ce70c67da2439613de90e0c61c2`**
(implementation commit **`915337fa34529c5197ad581a41d351b78bfb1d33`**)
Current status: **v1.5.6 is published after full local/CI validation and live
rollout on localhost plus `miernik.com.pl`. The final simultaneous 15-minute
window proved stable tariff and RCE readiness but contained no planned active
RCE slot. The maintainer explicitly accepted that bounded exact-final active-
slot evidence gap for release, with a hotfix path. The 2026-08-15 shared-bus
run remains hardware/protocol evidence from HA package 1.5.4 and ESP32 project
1.5.3, not exact-v1.5.6 per-Slave acknowledgement.**

Current v1.5.7 candidate: semantic hotfix commit
**`59fb1e28efb0471e10a40728f8d2e74b4b8164dd`**. Focused deterministic
tests and mutation probes pass as recorded below. This source entry does not
claim exact-final localhost, field, CI or public-release acceptance; those
remain mandatory gates and are recorded in the external release report for the
exact final commit.

This report covers the deterministic planning and control safeguards used by
the RCE market-price optimizer, tariff-aware grid charging and experimental
RCEm 253 V+ voltage management. The tests do not write to a real inverter.
They verify arithmetic, state transitions, interlocks and fail-safe behaviour
before field acceptance on each installation.

## v1.5.8 RCE lifecycle and HIT-20L hotfix — offline GO; exact-commit field gate pending

The pre-release field patch isolated two lifecycle causes. After physical ACK,
the controller incorrectly reused new-start eligibility as continuation
authority. A separate event-order race could publish a new current optimizer
result just before dependent execution readiness became current. The promoted
hotfix removes start eligibility only from post-ACK continuation and adds one
no-write bridge, anchored to the source-plan update and bounded to **0–5
seconds**, for an already active and physically acknowledged cycle. A new
start still requires start eligibility. Planned-slot and continuation
eligibility, owner/conflict, physical mode/readback, topology, fresh SOC/BMS
and all other hard stops remain mandatory; loss of continuation eligibility is
an immediate stop.

The HIT-20L selector still denotes a **20 kW** nameplate and total AC bridge.
RCE battery-only planning and verification use **16 kW per inverter**, so the
two-inverter battery base is **32 kW** and the requested percentage applies to
that base. PV, LOAD and charging continue to use their separate physical
paths. The calibration does not add a power-cap write, does not change 4306
semantics and does not affect the 5/10/12/15 kW profiles.

The already field-accepted start verifier remains transaction-frozen and uses
six fresh complete aggregate generations, four overlapping windows of three
and a **155-second** horizon. A single isolated sampled transition peak remains
best-effort diagnostics; persistent mismatch, missing export or no stable
window still produces the existing fail-closed neutral rollback. Master FC03
is still Master configuration acknowledgement only and no per-Slave protocol
ACK is claimed.

Pre-commit deterministic evidence on 2026-08-24:

- release validator: **PASS**, manifest/package version **1.5.8**, **294**
  localized entities;
- RCE optimizer: **77/77 PASS**;
- dedicated HIT-20L calibration: **10/10 PASS**;
- automation matrix quick: **488/488 PASS**;
- automation matrix exhaustive: **2064/2064 PASS**;
- explicit BMS fail-closed contracts: **4/4 PASS**;
- tariff, RCEm, RCE history, energy, LOAD, power balance, firmware-readback,
  optimizer executor/startup, source-device rebind, diagnostics and analyzer:
  **PASS**;
- frontend validator: **PASS**;
- generator: two in-place passes and clean-copy A/B: **deterministic PASS**;
- independent out-of-tree adversarial harness: two identical runs,
  **20/20 mutations detected**, survivors **0**, NOT RUN **0**; identical
  result SHA-256
  **9A24A7339D74181FCC6C1DC0330407C83E0B38BC06E2CA39047AB36694B0CC94**;
- exact clean v1.5.7 → v1.5.8 managed-asset simulation, fresh install,
  absent packages/www, idempotence, locally modified scheduler preservation,
  metadata and interrupted atomic-copy rollback: **PASS**;
- empirically exercised exact package lifecycle: exactly **two Home Assistant
  restarts** after HACS (first loads runtime/copies the unchanged managed
  package; second parses and activates it).

The accepted precursor field window on the two-HIT-20L installation ran from
2026-08-24 19:35:20.986 CEST through 20:05:31.047 CEST (**1810.06 s**) without
rollback. It crossed a cohort refresh, expiration of start eligibility and the
20:00 slot boundary. Final physical mode readback was 5, 4306 was 100%, battery
power was about 34.1 kW and grid export about 30.4 kW. The verifier reported
**confirmed / fresh_direction_and_target_confirmed**; its sampled transition
peak of 64.516 kW remained diagnostic. This is installation-specific precursor
evidence, not exact-v1.5.8 acceptance.

Publication remains blocked until these exact pre-commit bytes become one
candidate commit and that exact commit completes a natural 30-minute RCE field
window. The external exact-commit acceptance report records the commit,
deployment hashes, timestamps and final PASS/HOLD result; no post-acceptance
runtime or documentation commit is permitted.

## v1.5.7 RCE GCF cohort hotfix — focused candidate evidence

The change is limited to RCE source-state ingestion in `rce_sensor.py` and its
focused regression coverage. During delivery of one three-part physical GCF
report, the last coherent cohort may remain authoritative for no more than five
seconds. Completion consumes one changed semantic cohort once; an identical
completion does not advance the input revision or start another full optimizer
calculation. If coherence does not return, the retained state expires and the
existing physical contract fails closed. Unload clears the state and cancels
delayed callbacks. No retained state is restored across restart.

Focused pre-release evidence on 2026-08-22:

- RCE optimizer/sensor deterministic suite: **77/77 PASS**, repeated twice;
- optimizer executor contract: **PASS**;
- optimizer startup contract: **PASS**;
- Python AST syntax and `git diff --check`: **PASS**;
- temporary out-of-tree mutation campaign: **12/12 mutations killed**,
  including unbounded/60-second grace, duplicate or missed cohorts, retained
  unload state, future/stale/unsupported trust, factor drift, optimizer output
  drift and a sensor self-loop.

The frozen public-v1.5.6 `rce_optimizer.py` SHA-256 is
`F95CA95D8290995016CED33F12A9FEEC8306CA7E6BF224DA385C956774866870`;
the hotfix does not edit it. Exact enabled physical GCF plus export limit
`0.0%` still selects `fixed_zero_export`, excludes restricted learning and
uses exactly `0.80 × Solcast`. Positive export, disabled GCF and unknown/fail-
closed behavior remain unchanged. The complete exact-final release gate and
real-inverter acceptance are separate from these offline focused results.

The v1.5.2 RC2 deterministic, packaging and firmware checks below were
completed on 2026-08-13. Its local Home Assistant 2026.8 deployment exposed a
P1 source-device split. RC3 added the fail-closed rebind, passed its dedicated
**6/6** contract and exact-SHA GitHub gates, and was deployed locally. The
rebind persisted and native physical EMS readbacks were live, but ordinary
stable sensor proxies remained unavailable because a parallel-topology
availability gate incorrectly covered every sensor. RC4 scoped that gate only
to the two derived parallel-power proxies, passed exact-SHA CI and restored the
proxies in the local installation. That live run then exposed an independent
PSE interval-end P1: `dtime_utc` marks the end of a quarter, not its start. RC5
fixed the UTC mapping, passed full offline validation and exact-SHA CI, and was
deployed to the local and parallel test installations. Its parser and backend
fail-closed state were correct live. The parallel dashboard then exposed a
separate frontend P1: a transient `404` from the integration's `static-r2` route
was retained by the reverse proxy/browser, so Lovelace timed out waiting for the
dashboard strategy after restart. RC6 is the startup hotfix target; its code,
independent review and full offline gate passed with P0=0/P1=0. Exact runtime
commit `7ce13155533bfa0bf9752a0fd201224dac1a7393` then passed push CI and
`workflow_dispatch`, including the exhaustive matrix, and passed the local and
parallel-installation live frontend audit. The later v1.5.0 section is retained
only as an explicitly archived live baseline.

## v1.5.3 corrective release — offline and live PASS

A later audit found that the published 2064-scenario matrix had inherited
fail-closed defaults for BMS availability/freshness and omitted maximum charge
current. All 696 RCE matrix cases therefore had zero controlled capability and
never reached the joint solver. This invalidates the old matrix as evidence of
nominal RCE execution, but not the independent 69-scenario RCE optimizer suite
or the runtime's fail-closed behaviour.

The corrected matrix now supplies complete nominal BMS contracts and retains
four explicit missing/stale/future/zero fail-closed cases outside the scenario
count. Fresh results on the current workspace are:

- **Quick:** 488/488; all four models have positive power, joint-solver calls
  and controlled export plans.
- **Exhaustive:** 2064/2064; 220 joint-solver calls and 160 controlled export
  plans in the 576 main RCE cases, plus 58/49 respectively in the 120 random
  boundary cases; BMS fail-closed contracts 4/4.
- **RCE optimizer:** 69/69; tariff, RCEm, history, shared energy/LOAD/power,
  startup and executor contracts passed.

The same candidate adds a monotonic input revision and a direct before/after
fingerprint around every executor-backed optimizer. A changed input withdraws
execution authority before recalculation, stale executor output is discarded,
and only a result matching the latest revision is published with
`result_current: true`. Cross-optimizer fingerprints include only attributes
actually consumed, preventing RCE/tariff diagnostic ping-pong.

A live browser check on the preceding candidate reproduced a five-second
Lovelace timeout waiting for
`ll-strategy-dashboard-hoymiles-hit-xxl-g3`. Frontend revision 17 publishes one
canonical full module, removes duplicate and legacy bootstrap resources via
the live Lovelace collection, and upgrades a bootstrap-first constructor in
place. Offline PL, EN and bootstrap-first execution each render exactly 42
Aurora frames. The exact v1.5.3 merge then passed push, pull-request and
workflow-dispatch CI, including the exhaustive matrix, and was installed on
the local Home Assistant test system. Two configuration-checked restarts and a
fresh browser session confirmed revision 17 without strategy/configuration
errors on any of the 17 dashboard paths.

### v1.5.3 exact-SHA live evidence — 2026-08-14

| Check | Result |
|---|---|
| Runtime traceability | Implementation `915337fa34529c5197ad581a41d351b78bfb1d33`; tested `main` merge `ce4afc614a691ce70c67da2439613de90e0c61c2` |
| GitHub CI | Push run `31771450198`, PR run `31771472055`, exhaustive dispatch run `31771487285`, and post-merge `main` run `31771971876`: PASS |
| Deployment archive | SHA-256 `85881F64BBD6AFD66114C8944BADAABBF0F83BD70380D0B661F376560D0D7C22` |
| Home Assistant | Two `ha core check` runs and two required restarts succeeded; package **1.5.3**, setup **Ready** |
| Optimizer authority | RCE, tariff and RCEm `result_current: true` after restart |
| Control safety | Self-Use; every execution owner/cycle off; RCE and balancing off; tariff inactive; RCEm enabled in shadow mode |
| Frontend assets | Revision-17 card, PL/EN JSON and image returned HTTP 200; exactly one managed Hoymiles module resource; no relevant Repair issue |
| Aurora browser audit | All 17 authoritative paths rendered in a fresh session without strategy/configuration errors or relevant console warnings |
| Aurora structure | PL, EN and bootstrap-first deterministic configurations each contain exactly 42 non-nested Aurora frames; live DOM count is conditional by design |
| Firmware action | None; this release does not require another ESPHome flash on a system already running the v1.5.2 FC03-readback firmware |

The final release/tag commit is expected to differ only by this live-evidence
documentation. Its delta from the tested runtime merge must remain
documentation-only.

## v1.5.6 EMS stability release — offline/CI PASS; live rollout accepted with limit

The candidate adds a physical-response contract after Grid Discharge on a
detected parallel Master without changing the existing configuration ACK.
The FC16 system broadcast still has no Modbus reply, and only a newer exact
FC03 block from the Master acknowledges configuration. That FC03 remains
Master-only evidence; the new signal is deliberately named and documented as
an **aggregate system physical response**, never as a per-Slave protocol ACK.
The source generation is
`sensor.hoymiles_hit_parallel_aggregate_power_readback_generation`; the result
is `sensor.hoymiles_parallel_aggregate_physical_response`.

ESPHome publishes a new complete aggregate-power readback generation only from
the Master LOAD FC03 callback and only when the corresponding grid and PV
inputs are fresh. Local writes, timers and partial source callbacks cannot
advance it. After Master FC03 confirms Grid Discharge, Home Assistant applies a
20-second transition grace, considers five newer complete generations with up
to 20 seconds for each, and requires three consecutive stable generations. The
published horizon is 135 seconds. A sampled transition peak is retained as
diagnostic context but excluded from the stable window and is not sufficient by
itself for either success or failure.

RCE freezes its authoritative expected discharge power before the first owned
write. Its stable window requires battery discharge, grid export of at least
0.25 kW and agreement with that frozen target; otherwise it uses the existing
neutral fail-closed rollback. Manual/manual-recovery and RCEm pre-discharge have
no authoritative total-kW target, so their diagnostic evaluates fresh stable
battery-discharge direction without grid-export or amplitude rejection. The
neutral Self-Use rollback path never waits for aggregate discharge evidence.

The final post-RC5 stabilization work establishes these additional contracts:

- `result_current=false` is planning authority for **new starts**, not by
  itself a stop instruction for an **already accepted and latched active RCE
  cycle**. A harmless price/PV/LOAD update may preserve that cycle on its last
  committed contract during recalculation. BMS capability/freshness, physical
  GCF/export permission, ownership/conflict, Off-Grid or incompatible mode,
  required FC03/readback, SOC/reserve floor and inverter/topology availability
  continue to be evaluated directly from live safety inputs and stop the cycle
  immediately, even before the replacement result becomes current.
- The tariff optimizer fingerprint excludes `tariff_charge_active` and other
  owner/execution telemetry not consumed by the mathematics, preventing a
  tariff start from invalidating itself. Real SOC, forecast PV/LOAD,
  tariff/profile, BMS capability and freshness changes still invalidate the
  result and block new execution normally.
- Tariff rollback constructs one final `4300–4306` block from the last
  confirmed physical snapshot plus explicit rollback values, skips logical
  no-ops, emits at most one FC16 and waits for a newer matching physical FC03
  for the whole block. It cannot start another full-block transaction while
  that acknowledgement is pending.
- The same physical-ACK invariant now covers every Home Assistant helper for
  `4300–4306`: all seven readback registers must match. Missing, stale or
  inconsistent FC03 stays fail-closed. No second transaction mechanism was
  introduced and the existing firmware barrier/manual-helper semantics remain.
- Tariff optimizer attributes remain available live and to diagnostics/restore
  state but are excluded from Recorder persistence. Every overlapping delayed
  recalculation task is tracked and cancelled at unload. The fallback daily-
  LOAD input is now watched, so a real fallback change invalidates the result.
- Normal tariff input events and the periodic timer are coalesced into one
  five-minute planning cadence. Result invalidation and live safety gates remain
  immediate; only replacement-plan publication is rate-limited.
- Aurora frontend revision **19** enlarges the flow-node status, power and
  battery detail/ETA text. Available unchanged HA values remain visible instead
  of aging out on `last_updated`; unknown, unavailable, idle or implausible
  inputs still render `ETA —`. This display rule is not execution authority.

These fixes intentionally change execution authority, rollback/readback
lifecycle and battery-node presentation. They do not change RCE or tariff
optimizer objectives, slot selection, forecast/tariff mathematics, RCEm
voltage calculations or direct-register semantics, Off-Grid priority,
registers `258/259/306`, or the documented parallel broadcast/per-Slave
acknowledgement claim boundary.

### Exact candidate offline and CI evidence — PASS

Frozen runtime SHA
`42414d55c52406ae04cdd9f495388f550c24ad76` passed the complete applicable
offline gate:

- quick automation matrix **488/488** and exhaustive matrix **2064/2064**;
- RCE **75/75**, RCE history **3/3**, tariff, RCEm, shared energy/LOAD/power,
  firmware/readback, optimizer executor/startup and source-device rebind
  **6/6** suites;
- release and frontend validation, including 42 Aurora frames for PL, EN and
  bootstrap-first execution, plus deterministic generated assets; and
- diagnostics, identity/privacy contracts and the bounded 100-archive analyzer.

Independent review reported no open P0/P1/P2. Exact-SHA GitHub Actions
`workflow_dispatch` run **31982161039** passed `project-checks`, the conditional
`firmware-compile`, Hassfest and HACS validation. This is offline/CI evidence;
it does not substitute for either live gate.

### Final live rollout — 2026-08-20/21

The earlier localhost Core outage was traced to a loader-visible deployment
backup, not a runtime regression. After the backup was moved outside
`custom_components`, the release runtime and managed assets were deployed on
localhost and `miernik.com.pl`. `ha core check`, controlled restart, integration
setup, fresh physical FC03/readback and Aurora revision 24 passed on both sites.

During the final simultaneous observation from `2026-08-20T22:30:25Z` to
`22:45:45Z`, localhost tariff readiness was **16/16**, the status remained
enabled/waiting, there were no ready/status transitions, and three subsequent
five-minute planner cycles remained ready. Self-Use and no owner remained
stable. No integration or frontend console error was observed.

An earlier controlled meter test had RCE ownership in **16/16** samples and
physical grid discharge in **13/16**. Initial Self-Use transitions coincided
with operator power adjustments; the final eight minutes were stable, and the
manufacturer application confirmed Grid Discharge and the later Self-Use
return. A later ordinary result-refresh gap exposed the failsafe interaction
addressed by the final latched-execution scheduler guard.

In the final simultaneous meter window RCE readiness was **16/16**, but the
current slot was not planned in **16/16**, so owner and active discharge were
correctly absent. This proves stable neutral readiness, not an active exact-
final slot. The maintainer explicitly accepted publication with this evidence
limit and a hotfix path if the next active slot regresses. It does not relax any
live BMS, GCF/export, ownership, Off-Grid, FC03/readback, SOC or topology gate.

### Transition-history evidence — bounded to this installation and date range

Home Assistant history was inspected for the test installation over the 8–14
August evening windows, 19:00–22:00 local. An approximately **60 kW** stop
transient is stored on 8 August. An approximately **60 kW** start transient is
visibly aligned with the stored mode-code change on 14 August. The 9–13 August
windows show repeated discharge plateaus and switching impulses, although the
recorder cadence may have missed their full instantaneous peaks.

The separate 15 August live trace, outside that evening-history range at 18:20
local, captured **63.069 kW battery power / 65.910 kW inverter power** during a
switch. It is correlated by the live trace, not presented as part of the
8–14 August 19:00–22:00 history review.

This observation supports a transition grace followed by a three-generation
stable window. It does not prove that every HIT installation has the same
peak, does not convert a peak into accepted steady response, and does not prove
execution by a named Slave. The exact v1.5.6 build must still be retested with
separate retained Master/Slave evidence.

## v1.5.5 consolidated EMS safety release — offline PASS and published; exact-version live software acceptance pending

The v1.5.5 release consolidates the previously unpublished v1.5.4 candidate
and restores the previously proven Modbus address-0
FC16 write for the complete EMS block 4300-4306 on a Master/Slave plant. The
write is assembled only from a fresh, complete physical Master snapshot and
never publishes optimistic state. Success requires a later Master FC03
generation with all seven registers exactly matching the requested block.
This is deliberately described as **system broadcast with Master FC03
confirmation**, not as an acknowledgement from every Slave. The broadcast
reaches only inverters physically connected to the same external Modbus/RS485
bus as the ESP32 converter; the internal parallel network does not relay it.

The capability split remains fail-closed: RCE, tariff control, manual cycles
and battery balancing may use the verified 4300-4306 broadcast, while direct
registers 258, 259 and 306 remain blocked on Master/Slave. Consequently active
RCEm voltage control is still unavailable there; RCEm shadow mode remains
available. Internal addresses 6050/6055/... remain diagnostic and are not used
as prerequisites for the address-0 broadcast.

Fresh offline evidence for this candidate:

- ESPHome 2026.7.2 configuration and full clean compile with ESP-IDF 5.5.5
  from the complete local verification fixture: **PASS** (`Configuration is
  valid!` and `Successfully compiled program`). The resulting image used
  **973,135 / 1,835,008 bytes (53.0%)** of the application flash budget and
  **78,376 / 180,736 bytes (43.4%)** of DRAM; the fixture and all 19 package
  inputs remained byte-identical before and after the build.
- Firmware FC03/readback source contract: **PASS**.
- Release validator: **PASS**, 292 localized entities.
- Automation matrix with PyYAML structural checks: quick **488/488** and
  exhaustive **2064/2064**. The exhaustive set contains 576 RCE, 720 tariff,
  648 RCEm and 120 randomized RCE boundary scenarios, including four explicit
  BMS fail-closed cases. Both the implementation author and an independent
  read-only reviewer obtained the same result. After the v1.5.5 package marker
  was applied, the exact scheduler hash was
  `82689208546C2CA080ABB187EB30FA63FA5F712A31557C89B4AFEBAEF3379BCB` and
  the matrix hash was
  `A7F8CDB898D2A3980C2B9B9CDB47541AD0A9A20C21659B8C6DA45DBDA7B88871`.
- Core optimizer and integration contracts from `RELEASING.md`: **13/13 runs
  PASS**, including 69 deterministic RCE scenarios, RCE/RCEm history, tariff
  profiles and solver, RCEm solver, startup/listener lifecycle, executor
  serialization, parallel power balance, shared freshness/load models,
  physical FC03 and source-device rebind. `validate_release.py` and
  `py_compile` and `compileall` for all 66 Python files also passed on the exact
  v1.5.5 workspace.
- Aurora card/strategy validator: **PASS**; PL, EN and bootstrap-first paths
  each render exactly 42 Aurora frames.
- Asset generator: **PASS**, 292 localized entities and zero tracked-content
  differences in an isolated rebuild (147 tracked files compared).
- Diagnostic identity: **PASS** for first-start UUID v4 generation, persistent
  reload, concurrent first access, multi-entry reuse, save-failure retry,
  schema enforcement and exact redaction preservation.
- Batch diagnostic analyzer: **PASS** for 100 ZIP archives, bounded hostile-ZIP
  handling, semantic/history deduplication, RCE/RCEm/tariff rules,
  deterministic transactional output and default UUID/path privacy.
- Stable BMS voltage/current FC03 reports are now forced through ESPHome so
  their Home Assistant `last_reported` provenance remains current. The RCE
  Safe BMS diagnostic consumes the optimizer's authoritative
  `bms_discharge_power_limit_kw` and the same freshness/age contract instead
  of recomputing a misleading value from stale raw states.

On 2026-08-14 exact firmware commit
`11ee7c7306a2435059bf820b10bdf0a6be90c65d` was configuration-checked, flashed
and exercised at miernik.com.pl. The Master accepted Grid Discharge and its
physical FC03 readback changed accordingly; the later return to Self-Use was
also physically visible. No Home Assistant writer issued that later mode
change, so it was not caused by the restored queued FC16 path.

The test installation's ESP32 external RS485 cable was then found to reach the
Master only. That wiring can still expose valid topology, system-wide telemetry
and Master FC03, but it cannot deliver a wire-level address-`0` frame to the
Slave. The run therefore validates the Master path only and neither proves nor
disproves Slave execution.

The archived working setup is stronger evidence: the 2026-07-29 test connected
one ESP32 to the `RS485_2` ports of both inverters on one A/B/GND multidrop bus.
At an 80% discharge setting it reached **34.86 kW export** and **516.8 A** after
about 27 seconds, then returned to Self-Use without alarms. Commit
`25d3bbeaeac87976ac5b59266af61f35c33cb91b` preserved that exact address-`0`
FC16 implementation and explicitly documented every Slave on the shared
`RS485_2` bus.

Those earlier runs did not complete exact-version parallel software acceptance.
Master FC03 alone is not per-node acknowledgement, and aggregate
physical-response acknowledgement was not implemented in the candidate.

### 2026-08-15 shared-bus run — hardware/protocol evidence only

This run used managed Home Assistant package **1.5.4** and ESP32
firmware/project **1.5.3**. It therefore tests the physical shared bus and the
address-`0` protocol path, not the exact v1.5.5 software and not a v1.5.6
candidate. All times below are local Europe/Warsaw observations.

| Time | Recorded observation |
|---|---|
| 18:20:03 | The manual discharge owner became active. This is the first exact start-side ownership timestamp, not a claim about a later software version. |
| 18:20:07 | The first physical response was recorded, 4 seconds after ownership became active. |
| by 18:20:29 | Physical Master FC03 reported raw EMS mode code `5` (Grid Discharge), no later than 26 seconds after ownership. |
| 18:21:49 | Stable aggregate sample: grid export 29.087 kW, LOAD 7.307 kW, PV 2.741 kW and battery discharge 33.653 kW. |
| 18:22:12 | Second stable aggregate sample: grid export 29.232 kW, LOAD 7.353 kW, PV 2.722 kW and battery discharge 33.863 kW. |
| operator observation, exact time not retained | The operator reported Grid Discharge on both nodes in the manufacturer application. No per-node power, screenshot or exact application timestamp was retained. |
| 18:22:41 | The discharge timer was first recorded as idle. The operator had been instructed to stop manually; the exact stop command/service timestamp was not captured. This is a stop-side marker, not proof of an automatic stop. |
| by 18:23:25 | Physical Master FC03 reported raw EMS mode code `0` (Self-Use). The 44-second interval from the first idle-timer observation is not command-to-ACK latency because the actual manual stop time is unknown. |
| by 18:23:46 | Manual ownership and the observed execution flags were off and aggregate power was in the safe post-discharge state. The 65-second interval from the first idle-timer observation is likewise not exact stop latency. |
| 18:24:21 | Final observation remained safe; SOC was 97% and the recorded fault set was clean. |

Across the before, active and after observations, registers `4301–4306`
remained `20 / 90 / 70 / 50 / 30 / 100`. GCF registers `258/259` remained
`1 / 200`, battery maximum charge register `306` remained `100`, and no new
fault was recorded. The two stable aggregate samples were 23 seconds apart and
are consistent with physical contribution beyond a single 20 kW inverter, but
aggregate power is corroboration rather than an acknowledgement from a named
Slave.

The manufacturer-application observation supports the conclusion that both
nodes entered Grid Discharge, but its retained evidence is limited to the
operator report. It does not establish separate per-node power or an exact
vendor transition time. The later raw codes `5` and `0` are physical FC03
evidence from the Master only. Because the stop was requested manually and its
command timestamp was not retained, this run must not be described as an
automatic-stop test or assigned an exact stop latency.

Before v1.5.6 can be accepted, repeat the complete sequence on its exact
candidate versions. Capture exact start and stop service timestamps, separate
timestamped Master and Slave mode and power evidence, both Master FC03
transitions, stable aggregate samples, ownership/timer release, unchanged
protected registers and the final fault-free safe-power state.

During the 2026-08-14 live audit,
`sensor.solcast_pv_forecast_prognoza_na_dzien_3` was initially disabled in the
Miernik entity registry and was then enabled by the user. The native source
reported `146.9107 kWh`, a 14-second signed age and 48 detailed half-hour rows
at the verification point. The currently deployed older RCE sensor selected
that entity, while the older tariff sensor still reported Day 3 as `missing`
and retained its two-day horizon. Exact-snapshot live acceptance therefore
remains pending after deployment: both new planner diagnostics must report the
same entity as `fresh`, and tariff must select its three-day horizon. Missing or
stale Day 3 continues to use the safe shorter-horizon fallback.

Off-Grid operation was also observed as functional on the local inverter. That
observation proves the physical inverter mode, not yet the arbitration changes
in this v1.5.5 Home Assistant package. The final quick and exhaustive matrices
prove that automatic starts, active updates, timer recovery and cleanup yield
to physical mode code `3`; exact-snapshot live acceptance remains pending after
deployment.

## Representative systems

| System | Inverters | Battery | Daily PV | Home/day | Home/night | BMS current |
|---|---:|---:|---:|---:|---:|---:|
| HIT-10 | 1 × 10 kW | 10.2 kWh | 12 kWh | 8 kWh | 3.2 kWh | 100 A |
| HIT-15 | 1 × 15 kW | 21 kWh | 28 kWh | 16 kWh | 6.5 kWh | 175 A |
| HIT-20 | 1 × 20 kW | 40 kWh | 55 kWh | 28 kWh | 11 kWh | 250 A |
| Parallel HIT-20 | 2 × 20 kW | 230 kWh | 120 kWh | 48 kWh | 19 kWh | 700 A |

The values are representative test fixtures, not manufacturer performance
claims. They intentionally combine undersized and oversized storage, weak and
strong PV, ordinary and winter-like load, parallel operation and BMS limits
below inverter power.

## Scenario-matrix result — v1.5.2 offline candidate

- **Quick matrix:** 488/488 scenarios passed.
- **Exhaustive matrix:** 2064/2064 scenarios passed.

The complete sweep covers:

- inverter power of 10, 15, 20 and 2 × 20 kW;
- SOC below reserve, mid-range and nearly full;
- no PV, a severe forecast miss, ordinary production and overflow;
- today's and tomorrow's price peaks, negative prices and blocked export
  periods;
- G11, G12, G12w and G13, weekday/weekend/holiday and DST boundaries;
- 240.0–253.2 V, zero-export and user export caps, full batteries,
  insufficient headroom and BMS-limited power;
- power/energy invariants for every half-hour slot, conversion losses,
  minimum-SOC floors and end-of-horizon battery bounds.

## RCE v1.5.2 acceptance checks — passed offline

- The protected energy reserve is rounded **up** to a full 1% SOC step and is
  enforced for every planned export slot, not only at the end of the plan.
- The model distinguishes energy available now from energy expected later from
  PV, so a 48-hour export plan cannot silently treat forecast energy as current
  battery energy.
- RCE keeps a revenue-first objective across a bounded joint horizon. Day-three
  availability, forecast, expected LOAD, and shortfall remain explicit
  diagnostics; they do not add a terminal objective to the sale planner.
- The active-set implementation is explicitly heuristic. An independent oracle
  compares small constructed horizons as regression evidence; it does not prove
  exact or global optimality for the full mixed-constraint problem.
- Gross sale revenue and estimated net benefit are separate. Net benefit can
  subtract modeled battery wear, but no terminal household-energy value is
  reported as realized sale revenue.
- Charge and discharge plans share physical inverter/BMS/AC/export budgets,
  respect energy-arrival time, and do not count natural PV export twice.
- Export lockout, GCF/zero-export, physical system/BMS limits, stale inputs and
  Master/Slave readiness are fail-closed.
- Official PSE `dtime_utc` values are treated as 15-minute interval ends. The
  parser validates `period_utc`/`business_date`, supports the live `24:00`
  label, and reconstructs exactly 48 normal-day, 46 spring-DST and 50
  autumn-DST half-hours without shifting the current price.

## Tariff-charging v1.5.2 acceptance checks — passed offline

- Planning uses a real rolling horizon of at least 48 hours when fresh day-three
  Solcast data is available, including 47/48/49-slot DST days.
- If the third-day tail is missing or stale, the unknown period is modelled as
  zero PV plus average household load and a conservative reserve, capped by the
  user's maximum SOC.
- The plan exposes the exact PV and LOAD values used by the optimizer instead
  of displaying only unrelated live sensors.
- Effective Grid Charge power can be learned from confirmed charging sessions.
  The state is explicit: not observed, collecting, learned live or restored
  from previous evidence. Learning never occurs from Self-Use or a zero-power
  sample.
- Charging lead time includes household load and conversion losses. A required
  10 kWh cannot be postponed to the last minutes of a cheap window when the
  available battery-charging power needs a full hour or longer.
- G11 does not cycle the battery merely because all hours have the same price.

## RCEm 253 V+ v1.5.2 acceptance checks — passed offline

- RCEm starts in **observation-only (shadow)** mode, performs no inverter
  writes, and does not change certified grid-protection settings.
- Voltage risk uses per-phase history and robust recurring-window detection;
  current voltage can override historical expectations when the grid is calmer
  or worse than usual.
- The planner combines interval PV, weekday/weekend household LOAD, battery
  headroom, BMS limits and the user's legal/contractual export cap.
- Pre-discharge is permitted only when it creates useful headroom before a
  later high-voltage/PV-risk window and still protects household energy.
- Missing/stale profile data degrades to a conservative plan. At night, a
  supposedly safe live export power is deliberately reported as unavailable
  rather than invented from non-representative conditions.
- Multiple risk windows retain operational headroom independently; energy
  prepared for an early window is not double-counted for a later one.

## Static control-contract checks for v1.5.2 — passed offline

These repository tests verify scheduler markers, interlocks and planner
contracts statically. They do not execute a real Home Assistant automation or
a Modbus write/read-back cycle; live acceptance evidence is documented
separately below.

- Exactly one owner may control EMS: RCE, tariff charging, RCEm, battery
  balancing or a manual schedule.
- Mode, SOC and power writes are idempotent and require read-back confirmation.
- Freshness gates cover SOC, price, forecast, plan, inverter availability and
  parallel topology. Persistent loss of required control data returns the
  inverter to Self-Use.
- RCE decisions are latched to the selected 30-minute block; tariff targets are
  latched to the required charging window. This prevents rapid mode chatter.
- Notification debounce and fingerprints suppress repeated phone alerts while
  preserving a genuinely different stable state.
- The Home Assistant 2026.8 source-device contract preserves the configured
  composite anchor, accepts only one ESPHome-owned successor with matching
  native entity evidence, rejects ambiguous or contradictory candidates, and
  prevents a second integration entry for the resolved child.

## v1.5.2 RC5 frontend finding and RC6 final audit — 2026-08-14

- RCE optimizer: **68/68** deterministic scenarios passed, including the
  independent small-horizon oracle, padded 48-hour regressions, the exact live
  PSE interval-end shape, payload reordering, `24:00`, and both DST day lengths.
- Automation scheduler: **488/488** quick and **2064/2064** exhaustive
  cross-system scenarios passed.
- Tariff, RCEm, history reconstruction, policy-neutral energy/LOAD/power
  helpers, diagnostics, executor offload and the frontend card all passed.
- The heavy 96/110-slot solver wall-clock regressions use a **1.0 s**
  shared-runner ceiling, while the small horizon-clamp fixture retains its
  tighter **0.5 s** guard and event-loop executor offload remains an independent
  contract. This RC3 adjustment changes only test thresholds, not the
  production optimizer algorithm.
- The physical FC03 actuator-readback and ownership contract passed; generated
  Polish and English assets were deterministic and `validate_release.py`
  reported **291** localized entities with no structural or localization error.
- ESPHome **2026.7.1** accepted the complete local fixture and compiled it with
  ESP-IDF **5.5.5**. The resulting application used 43.3% RAM and 52.9% flash.
- A read-only audit found no open P0/P1 before RC2 deployment, but the local
  Home Assistant 2026.8 run then exposed the composite-device P1 described
  above. The RC3 rebind contract passes **6/6** offline: unique successor,
  revalidated previous successor, ambiguous-candidate rejection, unchanged
  live source, duplicate-entry rejection and invalid linkage rejection.
- RC3 subsequently passed its exact-SHA GitHub project checks, HACS Action,
  Hassfest and the exhaustive matrix. On local deployment the verified
  successor was persisted and native ESPHome physical EMS readbacks reported
  live values, confirming that the rebind itself worked.
- The same local deployment exposed a new P1: `HoymilesSensor.available`
  applied the parallel-topology readiness gate to every ordinary sensor proxy.
  Stable physical EMS readback proxies therefore remained unavailable despite
  their live native sources. RC4 limits this gate to `PARALLEL_POWER_TARGETS`;
  its dynamic ordinary-proxy regression passes.
- RC4 then passed exact-SHA push and workflow-dispatch CI, including the
  exhaustive matrix, and was deployed locally. The rebind, native readbacks,
  ordinary proxies, setup readiness and fail-closed safety state were all live.
- The live 96-row PSE payload exposed another independent P1. Its first
  `dtime_utc` was `22:15` for local `00:00 - 00:15`, proving that the field is
  the interval end. Treating it as a start produced 47/48 half-hours and safely
  held RCE in `missing_data`, but made the engine unusable. RC5 subtracts 15
  minutes on the UTC timeline and validates the accompanying metadata.
- Full RC5 offline validation passes: `validate_release.py`, the physical
  firmware/readback contract, executor/startup offload, source-device rebind
  **6/6**, quick **488/488**, exhaustive **2064/2064**, RCE **68/68**, RCE
  history **3/3**, tariff, RCEm, frontend and dynamic power-balance suites.
- RC5 passed exact-SHA push and workflow-dispatch CI and was deployed to both
  test installations. Its PSE interval-end mapping and backend fail-closed
  safety state were correct live.
- The parallel dashboard exposed an independent frontend P1. Lovelace requested
  `/api/hoymiles_hit_modbus/static-r2/hoymiles-rce-chart-card.js?v=1.5.2.15`
  before the custom route was ready, received a transient `404`, and continued
  receiving that cached response after restart. It then hit the five-second
  timeout while waiting for
  `ll-strategy-dashboard-hoymiles-hit-xxl-g3`.
- The RC6 target uses one canonical full module at
  `/local/hoymiles-rce-chart-card.js?v=1.5.2.16`. Before publishing the URL, the
  installer atomically materializes the card, bootstrap, PL/EN dashboard JSON
  and image. Storage mode is reconciled through Lovelace's live resource
  collection, while runtime installation does not directly mutate
  `.storage/lovelace.*`. A fresh installation without `config/www` copies the
  files, defers publication and raises a restart Repair. An already-open browser
  session requires a hard refresh after restart.
- Exact runtime commit `7ce13155533bfa0bf9752a0fd201224dac1a7393`
  passed push CI and `workflow_dispatch`; the exhaustive workflow completed
  successfully.

The earlier deterministic, packaging, firmware and RC5 backend evidence remains
valid. Together with the RC6 exact-SHA CI and live results below, it supports GO
for the tested Home Assistant runtime/frontend. Meter ESP32 firmware acceptance
remains separate and pending; no firmware was flashed during this audit and no
protected EMS writes were enabled.

## RC6 frontend startup contract — offline and live PASS

The exact runtime candidate demonstrated the following:

- one canonical full, versioned `/local` module registers the dashboard strategy
  before Lovelace's five-second deadline, and loading it again is idempotent;
- the card, bootstrap, both localized dashboard JSON files and image exist under
  `config/www` before the resource URL is published;
- storage-mode migration uses Home Assistant's live Lovelace resource collection
  and preserves user dashboards and `.storage/lovelace.*` files from direct
  runtime mutation;
- a fresh installation with no pre-existing `config/www` remains restart-gated,
  creates the localized Repair and succeeds after the required restart;
- optional asset `OSError` and live-resource failure are fail-soft: integration
  setup completes, no incomplete module is published, and Repair is created;
- YAML-mode loading remains supported; and
- after restart and hard refresh, the version-16 resource returns successfully,
  the dashboard renders, and the browser console contains no strategy timeout.

## RC6 live installation audit — PASS with one degraded-history observation

| Check | Local installation | Parallel meter installation |
|---|---|---|
| Home Assistant | 2026.8.1 | 2026.7.4 |
| Exact candidate | Integration and managed-asset hashes matched runtime commit `7ce13155533bfa0bf9752a0fd201224dac1a7393` | Runtime and asset hashes matched the same commit |
| Installation state | Integration/package 1.5.2; **Ready** | Integration runtime 1.5.2; ESP32 firmware update still pending |
| Frontend | Version-16 `/local` assets returned HTTP 200; fresh-tab and hard-refresh dashboard checks passed | All required version-16 `/local` assets returned HTTP 200; hard refresh rendered the Aurora dashboard |
| Control safety | Self-Use; every controller owner off | RCE and tariff off; RCEm enabled in shadow only; every owner and execution off |
| Firmware action | No firmware action was part of this frontend audit | No firmware was flashed |

One bounded background RCEm Recorder-history query timed out once during the
final observation. The optimizer stayed fail-closed, execution and all owners
remained off, no inverter write was attempted, and the timeout did not repeat in
the observation window. This is recorded as observed degraded history
availability. The bounded timeout and safe fallback behaved as designed, so it
is not an RC6 Home Assistant runtime/frontend release blocker.

## Exact-SHA traceability after documentation finalization

The deployed and live-tested runtime SHA is
`7ce13155533bfa0bf9752a0fd201224dac1a7393`. This final report changes
documentation only, so its eventual commit and release/tag SHA will necessarily
be later and different. Record that future SHA separately and verify that the
delta from the runtime candidate contains documentation/release metadata only.
Do not describe the future documentation/tag SHA as the candidate installed on
either Home Assistant host.

## Archived v1.5.0 live candidate acceptance — 2026-08-12

The same candidate archive was installed on a single-inverter Home Assistant
test system and on the two-inverter field system at `miernik.com.pl` during the
2026-08-12 01:06–02:00 CEST deployment and capture window. The
archive SHA-256 was verified before installation on both hosts. Home Assistant
configuration validation passed, the managed EMS package and Aurora dashboard
were synchronized, and no dashboard configuration errors remained after the
required restart cycle. Candidate archive SHA-256:
`260EC0A1B6374003298CAD60F76A4AD43FEC74C687FD7C1B6110508F489016CC`.

Observed field checks on the parallel installation:

- **RCE:** the protected reserve was 57.50 kWh (25% of a 230 kWh battery),
  current energy above reserve was 66.70 kWh and the projected end-of-horizon
  SOC was 28.3%. The plan stayed above the control reserve in every selected
  block and selected the highest-value allowed half-hour periods.
- **Tariff charging:** TAURON G12w produced `no_charge_needed`; 298.88 kWh of
  conservative modelled PV covered 83.41 kWh of modelled household demand.
  The missing day-three tail was visible and conservatively protected instead
  of being treated as free PV. No unnecessary Grid Charge cycle was created.
- **RCEm 253 V+:** four days and 70,915 phase-voltage samples identified three
  recurring risk windows. Daily maxima included 263.3, 253.6, 254.9 and
  257.1 V. The model found about 11.88 kWh of expected surplus in the risk
  windows, calculated independent headroom for each one and remained in
  observation mode. This is useful site evidence, not permission to change
  certified inverter protection thresholds.
- **Parallel readiness:** both inverters reported ready and the controller
  remained in Self-Use while the automatic modules were disabled for review.

The local zero-export installation also behaved correctly: RCE was blocked by
GCF at 0%, tariff charging reported no required charge, and RCEm remained an
observation-only diagnostic. These observations confirm the expected safe
behaviour on these two materially different configurations; they do not prove
behaviour on every possible installation.

## Remaining field-test limits

The simulations validate planning arithmetic and software safety invariants,
not inverter firmware, Modbus transport, wiring, an electricity meter or the
distribution grid. RCEm still requires observation on a real high-voltage
export site before it can be described as field-proven. Parallel command
propagation remains dependent on the physical Hoymiles topology and firmware.

This evidence can support a documented commissioning or acceptance process,
but it is **not a formal certificate** for the inverter, battery or complete
installation. See [Safety, compliance and commissioning evidence](SAFETY_AND_COMPLIANCE.md).
