# PV Delay: fixed retry cooldown, 9 October 2026

The user replaced the previous one-retry/window-equality policy with a maximum
three-minute retry cooldown for PV Delay only. This does not change tariff or
RCE battery-sale retry budgets, physical gates, prices or optimizer inputs.

On installation_3, the old e9f5898 code stopped a natural PV hold at 06:24:48 UTC with
`authorization_lost`, then confirmed neutral state. At 06:30 the plan was again
qualified, but its end was 07:30 UTC while the completed attempt's end was
08:00 UTC. `original_window_boundary_changed` prevented a new start. At
07:00:02 UTC the plan again ended at 08:00 UTC and the existing retry1 started
naturally. These are observations of the old version, not field acceptance of
this change. The exact input behind the first loss of qualification requires
separate historical evidence; a stop reason alone does not identify it.

## Resulting execution contract

- An ended PV attempt retains a fixed 180-second cooldown derived from durable
  command, restore and physical-evidence timestamps. Replans and HA restarts
  neither advance nor restart this timer.
- At expiry, the existing single adapter callback requests one fresh frame.
  It adds no device polling and grants no command authority by itself.
- A new attempt requires the full current qualified plan, complete fresh
  Self-Use FC03, BMS/topology readiness, consents and no competing owner. The
  prior attempt must be idle and neutral, with confirmed restore or complete
  evidence that no command was ever prepared or sent. Ambiguous transport,
  pending restore, Master Stop and Off-Grid do not qualify.
- There is no permanent retry-count ceiling for PV Delay. Each accepted new
  attempt has a new transaction identity and the current qualified plan's
  deadline. A changed end of an already completed plan is not a veto. Previous
  receipts remain unchanged; an active transaction's hard deadline is never
  extended, reset or replaced by this retry logic.
- An expired timer does not force a start. Missing neutral proof or qualification
  can still block execution for longer than three minutes.
- The separate initial 180-second PV settling period remains measured from
  that attempt's first command. Power flows remain diagnostic for PV Delay;
  no such relaxation is applied to battery sale or tariff execution.

## Evidence

`tools/test_pv_hold_retry_cooldown.py` reproduces the old moving-window and
consumed-retry locks, verifies an immutable cooldown and its single wakeup,
then checks fresh requalification across persisted recovery. Existing
pre-command and startup-loop regressions retain negative safety cases and
exercise repeated timeout/restore cycles without a rapid retry loop.

RED/GREEN logs, exact successor SHA validation and any later deployment
receipts belong to the external `2026-10-09_PV_DELAY_RETRY_DEPLOYMENT` report.
Offline success is not a natural-cycle acceptance claim. The earlier 6 October
and 3 October retry descriptions are historical and superseded by this policy.
