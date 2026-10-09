# EMS control continuity — 30 September 2026

Base: `0edd4c93663db116aac98f92d55cb154f8c64073`. This branch excludes
the later Recorder changes. Deployment scope authorized by the user is
installation_3 and installation_2, with independent preflight and rollback.
installation_1 remains owned by the Recorder work; merging and promoting Recorder
to the other installations requires a separate decision.

## Changes and contract

- C1: Home Assistant changed power samples enter the same four-channel
  collector as unchanged state reports. The existing 350 ms collection
  window, frozen timestamps and fresh GRID contradiction gate are retained.
  Incomplete cohorts do not prove tariff execution.
- C2: bounded renewal during RCE recalculation uses the accepted transaction,
  retained plan slot and continuation gates, not the unavailable legacy
  presentation helper. Its callback cannot invalidate a prepared retarget.
  Consent, BMS/GCF, reserve, physical FC03 proof, original hold anchor and
  immutable hard deadline remain mandatory. Renewals must expire before
  the current run end as well as the original hold/deadline. No TTL increase.
- C3: `input_number.hoymiles_rce_minimum_net_export_power` sets the minimum
  planned net RCE export, initially 2 kW for a new helper. A restored user
  value persists. It is checked after LOAD/PV balance and register 4306
  quantization. Infeasible tails may move only to selected adjacent slots
  under full feasibility/economics; otherwise they remain self consumption.
  This is not an instantaneous power STOP rule or an instruction to exceed
  BMS, reserve, AC bridge or export limits. Tariff's existing minimum is unchanged.

## Validation and evidence

RED/GREEN and exact-candidate records are stored in
`<PRIVATE_EVIDENCE>/2026-09-30_EMS_CONTINUITY_CONTROL`.
Tests cover all 16 changed/reported power combinations, missing/expired LOAD,
tariff physical confirmation, slow RCE replan with real YAML helpers,
withdrawn consent/slot/reserve, stale FC03, Master STOP, shortened run,
retarget and terminal restore, and minimum net power/caps/tail packing.
The pending-dispatch fixture explicitly sets the pause state normally set by
entity lifecycle. The FC03 callback fixture also drains the independent
partial-power expiry timer now reached by changed power samples.

Known baseline failures are retained separately: fresh-install publication
count and the host-dependent real-horizon solver performance ceiling. The
historical release freeze assertion is not weakened. Offline validation,
technical installation, runtime loading and natural field acceptance are
separate evidence layers. Existing M01 observations remain paused; no old-SHA
elapsed time or transaction is transferred to this candidate.
