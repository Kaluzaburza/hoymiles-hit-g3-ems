# RCE continuity repair — 2026-09-27

Base: `3e1f020181291673055ca7518c9b2dc7cad074c0`.
Scope: the natural morning failure on installation_2; same tested candidate
is intended for installation_2, installation_1 and installation_3. No firmware change.

## Field evidence

Private raw archive: `rce-observation-20260927-3e1f020.zip`, SHA256
`d1d09eb77c7d33cd30634d2eded053f356d4c6898bf215d7334cdf924456421e`.
14 files verified against the archive manifest. UTC event time is authoritative;
collector receipt time can lag and must not replace it.

- 04:29:22.330Z: current revision 235 removed the selected current half-hour.
  Export 1.312 -> 0 kWh; 0.63 minutes remained. STOP followed at .409Z.
  GRID fell from 28,395 W to 144 W; FC03 Mode 5 -> 0. This was a physical
  interruption. BMS remained above the requested power.
- 04:35:18.183Z, 04:39:19.454Z, 04:43:18.284Z: pending publication interrupted
  confirmed transactions. The raw run end was 07:00 CEST, while the immutable
  transaction deadline remained 07:30 CEST. The exact-end hold predicate rejected
  this shorter, still future run. This mechanism is reproduced offline.
- 06:43:24.165 CEST (04:43:24.165Z), HA log:
  `retarget_not_authorized`, before transport. The transaction then became
  `command_not_queued`. A planner update during the awaited lease challenge
  reproduces this outcome. The old log did not record which individual local
  authorization predicate failed; exact predicate attribution remains limited.

The initial planner decision is confirmed by history. Reconstruction uses recorded
prices and persisted LOAD profiles but rounded input diagnostics and incomplete
historical Solcast attributes; it is not an exact historical solver replay.

## Authorized behavior

The user explicitly chose to retain an already started block when fresh limits,
reserve and prices permit selling. The user also requested modest tolerance of
transient safety/readiness checks. Physical BMS/GCF, home reserve, pause and
Master STOP remain hard limits; transient publication is allowed to settle.

## Changes and regression evidence

1. Shorter run + pending: keep only the confirmed existing command, bounded by
   both the raw planned end and immutable deadline. No write or lease authority
   is granted by this hold. Tests cover expiry, BMS=0, permission and Master STOP.
   Fresh shorter runs also adopt safer/current targets without changing the
   transaction deadline and schedule STOP at the earlier planned end, including
   when no further planner event arrives. These two boundary cases failed before
   the repair. A shortened stop remains an interruption in notification semantics.
2. Initial challenge race: wait up to 5 seconds for trailing HA publication,
   inside the original dispatch budget, then recheck exact fresh authority.
   No unknown outcome or active lease is retried. Tests include denial during
   BMS=0, permission loss, pause, Master STOP and exhausted wait.
   If a complete newer FC03 arrives during the wait, the precise local
   `stale_snapshot_generation` rejection invokes the existing single bounded
   reprepare. Its persistence/authorization gates and original deadline remain
   mandatory. The same generation race failed before this classification.
3. Active-block planner policy: same-entry Supervisor proof binds a currently
   leased and physically confirmed RCE export to the current half-hour. When
   a fresh selection drops that slot, bounded fixed-schedule checks may retain
   it at no more than the accepted/sent power. Only future planned exports may
   shrink. Fresh complete home/night/PV/BMS/GCF physics and comparison with
   self-use remain mandatory. Changed market/economics, an expired block,
   missing/stale proof or unsafe conditions do not receive this retention.

The original optimizer search is unchanged. The retained plan explicitly exposes
`active_slot_commitment_applied` and is not claimed to be a globally optimal plan.
Tests exercise pure policy and the real publication/helpers/controller/lease path.
Offline virtual-clock tests do not certify a live inverter.

## Gates and deployment

### Daylight issue discovered during postflight

The first candidate `6ef5c44e189a350c5b8c5e3a0685429bd1fd9cdf` passed
44 local gates, was deployed with identical 87-file hashes and restarted on
all three hosts. Postflight on installation_2 and the third host showed a remaining
`plan_revalidation_failed` / `home_energy_shortage` publication loop. Acceptance
was held and the failure investigated before closing this task.

The real input adapter, using saved installation_2 values with explicitly synthetic
freshness and fixed sun times, reproduced different immutable PV maps after
only 50 ms without any state report. Expected PV changed 87.1756262149217 to
87.1744789901447 kWh; both snapshots passed freshness gates. This is a mechanism
reproduction, not an exact historical runtime replay. A self-contained synthetic
test also fails on the previous candidate and exercises the full async adapter
and publication path.

Adaptive forecast correction now compares cumulative actual PV and cumulative
forecast at the same fresh PV observation time. Current authorization and input
ages still use wall time. PV reports older than 300 seconds, invalid values,
missing/naive/future timestamps or a different local day cannot drive adaptation.
The strict immutable-input revalidator is unchanged; new PV data still invalidates
the old result. A current negative result grants zero execution authority.

`test_rce_daylight_publication.py` covers the clock drift, actual input parsing,
async publication, new PV report rejection, invalid BMS, and invalid PV samples.

Execution evidence is maintained in the local dated report directory
`PRIVATE_EVIDENCE/2026-09-27_installation_2_rce_observation` (outside this source tree).
Exact tested SHA, manifest, logs, installer checks, per-host backup and rollback
must be sealed before rollout. Historical Task02 release validation remains
unchanged and separate from the strict current-candidate deployment manifest.
The user paused EMS; deployment must preserve that pause. Natural field
acceptance remains PENDING after deployment until a new authorized observation.

## Solcast automatic adaptation (user-requested follow-up)

Both installation_2 and installation_3 were verified as Solcast v4.6.1 with
`auto_dampen: true`, `key_estimate: estimate`. Their identical solcastapi.py
SHA256 is `1a558a82f46354a4b338ec571916f27636b4b0bc0b7c3aa87d216a7903640492`.
Lines 905-906 publish `dampening_factor` on aggregate detailedForecast rows
only for the automatically dampened output. EMS already read the corrected
pv_estimate/10/90, then added another historical/live calibration. The local
causal fixture reproduced an extra factor of 0.3061 on already adapted data.

RCE and tariff now give that explicitly marked source one calibration owner:
Solcast. Each day's source is resolved independently; the extra EMS factor is
1.0 and its duplicate Recorder calibration scan is skipped for adapted today.
Unmarked or ambiguous sources retain EMS learning. Zero-export/unverified GCF,
freshness, coverage, P10 uncertainty reserve, BMS and execution guards remain.
No Solcast options or learning data are modified. This proves correct use of
upstream adaptation; improved real forecast accuracy requires future measurements.

`test_solcast_adaptation_contract.py` exercises both real input adapters,
corrected half-hour energy, new forecast updates, dormant local factors, mixed
sources, on/off, malformed markers, GCF precedence and Recorder query ownership.
