# Tariff continuity and additional sale protection — offline proposal, 2026-10-09

This source copy combines the preceding RCE/Pstryk sale-consolidation proposal
with the changes below. Base Git SHA: `164508b353806a2cd66c56a41567bba5dc2c203f`.
There is no new commit, live deployment, firmware change or publication.
Exact source/runtime/package hashes and every validation attempt are held in
the external `2026-10-09_TARIFF_RCE_PSTRYK_SOC_OFFLINE` report.

## Tariff continuity

Two controlled reproductions exposed avoidable plan fragmentation:

1. The support-layout comparator rejected a strictly cheaper trajectory because
   it compared the absolute cost difference with the equality tolerance.
2. A short necessary battery top-up prevented continuing direct household
   supply in the same Mode 4 transaction after that top-up's slot boundary.

The optimizer can preserve a verified active Mode 4 run only while its complete
remaining trajectory has no higher modeled grid/objective cost, unchanged total
grid import and ending battery energy, and no worse shortage, terminal target,
uncovered import or physical dispatch availability. Charging allocations remain
unchanged. The action-specific charging target ends at its own boundary; the
whole authorized supply run can continue until the original hard deadline.

This requires fresh physical commitment, valid aware timestamps, fresh control
inputs, real remaining household need and an entirely cheap remaining window.
It cannot create a start or bypass SOC, BMS, permission, FC03 or lease gates.
The change does not extend stale-input limits, retry budgets or deadlines.

The night-like optimizer cases are controlled reproductions, not a reconstruction
of unavailable historical inputs. The continuous-cycle test uses the production
optimizer, arbiter, controller and lease client with modeled physical feedback
and transport. It covers 05:00–06:00, a retarget, completed replans, fresh FC03,
correlated accepted lease events, natural restore, +2 minutes and 610 seconds of
postflight. It does not replace real Home Assistant/ESP acceptance.

## Missing-input evidence

For a tariff stop whose planner reason is `live_data_missing`, capture the
consumed LOAD/PV/battery values, source reasons and ages, the existing age limit,
input sample time and shared input revision. The existing bounded STOP record
owns this evidence. These fields cannot authorize control. There is no new
collector, periodic polling or Recorder frequency change.

The exact cause of the historical 02:44 missing-input cohort remains unknown.
This update makes a future occurrence diagnosable; it does not retrospectively
prove an ESP fault or justify accepting old telemetry.

## Additional protection from dynamic sale

- Existing helper `input_number.hoymiles_rce_soc_safety_margin`: range 0–90,
  step 1; default 5 preserved. Existing saved settings are not reset.
- UI name: **Dodatkowa ochrona przed sprzedażą**. It applies to both RCE and
  Pstryk while automatic home-energy protection is enabled.
- Example: reserve 20% + extra protection 60% = 80% of capacity protected;
  at most 20% of capacity is eligible for sale. Actual sale can be smaller due
  to charge state, home demand and the selected plan.
- Sums above 100% are explicitly capped at 100%, leaving 0% for sale.
- Missing/stale reserve evidence produces an unavailable explanation.
- When automatic protection is off, the UI explains that the manual threshold
  applies; it does not display an inapplicable additive formula.
- This is a sale buffer, not an obligatory charge target or a ban on household
  self-consumption. The pre-existing RCE/Pstryk economic formulas are unchanged.

Canonical PL/EN sources and generated resources use frontend revision 121.
The actual policy-card DOM is tested locally at desktop and mobile widths with
mocked services, including the submitted 90 value and acknowledged-state update.

## Test maintenance and limits

The old `test_buy_stays_hourly` assertion contradicted the frozen contiguous BUY
projection, as already reproduced on the predecessor and base. The test now
checks the existing full run, interruptions by other actions/missing slots and
the original deadline. This is a test correction, not a BUY runtime change.
Original failed logs remain in the predecessor report.

Offline changed-scope validation is distinct from the full Git release gate,
host installation and natural field acceptance. The historical complete-night
result and an exact-candidate field run remain unclaimed. Host changes need
separate authorization and the established identity/backup/deployment procedure.
