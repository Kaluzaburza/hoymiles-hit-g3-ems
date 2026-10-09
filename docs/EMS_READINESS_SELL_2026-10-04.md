# EMS readiness, retained chart and Pstryk publication

User scope: repair confirmed faults and deploy one tested candidate to all three
installations. This authorization supersedes the earlier Installation 3 canary
gate for deployment, but does not accept natural PV execution or authorize a
public RC2 merge/release. Scheduled PV observation stays PAUSED until requested.

## Evidence and minimal corrections

- Localhost: canonical `partial/rcm_timeline_unavailable` with RCEm explicitly
  disabled. The existing explicit-disabled dependency correction from `ee999e8`
  is already in this candidate; align the older installation rather than weaken
  readiness globally. Unknown, enabled or active RCEm still requires its inputs.
- installation_3 chart: the backend preserves complete slots during source staleness,
  but Aurora accepted them only for `pending/source_recalculation_pending`.
  Accept validated, explicitly non-current retained slots also for source
  `partial/*_timeline_stale|unavailable`. Keep the original 15-minute display
  expiry, never extend `built_at`, and label these data as not current. Structural
  failures and physical readiness/conflict checks remain closed.
- Pstryk invalidation: both plan projections could become non-current while
  their timelines stayed current because ordinary pending publication rejects
  an equal completed input revision. Use the existing explicit unavailable
  publication path, which withdraws authority and retains bounded old geometry.
  Do not change the general late-callback/revision guards.
- Pstryk age: after a slow solve and successful revalidation against fresh
  physical inputs, market age still began at the pre-solve capture. Age the
  accepted fresh input cohort instead. Cache reuse does not refresh age;
  the same 120-second limit, final revalidation, settings/revision fences,
  original transaction deadlines, retry budgets and physical controls remain.
- installation_3 SELL: at 13:39 local, a fresh read contains today's 19:00–19:30 sale
  of about 1.17 kWh and tomorrow's sale of about 3.57 kWh. A fresh EMS page
  displays today's next action and both intervals. This does not prove what
  was displayed earlier or actual future execution. No economic constraints
  are loosened to manufacture a sale.

Frontend cache revision: 111. Firmware and lease protocol remain unchanged.
Runtime regression tests exercise real timeline publishers at an unchanged
revision and a 45-second solver delay followed by fresh revalidation, including
the unchanged 120/121-second expiry boundary. UI regression replays
current→pending→partial→current with unchanged geometry, truthful status,
no writes, expiry and structural rejection.

Evidence, test logs, exact SHA/tree, installation manifests, backup receipts and
postflight belong outside Git in `2026-10-04_EMS_READINESS_SELL_FIX` under the
user's `PRIVATE_EVIDENCE`. Deployment and field results must be read from that
report, not inferred from this source document. Earlier FAIL/PARTIAL evidence,
RCEM-HISTORY-CADENCE-01 and FREEZE-LEGACY remain open; release HOLD.
