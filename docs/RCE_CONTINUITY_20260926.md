# RCE continuity repair — 2026-09-26

## Scope and baseline

Target audit: `installation_3` (a distinct installation from installation_1
and installation_2). Base: `5f5f4efb17548c53f40fd29cb3b240d9a10539c6`.
Read-only host SHA256 matched the base for all three changed production files:
`rce_sensor.py`, `rce_optimizer.py`, `supervisor_sensor.py`.
Implementation is isolated in branch `fix/rce-publication-20260926`; unrelated
root checkout changes and AGENTS.md are preserved.

## Observed failures

1. At 19:03:52, 19:21:52 and 19:33:57 CEST the active RCE transaction stopped
   with `authorization_lost` / `authorization_mismatch` while publication was
   pending. The first sequence made six solver attempts without a new plan.
   Recorded LOAD changed 33 times in a three-minute interval. Local controlled
   replay reproduces starvation both with identical numeric reports carrying
   new timestamps and with varying LOAD/PV during a four-second solver job.
   Historical logs do not identify the exact invalidating field of each call.
2. At 19:51:27 CEST an unsent 6.0 -> 5.9 kW retarget was followed by STOP after
   about 76 ms. Core logs identify `pending_source=rcm_plan` and failed predecessor
   attestation. RCEm was enabled, all three actuator-active flags were off, and
   its fresh plan was `restore`, without an emergency. The old predicate rejected
   that action before testing the unchanged GCF/306 rollback baseline. Local
   adapter replay reproduces and fixes this predicate. The historical previous
   RCEm transaction record is not available, so its cleanup state is not claimed
   as independently proven.
3. At 20:56:42 CEST SOC reached 44%, matching the active command's physical 4305
   floor. The plan was current and continuation remained eligible. This boundary
   is retained; the newer planner floor42 does not silently lower active authority.

## Resulting contracts

- A slow optimizer cannot publish its old result merely because the selected
  trade still exists. Captured inputs are immutable. Source, intent, market,
  topology and quality remain guarded; accepted telemetry refreshes require
  monotonic timestamps and current freshness.
- Immediately after the worker returns, the adapter captures fresh inputs and
  synchronously revalidates the fixed selection using the shared physical model.
  It cannot add export slots, increase their energy/current power or extend the
  run. Full-horizon economics also checks the cost of later imports, battery wear
  and retained energy. No await separates fresh validation and publication.
- Failed feasibility or freshness does not become `result_current=true`.
  Stable results with zero execution authority remain publishable.
- Revalidation is reported as such, not as a new proof of global optimality.
  Public timeline remains bounded to48h; its internal economic check uses the
  complete supported horizon.
- Enabled, non-acting RCEm `restore`/`release_export` may retain only the verified
  previous RCE command while an unsent successor is cancelled. Fresh matching
  GCF/306, no unresolved RCEm rollback, no active RCEm actuator/emergency,
  valid RCE window and unchanged safety/ownership/BMS gates remain mandatory.
- No change to lease TTL, hard deadline, reserve protection, physical STOP,
  firmware, user settings, tariff algorithms or deployment topology.

## Validation and evidence

New regressions: `test_rce_plan_revalidation.py`, `test_rce_publication_refresh.py`,
`test_rce_publication_continuity.py`, `test_rce_enabled_rcm_retention.py`.
Existing optimizer, lifecycle, retarget, lease, shared-control and safety suites
remain applicable. New gates are wired into the existing CI workflow.

Local evidence directory:
`<PRIVATE_EVIDENCE>/2026-09-26_rce_third_host/`.
It contains the plan, read-only Recorder transcriptions, RED/GREEN logs,
independent reviews, final validation manifest, exact candidate SHA and package
hashes. Transcribed screenshots are explicitly distinguished from raw exports.

Virtual 270-second integration tests exercise production optimizer publication,
YAML helpers, Supervisor and an ESP lease model. They do not establish actual
Home Assistant scheduler timing or real-inverter acceptance. Local benchmark
latency does not establish target hardware latency.

The historical release-freeze validator is not weakened. Its frozen historical
SHA/manifest checks and current candidate validation are separate evidence layers.
Host deployment, loaded-code verification and real discharge continuity require
their own recorded outcome. This document does not claim deployment or release.
