# PV hold: startup observations and retry control

## Requested start window through 14:00

The owner extended the latest new PV-delay start from noon to **before 14:00
Europe/Warsaw**. Exactly 14:00 does not start a new window. RCE and Pstryk
use the same constraint. A previously accepted window may continue across
14:00 only with its existing commitment and original hard deadline.

The former independent end-before-14:00 restriction is removed so a late
start has a usable duration. The conservative forecast must still restore
the original energy/PV-origin stock before another fixed BUY/SELL action,
by 16:30 local and at least one hour before forecast surplus ends. Missing
refill capability, preservation of the stock present at the start, consent
or export permission still rejects it. The battery-sale reserve is not a
minimum entry SOC for PV deferral; see [the reserve correction](PV_DELAY_RESERVE_2026-10-04.md).
This changes the planning cutoff, not the lease, startup budget or retry guard.
Frontend 108 describes the same limits in Polish and English.

Installation 3's noon observation on 5c5cdd3 created a logical attempt but
sent no command: the morning-only planner removed its window, yielding
`authorization_lost`, Self-Use and no lease. This is a PARTIAL field test,
separate from the earlier failed 612a3e3 physical canary. The requested
extended-window candidate still requires its own full natural acceptance.

Boundary/refill tests: `tools/test_pv_delay_start_cutoff.py`;
cross-cutoff commitment tests: `tools/test_pv_delay_active_window.py` and
`tools/test_pv_charge_delay_horizon.py`.
The extension passed 26 offline test scripts, including real Pstryk publications,
86 RCE optimizer scenarios and its unchanged performance limits. Generated
frontend resources match their canonical sources. Natural field acceptance
remains PENDING for this candidate.

## Startup and retry repair

Status: implemented; 31 relevant offline test scripts passed (including
682 Supervisor sensor checks). Live installation 3 acceptance is PENDING.
Offline validation and installation 3 canary are separate gates. No public release. Installations 1 and 2 wait for the natural completion
and restore of installation 3, per the owner's explicit instruction.

Recorded evidence on the prior runtime: 74 transaction IDs in a bounded
20-minute slice; repeated restore and immediate restart. One complete plant
cohort reported battery -3299 W while independent BMS register 1914 reported
-5 W. `no_net_export` is observation-only; it does not itself command a stop.
The owner reports short import/discharge transients during inverter startup.
Their duration/amplitude is not a calibrated sensor specification.

The existing 180-second maximum starts at the original command timestamp.
During WAITING_READBACK, unavailable, inconsistent or transient flow reports
may remain PENDING. The same command must have fresh matching full FC03,
validated topology, live SOC/BMS limits, consent, export permission and a
current plan (or its separately bounded pending publication). Mode 5, the
SOC ceiling and 1% discharge limit are unchanged. A live lease is needed to
wait; renewal requires fresh critical control evidence and cannot exceed the
initial budget or the original hard deadline. Flow absence alone is never
renewal authority. No restart resumes a startup hold.

Two independent matching flow cohorts, at least 15 seconds apart, are still
required for EXECUTING. A bad intermediate cohort resets qualification.
Once qualified, strict physical evaluation applies immediately. Otherwise the
180-second deadline restores Self-Use; this budget is not a mandatory delay
before accepting already stable physical evidence.

One retry is allowed after at least 60 seconds, confirmed restore and a newer
neutral FC03 with fully ready start inputs. Its transaction ID preserves the
consumed attempt through restart. Plan revision/deadline churn does not clear
the budget. The next independent episode starts only after the prior deadline
or an intervening completed non-PV policy; this is not a global daily counter.

Entry points for agents:

- `supervisor_active_controller.py`: `_pv_hold_startup_controls_ready`,
  `_pv_initial_input_wait`, `pv_hold_settling_lease_authorized`,
  `_pv_hold_retry_start_allowed`.
- `supervisor_sensor.py`: actual lease renewal gate, distinct from proof of flow.
- `supervisor_active_bridge.py`: strict physical predicate and BMS delta evidence.
- `tools/test_pv_hold_startup_loop.py`: recorded divergence, transient/missing
  data, actual renewal gate/firmware model, timeout, restore and restart guard.
- Private evidence: report directory `2026-10-03_PV_HOLD_LOOP`; no addresses,
  identifiers or raw installation data belong in this public tree.

## Canary follow-up

The first installation 3 canary (612a3e3) confirmed Mode 5 and physical PV
export with near-zero battery power, but restored after about 57 seconds with
`direction_unavailable`. The complete stop frame showed valid BMS, current PV
plan and no owner conflict, while the next system-flow cohort was incomplete.
The bounded telemetry fallback incorrectly also required the independent
battery-sale slot to allow continuation. An inactive SELL slot is expected
during PV hold and must not invalidate its separately qualified current plan.

The regression reproduces that exact combination and now preserves only the
existing lease and last real physical proof through the report gap. A fresh
complete cohort is required before renewal resumes. Existing STOP, BMS,
actual opposite flow, expired proof/lease and PV-plan revocation tests remain.
The recorded negative canary remains FAIL/HOLD; no field PASS is claimed.
The one-retry guard is not cleared by this repair or a Home Assistant restart.
# Historical scope: the one-retry/60-second/window-equality admission described
# below is superseded by [PV Delay retry policy, 9 October](PV_DELAY_RETRY_2026-10-09.md).
