# Tariff, Pstryk BUY and delayed PV charging — 2026-10-05

Historical repair record. The monitor was subsequently stopped by the user.
See the [6 October acceptance scope](releases/1.5.8RC2/FIELD_ACCEPTANCE.md)
for the later deployment and completed cycle; earlier results below retain
their original candidate scope.

Scope: repair the verified overnight localhost failures from base
`d9dcdf684bb3ffdd2cb2b9c4d350582134b724ac`. The owner authorizes fixes and
controlled deployment. The latest instruction supersedes the initial four-host
sequence: iterate only on installation_3 until delayed PV charging succeeds; the other
three installations are HOLD. Public release and ESP flashing are outside this
change. The single installation_3 PV monitor is ACTIVE; the old tariff monitor is PAUSED.

## What the overnight evidence establishes

The external evidence directory is `2026-10-04_TARIFF_WINDOW_STABILITY`,
subdirectory `diagnosis-2026-10-05`. Its original samples, Recorder exports and
negative findings are retained. This document does not replace those records.

| Episode | Established observation | Interpretation boundary |
| --- | --- | --- |
| 1 | No current successor plan within the fixed hold; soft lease expired | Exact cause of prolonged pending remains unproven |
| 2–4 | Lease callback created an anchor 0.35–0.38 s later than the frame consumed by reconcile; valid Mode 4 then lost authorization | Reproduced callback-order bug, not evidence of a missing physical ACK |
| 5 | Charge changed to house support; ACKed Mode 4 did not yet supply the house; the 120 s lease expired within the permitted 180 s confirmation period | Missing bounded support renewal reproduced; cause of inverter's absent effect remains unproven |
| 6 | 57 completed replans preceded a 189 W house-support plan rejected by the generic 200 W minimum | Wrong minimum applied to house supply; not a battery-charging failure |

The sixth cycle restored Self-Use and completed 612.911 seconds of neutral
postflight. Only 39.771 seconds of actual battery charging were captured in
episode 5; that is not full functional acceptance of battery charging. The
episode-1 historical-source hash discrepancy remains an evidence limitation.

## Answers and resulting contract

1. **A started block is a bounded commitment, not an unconditional latch.**
   Ordinary tariff already had optimizer support retention, but the supervisor
   did not publish a commitment for `grid_support`, making that route
   unreachable. It now publishes it only for a live lease, matching full Mode 4
   block, recent physical confirmation, consent, BMS and current ownership.
   Missing data, revoked consent, completed need or physical contradiction still
   stop execution; the original hard deadline cannot increase.
2. **Tariff and delayed PV charging share controller/lease infrastructure.**
   The callback-order fix applies to both bounded holds. Their physical effects
   remain different: Mode 4 supplies/charges from grid, while Mode 5 PV delay
   must prove curtailed battery charging with independent BMS, surplus and
   coherent flows. No tariff charging rule is copied over those safeguards.
3. **Pstryk BUY shared the tariff execution gaps and lacked SELL's retained
   run basis.** BUY now retains an original accepted contiguous run across
   half-hour/hour boundaries, using fresh proof. Remaining energy, prices,
   power, SOC, costs and shortfall are revalidated. A worse or unnecessary run
   is rejected; later purchases cannot inflate the current target. Public
   hourly net price remains the price basis; no fees or fallback are introduced.
4. **The common times were already 120 s lease / 20 s renew / 180 s initial
   physical confirmation.** The fault was path coverage and callback ordering,
   not different constants. House-support ramp now renews only inside the
   existing fixed 180 s allowance, with fresh complete FC03 and critical checks.
   It stays PENDING until the actual effect is proved. No TTL, retry, polling,
   Recorder cadence or hard deadline is increased. Tariff publication also
   gives the existing FC03 coalescer at most 0.5 s to finish, then rechecks input
   revisions/fingerprints. It cannot accept stale inputs or wait indefinitely.
5. **The 200 W minimum belongs to battery charging (and the separate export
   contract), not direct house supply.** Import for the house needs finite
   positive demand and existing economic/reserve eligibility. Small import
   must have proportionately coherent battery/GRID/LOAD/PV evidence; meter
   noise or battery-fed house is insufficient. Battery charging uses command
   headroom after house supply, rather than total import or a whole-slot energy
   average that misclassifies a short valid top-up. The existing economic
   benefit and minimum useful-cycle gates remain in place.

An additional physical regression was reproduced with 1000 W PV, 1189 W LOAD,
zero grid import and 189 W battery discharge. The old 200 W PV-coverage tolerance
incorrectly confirmed house support. Positive house deficits now require the
explicit import proof, including when PV is producing. Tests also reject a 1 W
import that cannot account for the remaining house demand.

## Validation and delivery evidence

New regressions: `test_tariff_stability_night.py`,
`test_pstryk_buy_commitment.py`, `test_tariff_commitment_cohort.py`; extended
PV-delay, minimum-power, real HA sensor and Pstryk runtime tests. They exercise
actual controller/lease code and an independent firmware lease model, callback
offsets from field samples, house power 1–201 W, bounded ramp, vetoes, replan
continuity, unchanged deadline and stale-result rejection.

Full results, source hashes, final SHA/tree and per-host receipts belong in
the external `2026-10-05_EMS_CHARGE_STABILITY` report. Frontend revision 113;
integration/package 1.5.8rc2, lease protocol 2. No ESP source semantics change.
Offline PASS must never be presented as real-inverter acceptance. Each host
requires its own fresh identity, 110-file baseline, settings, persisted idle
pause/Off, verified backup, check/restart, receipt, neutral postflight and
restoration of its original settings. Packages are not interchangeable.

## installation_3 follow-up: rejected start with no device command

installation_3's ce6067a deployment has verified 110-file hashes, HA check/restart,
614.559 seconds of sampled neutral postflight and restoration of its original
settings. This is technical acceptance, not acceptance of a natural PV cycle.

Its retained 06:00:17 UTC attempt was rejected with `direction_unavailable`.
The persisted record has no sent command, prewrite snapshot, expected readback,
lease identity or restore command. Rollback is `not_required`, yet the PV retry
gate required confirmed rollback. Fresh eligible plans therefore remained
blocked by `neutral_evidence_incomplete` until the original 10:00 UTC deadline.
The exact physical predicate behind the initial rejection is not established;
the no-surplus regression reproduces the same controller/codec transition.

The narrow correction recognizes a complete persisted never-issued shape only
for this rejection, with a coherent original Self-Use snapshot matching the
fresh full EMS block. It still requires all current physical, BMS, topology and
ownership gates. One retry, 60-second spacing, original deadline equality and
the persisted retry budget are unchanged. Missing timestamps alone, ambiguous
transport outcomes, incomplete restore, changed neutral settings and a used
retry cannot use this exception. Diagnostic `neutral_basis` distinguishes
confirmed restore from a proven start rejected before command dispatch.

`test_pv_hold_precommand_recovery.py` reproduces the failure through the real
controller, persists/reloads its codec, confirms Mode 5 across replans and
restores at the unchanged deadline. It also exercises current vetoes and
ambiguous previous attempts. RED and GREEN logs and subsequent exact-SHA
validation are retained in the external report; live acceptance remains pending.
