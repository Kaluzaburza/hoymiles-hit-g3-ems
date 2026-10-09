# Qualified household history survives a missing latest day

Base: `95b76424418bd5fa73fa1e2496c5372df6a121b1`, deployed on all three
installations with 110/110 source hashes. Evidence for that deployment remains
in `2026-10-04_SOC_SCOPE_READINESS_FIX` outside Git.

## Confirmed failure

Localhost retained nine qualified days (24 September–2 October) in its
identity-bound cache. Its accepted observation timestamp was 3 October 00:03:47
Europe/Warsaw. The 30-hour observation-age gate discarded all usable history
when the next day was rejected. A successful reread correctly did not renew
that timestamp, but the resulting zero history coverage unnecessarily removed
valid statistical input from RCE, tariff and RCEm planning.

Bounded Recorder reads found 66 shared availability episodes. The midnight gap
was 3 October 23:46:29–4 October 00:41:18 (about 54m50s). Daily phase counters
reset inside the gap, so rejection of that incomplete day remains correct.
Native ESPHome uptime and signal entities were unavailable in the same gaps;
uptime continued monotonically across the long gap, excluding an ESP restart
as its explanation. This establishes HA–ESPHome availability loss, not its
underlying network/client cause. Signal values before/after the long gap were
both -71 dBm. Historical core log files were absent; no definitive transport
root cause is claimed.

## Minimal correction

- Rebuild statistical totals and profiles from qualified completed dates in
  the existing 28-day window; do not mutate the persisted cache or diagnostics.
- At least three unique qualified dates, an aware non-future original
  observation timestamp, and the bounded window permit retaining history
  after 30 hours. Invalid dates, expired windows and missing provenance fail.
- Provider, shared broker, tariff and RCEm apply the same history usability
  contract. The broker is evaluated in the installation calendar/time zone.
- Keep the observation timestamp and separate its freshness from usability.
  Expose retained-history diagnostics instead of pretending a new day arrived.
- Keep the configured daily fallback when usable history is absent. Actual
  telemetry, BMS, broker capture freshness, FC03, ownership, lease, retry and
  deadlines retain their existing requirements.

Regression evidence includes the nine-day localhost case, window expiration,
malformed/future/missing dates and timestamps, immutable cache projection,
and the actual tariff/RCEm gate expressions with fresh/stale broker snapshots.
Existing qualification tests still reject the incomplete midnight interval.

Deployment authorized to all three hosts with one tested SHA, fresh 110-file
baseline, identity/settings checks, idle ownership, durable Off/pause, scoped
backup, HA check/restart and receipts. Evidence:
`2026-10-04_LOAD_HISTORY_CONTINUITY` outside Git. Natural PV observation remains
PAUSED until the user requests it. Technical deployment does not establish
natural PV acceptance. Public RC2 release remains HOLD.
