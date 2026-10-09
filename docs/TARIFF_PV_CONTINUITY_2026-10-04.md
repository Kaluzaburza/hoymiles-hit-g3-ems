# Tariff retarget and PV initial dispatch — Installation 3 candidate

Base `46e180bf8cbeac0957bb280023161ee69bcd85fa`; branch
`fix/tariff-pv-dispatch-20261004`. Frontend stays `1.5.8rc2.110` because
these fixes change backend execution/model code, not frontend assets.

## Confirmed failures and minimal corrections

| ID | Evidence on the base | Correction |
| --- | --- | --- |
| TARIFF-RETARGET-UNSENT-01 | The 07:00 BUY on Installation 3 returned to Self-Use when the lease challenge overlapped a publication. The generic tariff adapter replay reproduces it without a Pstryk planner. | The existing fresh predecessor attestation also handles TARIFF after an explicitly unsent successor. No replacement command, lease extension or deadline extension is granted by this recovery. |
| PSTRYK-REQUIRED-SLOT-01 | At 07:27:58, SOC 19%, reserve 20%, the short current slot advertised support-only while the next slot held the top-up. | Mandatory reserve simulation preserves fractional intermediate energy on the way to its already rounded integer stop target. Economic BUY quantization, physical power/energy caps and fixed-revalidation ceilings stay intact. The hourly run must still reach the protected target to be ready. |
| PV-HOLD-UNSENT-START-01 | Production-adapter replay: new full FC03 during challenge, zero arms, yet `command_outcome_unknown`. | Include the single-block PV hold action in existing explicit-not-queued handling and one fresh-snapshot reprepare. Unknown transport outcomes remain faults. |

The initial retry preserves transaction identity, start time, restore baseline
and hard deadline, consumes a strictly newer complete FC03 cohort, and stays
inside the original dispatch timeout. No reset of PV episode/retry state,
lease TTL (120 s), renewal cadence (20 s), or qualification (180 s).

Tariff predecessor recovery still requires current permissions, BMS capacity,
physical readback and deadline. An invalid or incomplete attestation stops.
The common tariff path includes Pstryk and fixed tariffs; no field failure
on Installations 1/2 is inferred from the code replay.

## Validation and handoff

Regressions use the production HA lease adapter with fake transport, plus
Pstryk energy-balance cases around half-hour/hour boundaries. They cover a
valid predecessor, permission/BMS/readback/deadline rejection, one/two FC03
races, changed baseline, unknown transport, partial top-ups and genuinely
unreachable windows. Existing lifecycle/lease/restore/optimizer suites remain
required. Offline results do not establish natural PV acceptance.

Private evidence: `2026-10-04_TARIFF_PV_CONTINUITY_FIX` outside Git. Start
with its `RESULT.json`, `CANDIDATE.json`, `VALIDATION_FINAL.json` and per-host
manifest/receipts for actual tested/deployed SHA and tree. Historical evidence
in `2026-10-04_PSTRYK_BUY_CYCLE` remains FAIL/HOLD; it is not overwritten.

Only Installation 3 is authorized for this deployment. Packages for 1/2 may be
prepared locally for the identical candidate, but deployment remains HOLD
until a complete successful natural PV-delay cycle on that SHA. Recheck their
identity, 110 baseline hashes, settings and idle state before later rollout.
The paused PV monitor is not resumed by this deployment. Public RC2 merge,
push, tag and publication remain withheld.

The final BUY plan withdrawal at the reached target used the existing
`authorization_lost` lifecycle reason. It restored safely; a more specific
terminal description is a separate diagnostic refinement, not an excuse to
relax authorization or to relabel the five earlier physical interruptions.
