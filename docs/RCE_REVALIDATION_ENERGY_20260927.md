# RCE fresh energy revalidation — 2026-09-27

## Evidence and cause

Base `6484a8249780d144756e83a6c293ebebed2a9fae` repeatedly published
`plan_revalidation_failed` on installation_2 despite fresh required inputs and
an empty missing-entity list. After the retention window, it mislabeled the
rejection as missing data. Diagnostic checkpoint `177215b` was separately
tested and deployed to this host with an exact 87-file manifest and backup.

At 14:49:11 UTC, that runtime reported solver ready, no missing entities,
`fixed_schedule_not_ready` and fixed status `optimizer_error`. The fixed
schedule no longer fit fresh physical energy constraints. Offline replay of
saved site values reproduced this class of failure when LOAD increased.
That replay uses reconstructed freshness and is not an exact historical
solver-input pair. An independent small regression reproduces rejection after
a 0.05 percentage-point SOC drop with no missing inputs.

The solver can select energy up to the protected reserve. The fixed-schedule
revalidation already reduced exports for fresh power limits, but did not
reduce them for fresh available energy. Small LOAD/SOC changes could therefore
discard all selected sales and repeatedly prevent a current publication.

## Contract

The fixed branch walks the existing selection chronologically using fresh
physical balance. If an export crosses its reserve, it reduces that export
by the DC deficit converted to AC energy and rechecks the slot. It neither
adds slots, moves sales, increases energy/power nor runs another optimization.
The complete physical and economic gates still run; infeasible home demand,
stale BMS/SOC, changed intent/prices and uneconomic sales remain rejected.
No physical current or grid protection setting is changed.

Rejected-plan diagnostics name the rejecting gate and changed immutable field
names. An expired rejected plan has its own localized status instead of a
false missing-data label. Diagnostics contain no complete input snapshots.

## Verification and boundaries

`test_rce_revalidation_energy_trim.py` checks 18 SOC/LOAD variations, current
and future sales, 80/95/100% export efficiency, an independent energy balance,
selection/power/run subset, economics, input immutability, reserve rejection
and absence of optimization searches. `--base` reproduces the failure on
177215b. Existing revalidation tests retain their full-horizon economics,
freshness, BMS, GCF and safety-negative contracts.

Evidence directory:
`<PRIVATE_EVIDENCE>/2026-09-27_RCE_REVALIDATION_FIX/`.
Local tests and diagnostic deployment are not final field acceptance.
Natural M01 observation was stopped by the user and is not resumed here.

## Concurrent Recorder work

Storage checkpoint `e541903c9853fed3e0f437bac4967fa2c1aaf38c` is separate and
not deployed. Its five runtime files do not overlap this RCE fix. Preserve its
installation_1 24/72-hour acceptance gate before remote rollout and its disabled
retention proposal; no purge or retention change is part of this repair.
Validate the combined tree before a later storage deployment and use this
repair's final SHA as its new base. Never overwrite RCE with its older base.
