# Shared EMS timing candidate, 2026-10-02

Scope: RCE/Pstryk SELL, tariff BUY, delayed PV charging and forced RCEm
pre-discharge use the same versioned HA/ESP control lease. Base: c3648ae.
The user's requested longer timing also explicitly includes PV delay.

## Contract

- Protocol 2 advertises a maximum 120-second lease and renewal every 20 seconds.
  Both sides must support protocol 2; mixed old/new versions deny forced starts.
  A grant can be shorter at a policy boundary. This is not a fixed 120-second
  extension after every request or response.
- ESP renewal nonce validity is 120 seconds; the initial challenge is 60 seconds.
  Original hard deadline, policy end, fixed replan/settling end and nonce issuance
  bound the grant. HA anchors validity to first send, never ACK arrival.
  An uncertain retry retains sequence/nonce/horizon. A shortened policy cannot
  resend the old wider horizon. Duplicate and late ACKs cannot revive expiry.
- A physically confirmed unchanged command may wait for missing reports within
  the existing actual lease, last real proof plus 120 seconds and original policy
  deadline. It cannot start, retarget, renew or fabricate new physical proof from
  stale data. Fresh commands still require the existing complete FC03 contract.
- Explicit STOP/pause, owner conflict, Off-Grid, export prohibition, BMS limit,
  SOC limit, wrong flow, lost PV surplus and withdrawn current authority take
  precedence. RCEm retains its existing live BMS/plan authority checks.
- After matching command readback, forced Mode 4/5 physical settling is bounded
  by 180 seconds from send. RCE/Pstryk and tariff plan holds are 180 seconds;
  split publication of an RCE successor may wait 60 seconds. The LOAD response
  replan is requested at 150 seconds, with the original 180-second end retained.
  Renewals during a ramp require the appropriate fresh, qualified evidence.
- Dispatch, unacknowledged command, restore, polling and electrical/grid
  protection are unchanged. A software expiry can request restore; an unavailable
  RS485 link cannot guarantee physical delivery within that deadline.
- One compact preceding renewal-denial reason is retained in existing live
  diagnostics. No new Store, Recorder entity, high-rate history or Modbus polling.

## Validation and rollout

Evidence is outside Git in
`<PRIVATE_EVIDENCE>/2026-10-02_PV_DELAY_REPAIR/lease-timing`.
Tests cover real controller/sensor paths, delayed/lost responses, unchanged
transactions, bounded replans, tariff initial ramp, PV delay, manual STOP,
BMS/export vetoes, RCEm discharge, recorders and pinned firmware compilation.

Two pre-existing tests also fail on unchanged c3648ae: the historical exact
helper allowlist and an old RCE sensor probe missing the newer cohort method.
Their failures must remain visible; they are not new timing regressions.
The historical release freeze validator is a separate gate, not waived here.

Deployment requires a clean exact commit, scoped backup, matching HA and compiled
ESP firmware, idle/paused preflight, newer complete FC03 after restart/OTA, then
at least 120 seconds of healthy postflight. Order: installation_1 (zero export stays
0%), installation_3 (preserve Maximum profile), installation_2 (paused by the user).
Preserve independent prior SELL FAIL/HOLD. Offline validation and technical
deployment are not natural SELL/BUY/PV field acceptance or public release.

## installation_3 follow-up before remote rollout

At 09:43-09:47 Europe/Warsaw on 2 October, installation_3 advertised a current,
qualified PV-delay window but had no active transaction and stayed in Self-Use.
Its inactive legacy RCE SOC latch was 0%, while current SOC was 27%. The hold
candidate incorrectly consumed that old helper instead of selecting SOC + 1%.
Offline RED reproduced both a refused start with a low old floor and an
incorrect target with a higher old floor. A new hold now derives its own
target; an active hold still requires and preserves its confirmed target.
The regression also exercises the executor lifecycle with the real 0% default.
This is a start defect, independent of the new lease timings. The chart's
planned trajectory is not evidence of a physical transaction.

## Initial PV acknowledgement before the power cohort

The e0ea028 field attempt on installation_3 at 10:26 CEST received matching FC03
after about 3 seconds, while the independent BMS power timestamp still
preceded dispatch. It rolled back before the 180-second settling interval.
The retry also treated an incomplete flow cohort as unavailable BMS and
faulted. The firmware's existing 120-second lease restored Self-Use;
this attempt remains FAIL/HOLD in the external incident evidence.

A sent PV hold with newer matching FC03 may now await the independent
power cohort within the accepted lease and original command/policy deadline.
It retains the transaction and records only the real FC03 acknowledgement.
It cannot renew the lease or publish physical execution from incomplete
telemetry. Fresh coherent measurements resume the existing settling path;
two distinct successful cohorts are still needed to confirm execution.
STOP, pause, revoked consent, export restrictions, BMS power limits, SOC,
observed import/discharge and lost PV surplus remain vetoes. Invalid or
future timestamps cannot provide the waiting exception.

The regression reproduces failure on e0ea028 and covers subsequent recovery,
unchanged deadlines, expiry without renewal, replans and the explicit vetoes.
No extra Recorder history, persistent state or Modbus polling is introduced.
The same candidate changes PV forecast profile weights to 70/60/50 and
frontend revision 106, preserving the selected profile and Conservative default.
