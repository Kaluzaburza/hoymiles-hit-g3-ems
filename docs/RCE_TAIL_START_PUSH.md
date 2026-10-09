# RCE executable tails and START-only pushes

Base: 670bade8e1522ebfbcda4a402763517bcaf3540e. Scope approved 2026-09-28:
local fixes, exact package evidence and deployment to the same three HA hosts.
M01 observation automations remain paused; no field acceptance is claimed.

## Planning

The planner now checks net grid export after LOAD/PV and integer register 4306.
New selections require the existing 200 W minimum plus one command quantum
(100 W on the 10 kW installation). Fixed-plan re-attestation remains reduction
only and uses the existing 200 W continuation threshold. Fresh START also
checks the higher margin so reduced commitments do not authorize a tiny restart.

An unusable tail is first moved into an already selected adjacent slot, only
when fresh conservative physical simulation accepts it and whole-horizon
economics do not worsen. If that slot is saturated, an equal/better-price
neighbour may donate enough energy to make both selected slots executable,
again without increasing total energy or worsening the objective. Otherwise
the residue remains for self-consumption. The optional self-consumption filter
also rejects subminimum results. No BMS/GCF/reserve/freshness veto is weakened.

This change does not introduce subslot execution deadlines or extend active
leases. The original immutable deadline and bounded 90 s replan hold remain.
Those protocol changes require separate design and validation. The field
sequence 62/211/116/294 W cannot start new runs under the 300 W entry margin;
this is not a proof against every possible LOAD fluctuation or STOP cause.

## Notifications

Tariff and RCE push only START, with actual start and planned end displayed as
a local time range. Their combined budget is two provider attempts per rolling
60 minutes for the EMS notification manager, retained separately from recent
events in the durable ledger. Uncertain delivery consumes budget; excess START
events are suppressed, not delayed. Restart/clock rollback do not reset quota.
Legacy or invalid ledgers conservatively reserve the two attempts for one hour
on migration. A fresh ledger starts empty. Existing send enable/target settings
are preserved. END/interruption/error events remain internal diagnostic data.
The scheduler alarm script no longer sends error/STOP push. RCM and balancing
notifications are outside this tariff/RCE change. No real notification test.

## Evidence

Live `rce_export_evidence` checks fresh coherent GRID/BAT/PV/LOAD and positive
net export separately from command readback. It grants no control authority,
does not trigger STOP, and is not per-device or duration/transaction acceptance.
Recorder keeps compact status/reason, avoiding new live power/timestamp churn.
`recent_stop_decisions` freezes up to eight first STOP/FAULT frames, including
plan revision/current/pending, blocker, SOC/floor, BMS/GCF, lease identity,
hold/retarget checks and physical flow. The bounded list is recorded as an HA
attribute and resets in memory on HA restart; it is not restored authority.

Prior causes missing from the original ledger remain unknown. In particular,
this release does not retroactively prove every authorization_lost cause or
turn the interrupted M01 transactions into PASS.

Test and deployment evidence:
`<PRIVATE_EVIDENCE>/2026-09-28_RCE_TAIL_PUSH_FIX/`.
