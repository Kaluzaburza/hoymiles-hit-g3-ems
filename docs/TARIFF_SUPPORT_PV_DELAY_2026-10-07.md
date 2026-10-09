# Tariff support and RCE PV Delay — 7 October 2026

Only Installation 3 deployment is authorized. Base: e6d08c664fc81321a58ec9c5b3197e7e4de18f6c.
No publication, other-host changes, firmware, polling or Recorder expansion.

## Tariff support

For already confirmed grid support, a fresh coherent battery excursion can wait
until 60 seconds after the last confirmed physical cohort. Repeated excursions
and completed replans cannot reset this boundary. Raw evidence stays contradicted;
no lease renewal is authorized by the filter. Recovery needs a new good cohort.
Expiry, stale/mismatched FC03, missing/incoherent power, export, loss of BMS,
SOC/readiness/consent/plan, original deadline and lease expiry remain stop gates.
Incomplete telemetry retains its shorter existing watchdog; no grace stacking.
Charging and RCE SELL retain their existing physical contracts.

The support target is floor(current SOC) minus two percentage points on entry,
latched for that command. Existing minimum/maximum validity checks still apply.
The separate manual Mode 4 issue is deferred at the user's request.

## RCE PV Delay

The adapter used Pstryk's aggregate minimum-efficiency power cap and quantized
fixed RCE exports again. A legal native RCE sale could therefore fail its own
baseline replay, withdrawing a profitable earlier PV delay. In the reconstructed
7 October inputs the mismatch was about 0.05 kWh in a later sale.

Each fixed export now passes RCE's native per-slot power check (house/export
efficiencies, BMS, AC, GCF, live current-slot limits and integer command) before
exact-energy replay. Existing stock/reserve/origin, tariff barrier, lower-PV
refill, recovery-time and whole-window constraints remain. The optimizer result
and fixed sale are not modified. No execution or provider setting is changed.

Tests reproduce failure on the deployed base, success after repair, and rejection
when BMS/export limits fall or telemetry is stale. Field-input replay is an
approximate reconstruction of sequential UI snapshots, not a historical solver
trace. Offline success does not establish physical execution.

## Evidence layers

Exact test SHA/tree, log hashes, package/manifest, verified backup, HA checks,
restart and settings restoration are required for technical deployment PASS.
Natural start, matching new FC03 generations, accepted-renewal continuity,
replans, original deadline and end/restore/postflight need separate field evidence.
The prior localhost plan withdrawal remains only partially diagnosed; this change
does not claim to reconstruct its missing solver inputs or triggering callback.
