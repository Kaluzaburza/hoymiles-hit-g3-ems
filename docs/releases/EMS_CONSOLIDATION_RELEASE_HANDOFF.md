# EMS v1.5.8 consolidation release handoff

Status: **release/v1.5.8 prepared locally; M01/CI/publication HOLD**

Prepared: 2026-09-09

## Latest Task 02 alignment boundary — 2026-09-15

The clean product runtime candidate is
`119b0b0476457065f08b3dd641869ee757dac70a`, tree
`acd2c6f520cd651cb57859abaf4fa3c698678186`, on
`fix/task02-m01-alignment-01`. The same six HA runtime files are deployed on
installation_1 and the installation_2. The meter deployment has an external
backup and exact package/hash record; config check, HA restart and post-restart
hash verification passed. installation_1 matches after line-ending normalisation.

No RCE retest belongs to this completion step. RCE remained disabled and the
meter EMS remained paused in Self-Use with no active or next action. installation_1
remained EMS-off in Self-Use with zero export. ESP sources on both installations
remain on the common `d20cbd0` line and have no delta requiring OTA.

Current gates: `SOC-01 PASS`; `PV-NOC-01 PASS` including
`NIGHT_TRANSITION_LIVE`; `DIAG-01 DEFERRED BY OPERATOR`;
`RCE_RETEST_119B NOT_RUN_BY_OPERATOR_SCOPE`; `M01 PARTIAL/FIELD_PENDING`;
CI/publication HOLD. The previous `M01 PARTIAL/STOP` remains historical evidence
and is not converted to PASS. The exact deployment report and manifest are in
`<PRIVATE_EVIDENCE>/2026-09-15_m01_alignment_119b0b0/`.
The dirty user-owned `release/v1.5.8` worktree remains pinned at `bce6f27` and
was not overwritten or moved.

## Previous Task 02 field boundary — 2026-09-14

`release/v1.5.8` remains pinned at `bce6f27`. The later fix line deployed
`d20cbd0` HA/ESP and the HA-only RCE lease correction `fa73ecd` to the meter
installation. Parallel lease renewal passed in a real RCE transaction, but two
power-retarget overshoots (about 62.79 kW and 89.65 kW for a 32 kW target)
required a controlled STOP. The evidence-only head `c894633` on
`fix/taryfa-stabilnosc-02` is not an accepted or promoted release candidate.

Current gates: `SOC-01 OFFLINE/installation_1 PASS`; `PV-NOC-01 OFFLINE PASS /
installation_1 PARTIAL / NIGHT_TRANSITION_LIVE PENDING`; `DIAG-01 DEFERRED BY
OPERATOR`; `RCE_LEASE_FIELD_RETEST PASS`; `RCE_POWER_RETARGET_STABILITY FAIL`;
`M01 PARTIAL/STOP`; CI/publication HOLD. A new product candidate requires a
minimal retarget correction, explicit delta/manifest, offline release gates and
installation_1 acceptance before another meter window.

Consolidation branch: `recovery/ems-consolidated-2026-09-09`

## Provenance and release identity

- Repository: `https://github.com/Kaluzaburza/hoymiles-hit-g3-ems.git`.
- Publication branch: `main`.
- Last confirmed public GitHub Release: `v1.5.7`, commit
  `6617bc4de6592439ea2c64889b0a25bbe5bfa45e`.
- Accepted consolidated baseline: commit
  `cfe6c2b8dab5a41ed50d7a347d0b7fd1890f3b87`, tree
  `ac4b6d9ebe624401c2b8a73eb38df98efc3d484e`, with single parent
  `1d5e6f592433dbd0df28f2ad33d00956037da1f1`.
- Candidate code commit before this documentation-only handoff:
  `2d075a18be1172feced29ca238fcf3508253ef95`, tree
  `5bc7a741f35e47daf78b19bb45d0e8e4064e094f`.
- Planned release and immutable tag: **v1.5.8**. No public `v1.5.8`
  release or tag existed when this handoff was prepared. Recheck immediately
  before publication.

The accepted baseline records the complete reviewed dirty integration state,
including its previously untracked regression tests. Commits after that point
only prepare release metadata, freeze the accepted candidate in the release
validators, complete two missing English scheduler translations, and add this
handoff. Private source manifests and field receipts are indexed outside the
tracked tree under `_local`; they are evidence, not public release content.

## N12 adoption and release-branch preparation — 2026-09-13

The local `release/v1.5.8` branch was created directly from accepted N12 commit
`c736b69d985d6f2a75015abdacb8839a461ac7f1`, tree
`5c15dae9b4b429a22442de1b2fec953c3f89be2d`, whose parent is reconstructed
runtime `96fc91b7bc6c4b258e654958c2ecf2400438ae74`, tree
`c35805df108df3eb6f0bcb85e6fa209a31153686`. The accepted external
`FREEZE_MANIFEST_V2.json` SHA-256 is
`dd2c700eb85c2d4cbe70cd2ce937ce534104b27b7e75a0476adae160074b1503`.
Historical N12 reports remain unchanged.

The release preparation contract permits one exact, reviewed documentation and
validation delta. `tools/release_manifests/v1_5_8_release_delta.json` pins every
changed path and every changed file except itself and the self-hashed validator.
The validator compares all 114 protected product paths directly with `c736b69…`,
including modes, object types and blob identities. It also requires a clean tree
and rejects masked index flags. The contract does not depend on the branch name.

The N12 mutation fixture now uses reachable `96fc91b…` for the rejected
runtime-without-validation-delta case. Disposable clones use Git transport with
`--no-local --no-hardlinks`; the private, unreferenced `f2fbfb…` object is
required to be absent. This preserves the negative test without relying on
alternates, reflogs or author-local object storage.

The global validator's older REV28 proof now also has an exact, hashed
`rev28_historical_fixture.json` plus a compressed archive of the exact frontend
layers needed by its mutation suite. When the divergent historical commit exists,
the validator cross-checks its tree, parent, subject, branch manifest and
protected blobs against Git. In a release-only transport where that unpublished
commit is correctly absent, it validates the same closed facts from the tracked
fixture/archive and still runs the real frontend suite plus all count/mutation
self-tests; this is not a skip or a branch-name exception.

The release delta changes no HA runtime, scheduler/frontend asset or ESPHome
firmware source. Local G3 remains `CLOSED / PASS WITH ACCEPTED RESIDUAL RISK`;
T02 remains accepted; `RCE_FIELD_PENDING = M01`, remote CI and publication remain
open. A completely dead sole ESP cannot transmit STOP, and an SOC limit is not
a Self-Use command. These are explicit boundaries, not regressions waived by
the branch preparation.

For a later closed M01 documentation delta, record the evidence externally,
review an exact minimal path/hash set, create a new pinned manifest identity and
rerun the release-contract mutations plus affected release gates. Do not permit
all documentation and do not treat runtime changes as evidence-only updates.
Record the resulting commit/tree in the external handoff only after it exists.

## Post-preparation correction queue — SOC-01

The release branch now contains an isolated SOC-01 product correction after the
reviewed preparation commit `a57d405...` and the handoff-text correction commit
`3ecc3e1...`. Offline tests preserve the measured 25% starting SOC with an
unchanged 30% RCEm floor, prohibit discharge below that floor, prohibit
flow-less energy increases in both natural and segmented pre-discharge paths,
and retain the existing invalid/upper-bound checks. The UI distinguishes a
healthy idle `no_eligible_candidate` state from a real execution blocker.

This section records implementation and focused offline validation only. It is
not a installation_1 deployment, physical test, M01 result or publication approval.
The exact SOC-01 path/hash set is pinned in
`tools/release_manifests/v1_5_8_soc01_delta.json`; installation_1 acceptance remains
pending and the shared frontend revision will be advanced once all required
SOC-01 and PV-NOC-01 card changes are closed together. DIAG-01 was later
deferred by the operator and is not a gate for this candidate.

## TARYFA-LEASE-01 correction — 2026-09-14

The isolated product commit `29f0e83` corrects two lease lifecycle races found
while reconstructing the 14 September tariff event. When correlated physical
authorization becomes available near the ESP TTL, an already-due renewal now
replaces the pending unauthorised five-second recheck. A confirmed terminal
record releases only the handle for that completed transaction, and late arm or
renew responses cannot mutate a newer handle. A retarget rejected before the
forced transport is reported as known not-queued rather than an unknown outcome.

The red reproductions failed on the prior `bda41f1` product and pass with
`29f0e83`; the surrounding Supervisor, tariff, RCE, STOP and firmware-readback
test matrix is green offline. The change does not alter the 30-second ESP TTL,
five-second renewal interval, hard deadline, firmware, FC03/BMS gates or planner
current/pending rules. Active installation_1 acceptance and M01 remain separate,
nominated windows. DIAG-01 remains deferred and absent.

## Included behavior

The candidate retains the integrated RCE, tariff, RCEm, Supervisor, balancing,
firmware and Aurora work described in `CHANGELOG.md` and `docs/releases/v1.5.8.md`:

- integer control targets after safety limits while retaining precise physical
  readback and restore snapshots;
- complete 4300–4306 writes, newer FC03 acknowledgement, RCE retarget/restore
  continuity and bounded post-command settling;
- pending-frame revision protection and the fail-closed Supervisor ownership
  and transport lifecycle;
- optional RCE battery-wear-cost helper semantics;
- tariff Mode 4 charging/hold transitions, PV and current BMS limits, and the
  pending RCEm race guard;
- 120-second RCE/tariff planning cadence, separate from the approximately
  three-minute user chart refresh;
- Aurora revision `1.5.8.72`, including import/export energy, action-coloured
  SOC shading, the larger detail panel, full balancing date, stable mobile
  scroll/selection, theme-independent text, status/fault dialog and initial
  EMS defaults;
- firmware transport/polling changes, one-percent setting steps and explicit
  rejection of stale snapshot generations.

No new optimizer policy was designed during consolidation.

## Version map

| Surface | Candidate value | Source of truth |
| --- | --- | --- |
| Integration manifest | `1.5.8` | `custom_components/hoymiles_hit_modbus/manifest.json` |
| Managed EMS package marker | `1.5.8` | integration constants and canonical scheduler generation |
| ESPHome project version | `1.5.8` | `packages/core.yaml` |
| Public ESPHome package ref | planned immutable `v1.5.8` | `hoymiles-inverter.yaml` and `examples/esphome/hoymiles-hit-g3.yaml` |
| Frontend cache revision | `1.5.8.72` | `assets.py` and generated dashboard strategy |
| Changelog | `[1.5.8] - Unreleased` | `CHANGELOG.md` |
| Release notes | candidate, no publication date | `docs/releases/v1.5.8.md` |

## ESP32 decision

An ESP32 rebuild and upload **is required for v1.5.8**. Compared with the last
public `v1.5.7` stack, this candidate changes `packages/modbus_connection.yaml`,
`packages/overview.yaml` and `packages/settings.yaml`; these affect transport,
polling and EMS setting behavior. HACS updates the Home Assistant integration
and does not flash ESP32 firmware.

The local CI fixture passed `esphome config` and `esphome compile` with ESPHome
`2026.7.2` at candidate commit `2d075a18...`. This proves local compilation
only. The user-facing build must be made from the immutable `v1.5.8` tag after
that tag exists and its package download path has been verified.

## Firmware and control evidence reconciliation — 2026-09-12

The working branch remains `recovery/ems-consolidated-2026-09-09`, based on
`1c20cb9d7a9c09ea45050ba294c5f18fac14dae1`, integration/project version `1.5.8`.
The `planning/v1.5.9` worktree adds planning documents only; it does not contain
a newer product implementation. No version bump or publication is part of this task.

| Item | Evidence and exact scope |
| --- | --- |
| First documented 100 ms firmware | installation_4, 2026-09-08 about 15:50 UTC; Builder/ESPHome `2026.8.2`, build `17:46:06+0200`, device `config_hash=b05237cf` and In sync. Transport YAML SHA256 `4ba72748d2c6a3a3f1c55eddbae9b68e233114e550326e2a1034d5c63f8d19cd`. Native logs observed 102–114 ms after reception. This is command spacing, not a guaranteed 100 ms inverter response. |
| Later firmware layers | The accepted set also includes Overview FC04 30001–30020 on the 5 s control poll, native correlated accepted/rejected action responses, whole-percent targets and rejection of stale FC03 snapshot generations. The first 100 ms upload alone does not prove these later layers. |
| RCE discharge continuity | installation_4 field run 2026-09-09 01:17:28–01:20:48 UTC: 50→40→50→49%, SOC target 45%, own FC03 generations 4042/4052/4053/4063, no intermediate Self-Use, continuous owner/transaction and original deadline. STOP restored the full original block. This run did not sweep the SOC target. |
| RCE 60 s settling | Bounded from actual `command_sent_at`, with one replan around +45 s for the recognized transient LOAD/plan loss. It does **not** disable rollback globally and is not a new tariff/RCEm exception. Hard stops, BMS/SOC, ownership, Off-Grid and required fresh physical evidence remain enforced. The field run recovered before +60; the persistent-failure boundary is covered offline. |
| Tariff charge/hold continuity | installation_1 field runs on 2026-09-08 cover SOC/power changes and Mode 4 charge→hold→charge without intermediate Mode 0. Final PV cycle: 75/100→75/30→hold68/30→75/30→75/100, five complete commands with their own FC03, followed by verified STOP/restore. This is one zero-export installation, not Mode 5 or per-Slave acceptance. |
| installation_1 before this update | 2026-09-12 read-only comparison: active Polish scheduler and the RCE/tariff/Supervisor control modules match current source bytes. Eight other Python files differ only in LF/CRLF; the bundled English scheduler differs and was left untouched. All 17 ESP packages match semantically except the old `core.yaml` project label `1.5.6` and this task's transport cleanup. Settings/Overview already match exactly. |
| installation_1 after this update | ESP `installation_1_esp`: Builder `2026.8.2`, build `2026-09-12 10:28:43 +0200`, `config_hash=81a0cc02`, OTA successful and device In sync. Native boot log reports project `1.5.8`. At 08:33:14 UTC postcheck confirmed 17/17 package hashes, unchanged device entry, advancing FC03 12→13, full block `0/25/90/98/60/0/100` and owner none. Previous RCE/tariff/balancing permissions were restored; RCEm remained off. |

Private receipts retained under `_local/reports/PRIVATE_EVIDENCE/`:
`RCE_TAKEOVER_20260908/TRANSPORT_DEPLOYMENT.md`,
`RCE_TAKEOVER_20260908/RCE_SETTLING60_FIELD_ACCEPTANCE_20260909.md`,
`TARIFF_GRID_SUPPORT_20260908/tariff_handoff/FIELD_ACCEPTANCE.md`, and
`EMS_120S_HANDOFF_20260908/SITE_ALIGNMENT_20260909/installation_1_alignment_final_receipt.json`.
These historical receipts are not a fresh inspection of installation_4 or installation_2.

The 12 September source cleanup removes four deprecated `command_throttle`
entries while retaining shared `turnaround_time: 100ms` and `send_wait_time: 250ms`.
installation_1 ESP packages were deployed from this worktree; the existing device
entry, hardware settings and secrets were preserved. No HA restart or manual
inverter command was needed. The rollback directory on installation_1 is
`/config/.codex_backups/esp-align-20260912`; it contains the prior entry and all
17 prior packages. Restore those sources and rebuild/upload the same device
only under the same idle/fresh-readback maintenance conditions if rollback is needed.

Offline validation of this patch passed: firmware/Overview/integer-percent
contracts, RCE retarget and 60 s settling suites, lifecycle-frame revision,
tariff physical-phase/BMS/pending-dispatch suites and the 488-case matrix.
The generator's second pass was byte-identical for all 14 managed outputs.
All three public entry configurations and the complete local firmware build
passed with pinned ESPHome `2026.7.2`; the target Builder `2026.8.2` validated
and compiled without the deprecated-setting warning. Remaining compiler
`-Wempty-body` warnings originate in ESPHome's Modbus debug logging.
The full release validator is blocked by its clean-working-tree requirement;
the user's pre-existing `AGENTS.md` change is preserved. This is not a public
release or a new live power/SOC sweep. The historical gate table below continues
to describe the earlier frozen candidate, not this patch.

## Release gates

| Requirement | Command/environment | Evidence scope | Status |
| --- | --- | --- | --- |
| Accepted source identity | exact commit, parent and tree checks in `tools/validate_release.py` and `tools/validate_rce_card.js` | baseline commit/tree above | PASS |
| Generated assets | Python 3.12.14, `python tools/build_hacs_assets.py` twice | 294 entities; second pass byte-identical | PASS |
| Structural release validation | Python 3.12.14, `python tools/validate_release.py` | candidate release delta, PL/EN assets and regression matrix | PASS after the handoff material was added; exact final-commit evidence is retained in the private consolidation report |
| Standalone frontend validator | Node 24.19.0, `node tools/validate_rce_card.js` | C4 278 checks; RCE 48 h 75 checks; Aurora/Supervisor/card contracts | PASS after the handoff material was added; exact final-commit evidence is retained in the private consolidation report |
| Full Python regression set | Python 3.12.14 | accepted baseline; retained full-gate receipt and exact source hashes | PASS, reused because later product-code delta is versioning/localization only |
| Exhaustive automation matrix | `python tools/test_automation_matrix.py --exhaustive` | accepted baseline, 2064 cases | PASS, retained receipt |
| ESPHome configuration | ESPHome 2026.7.2, `esphome config tools/esphome_verify_ci.yaml` | candidate `2d075a18...` | PASS |
| ESPHome compile | ESPHome 2026.7.2, `esphome compile tools/esphome_verify_ci.yaml` | 991,131-byte image; RAM 43.9%; Flash 54.0% | PASS locally |
| Home Assistant pinned runtime | Python 3.14.7, Home Assistant 2026.8.2; two runtime tests from CI | exact candidate ref | NOT RUN locally |
| HACS Action | GitHub Actions on exact publication ref | public repository layout | NOT RUN on candidate ref |
| Hassfest | GitHub Actions on exact publication ref | integration metadata and translations | NOT RUN on candidate ref |
| CI firmware job | `workflow_dispatch` with firmware compile on exact ref | immutable publication candidate | PENDING |
| Exact-candidate field campaign | requirements in `RELEASING.md` | exact integration/package/firmware tag | NO EVIDENCE YET |

The retained earlier full gate is scoped to the accepted consolidated product
bytes. The later localization change affects only two English status strings;
release-validator changes freeze the new clean Git shape. Neither turns earlier
field observations into exact-candidate field acceptance.

## Field evidence and current limits

- The installation_2 accepted the frozen Aurora UI on a physical iPhone 14
  and now serves revision `.72` assets matching the local candidate. This is UI
  evidence, not a full exact-candidate inverter campaign.
- The local laboratory installation has an earlier tariff FIELD PASS and a
  later 44/44 frozen-base alignment receipt. A read-only check on 2026-09-09
  found `.72` frontend assets matching the local candidate. It does not prove
  every later shared-code path on hardware.
- The deferred third field installation has a bounded RCE FIELD PASS covering
  50→40→50%, a later natural 49% retarget, more than 125 seconds of active work,
  a newer physical FC03 and STOP/restore. A read-only check on 2026-09-09 found
  that it was reachable but still served `.71` frontend assets. Its update is
  owned by the existing deployment task and is not part of this local
  consolidation.
- Offline validation confirms the protective 60-second settling boundary. The
  short field run does not satisfy the complete release campaign in
  `RELEASING.md`, nor does it prove per-Slave acknowledgement.

## User update steps / Kroki po aktualizacji

### English

1. Keep all EMS automations disabled and record the current physical mode and
   important user settings.
2. Update **EMS for Hoymiles HIT-(5–20)L-G3** to `v1.5.8` through HACS.
3. Follow the managed-package preflight in the release notes. Preserve any
   locally modified scheduler, then perform the required Home Assistant check
   and two full restarts so the Python runtime and managed package both load.
4. Build and upload ESP32 firmware from the immutable `v1.5.8` tag. HACS does
   not perform this step.
5. Confirm integration `1.5.8`, package `1.5.8 / Ready`, frontend
   `1.5.8.72`, fresh physical FC03 readback, physical Self-Use and no relevant
   Repair/fault before enabling one automation at a time.
6. Existing user settings remain in storage. The 5% reserves, 50% powers and
   PLN 1/kWh tariff saving are seeded only for a fresh installation. A locally
   modified managed scheduler is preserved until the user explicitly replaces
   it.

### Polski

1. Pozostaw wszystkie automatyki EMS wyłączone i zapisz bieżący tryb fizyczny
   oraz ważne ustawienia użytkownika.
2. Zaktualizuj **EMS for Hoymiles HIT-(5–20)L-G3** w HACS do `v1.5.8`.
3. Wykonaj preflight pakietu z notatek wydania. Zachowaj lokalnie zmodyfikowany
   scheduler, wykonaj sprawdzenie HA i dwa pełne restarty, aby załadować runtime
   Python oraz zarządzany pakiet.
4. Zbuduj i wgraj firmware ESP32 z niezmiennego tagu `v1.5.8`. HACS tego nie
   wykonuje.
5. Przed włączaniem automatyk potwierdź integrację `1.5.8`, pakiet
   `1.5.8 / Gotowe`, frontend `1.5.8.72`, świeży fizyczny FC03, fizyczny
   Self-Use oraz brak istotnych Napraw/błędów. Włączaj po jednej automatyce.
6. Dotychczasowe ustawienia użytkownika pozostają w storage. Rezerwy 5%, moce
   50% i minimalna oszczędność taryfy 1 PLN/kWh są zasiewane tylko przy świeżej
   instalacji. Lokalnie zmodyfikowany scheduler nie jest zastępowany bez jawnej
   decyzji użytkownika.

The complete HACS-visible bilingual instructions remain in `CHANGELOG.md` and
`docs/releases/v1.5.8.md` under the same required heading.

## Publication sequence

1. Fetch current public refs and confirm that `v1.5.8` is still free and the
   candidate has not gained unreviewed product changes.
2. Regenerate assets twice and run the union of `RELEASING.md`, `AGENTS.md` and
   `.github/workflows/validate.yml`, including the pinned HA runtime tests.
3. Transfer the reviewed commits to the existing repository without rewriting
   published history, then run HACS, Hassfest and all CI on that exact ref.
4. Run the explicitly enabled CI firmware compile, create immutable tag
   `v1.5.8`, and create the GitHub Release from `docs/releases/v1.5.8.md`.
5. Verify that HACS detects `v1.5.8`, renders the update instructions, and that
   the ESPHome package URLs resolve from the immutable tag before user rollout.
6. Perform the documented exact-version field campaign and retain its evidence.

Publication of a commit, creation of an immutable tag/GitHub Release, HACS
detection, firmware rollout and field acceptance are separate states.

## PV-NOC-01 local delta

The local candidate now contains one shared, bounded forecast-usefulness
evaluation used by RCE, tariff charging and RCEm. It preserves raw source age
and each policy's existing freshness TTL. During a verified Solcast scheduled
pause, stale numeric data remains usable only through a finite deadline and
only with a matching, complete date horizon. Diagnostics expose `fresh`,
`scheduled_pause` or `expired`, usability/reason, last successful API update,
next update, deadline and coverage dates. Wrong dates, incomplete coverage,
future timestamps, invalid values and missed deadlines remain fail-closed.

Offline PV-NOC-01 tests and the existing RCE/tariff/RCEm matrices pass. The
observed installation_1 incident and current Solcast metadata are retained in
`tools/fixtures/pv_noc01_installation_1_2026_09_13.json`. This delta has not been
deployed to Home Assistant. The full night-to-first-update observation remains
`NIGHT_TRANSITION_LIVE=PENDING` and is a publication gate, not a blocker for
the later bounded daytime installation_1 acceptance requested for this candidate.

## Rollback

Retain the local Git history bundle and private pre-consolidation patches. For a
software rollback, disable EMS automation, restore the last known compatible
integration/package/firmware set from immutable tag `v1.5.7` according to its
release notes, run Home Assistant checks and full restarts, and verify physical
readback before restoring automation. Never move or rewrite the published
`v1.5.7` tag. Site-specific backups and local scheduler changes must be handled
as separate evidence; a source checkout alone is not proof that a host loaded
the rollback.
