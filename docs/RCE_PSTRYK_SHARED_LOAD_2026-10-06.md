# Shared LOAD and RCE/Pstryk points 1–4

Authorized candidate from e915066, frontend 1.5.8rc2.119, lease 2.
Only Installation 3 deployment is authorized. Public publication, other hosts
and automatic monitoring remain on hold. Exact SHA/tree, logs, archive hashes
and receipts: <PRIVATE_EVIDENCE>/2026-10-06_RCE_PSTRYK_1_4.

## Shared forecast and physical limits

The installation-owned LOAD model supplies nominal full half-hour energy to
RCE, tariff, Pstryk and RCEm. Consumers clip elapsed time once. The broker
carries the common night window, history, quality and persistence data.
RCEm P10/P90 remain explicit policy envelopes, using UTC keys across 23-, 24-
and 25-hour days. PV Delay uses the same planning input; its diagnostic-only
flow contract is unchanged.

Raw power never replaces nominal current-slot LOAD. A single spike cannot
become a half-hour prediction. Existing dense persistence needs four samples
over at least 12 minutes with gaps at most five minutes. Stale or future
observations cannot add a persistence correction. The current slot receives
the same qualified correction as future slots. No new collector, Modbus
polling, Recorder schema or write frequency is introduced.

The fresh physical LOAD/PV cohort still caps execution. RCE/Pstryk discharge
also respects present battery DC stock above the existing protected integer
SOC, remaining slot duration, home/export efficiencies, BMS, AC bridge, grid
limit and whole-percent quantization. Nominal energy and live command limits
are separate. Tariff support can be vetoed by insufficient live demand without
inflating nominal energy.

Pstryk's market fingerprint excludes the separate raw LOAD execution cap.
Changing that cap cannot masquerade as a price/forecast change, while an
undeliverable physical sale remains ineligible. Regression fixtures specify
nominal demand explicitly; zero forecast is not replaced by raw telemetry.

## Diagnostics and continuity

Recorder returns a queued Future. ensure_future preserves ownership, timeout,
cancellation shielding and single-flight protection; create_task(Future)
caused the reproduced diagnostic export TypeError.

Existing unsent-retarget re-attestation is tested through fresh FC03 and an
accepted renewal, with BMS=0 still blocking. No extra retry, longer deadline,
reset stabilization or weaker authorization is introduced. The precise field
callback behind historical M01 was not captured. That cause remains PARTIAL.

## Bounded BUY refinement

Before SELL, at most eight candidate indices are considered for pairwise BUY
undo/swap over three passes (at most 672 extra simulations). Changes must
reduce modeled cost without worsening terminal shortfall or consuming the
baseline terminal stock. SELL cannot increase household BUY. Active runs,
below-reserve restoration and PV-delay alternatives retain the existing solver.

Eleven reproduced counterexamples are covered. A separate 48-case exhaustive
small-instance oracle retained one residual gap about 0.02416 PLN during
development. This is not a guarantee of global optimality or measured savings.
Independent AC/DC, reserve, commitment and computation-budget tests are
required on the final exact SHA.

## Acceptance layers

The full CI/RELEASING union must pass on the clean exact candidate. The host
package requires fresh identity, all 113 baseline hashes, idle ownership,
durable pause/Off, verified backup and HA checks. Restart/postflight and exact
settings restoration have separate receipts. Natural acceptance is PENDING on
this SHA until separately observed; old M01/Slave/PV evidence retains its scope.
