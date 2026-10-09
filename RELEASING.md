# Release procedure

Current release preparation: **1.5.8 stable**, tag `v1.5.8`.
Use the [stable publication map](docs/releases/1.5.8/PUBLICATION.md) and
[public release body](docs/releases/v1.5.8.md). Compatible firmware stays at
`v1.5.8RC2`; the immutable RC2 tag/assets and historical gates remain intact.
Preparing these files alone does not authorize push, tagging or publication.

For routine task closure before a release freeze, use
[the version-independent EMS closeout prompt](docs/EMS_CHANGE_CLOSEOUT_PROMPT.md)
and the integration branch recorded in [WORK_STATE](docs/WORK_STATE.MD).
An integration checkpoint does not satisfy the release gates below.

This file is the persistent release checklist for maintainers. HACS presents
the GitHub Release body to users, so every release must explain the required
post-update actions in the order in which they must be performed.

## Required user-action section

Every `CHANGELOG.md` release entry and the matching GitHub Release body must
contain this exact heading:

```markdown
### User update steps / Kroki po aktualizacji
```

The section must contain a numbered, bilingual list covering:

1. **HACS** — whether the integration must be updated through HACS.
2. **Home Assistant** — whether a restart, reload or configuration check is
   required.
3. **ESP32 / ESPHome** — explicitly state whether firmware must be rebuilt and
   uploaded.
4. **Verification / Weryfikacja** — what the user should check after the
   update.

Do not write only “update normally”. If a step is unnecessary, state that
explicitly, for example:

```markdown
3. **ESP32 / ESPHome:** no firmware rebuild is required for this release.
   **PL:** ta wersja nie wymaga ponownej kompilacji ani wgrywania firmware ESP32.
```

## When ESP32 recompilation is mandatory

Tell users to rebuild and upload firmware whenever a release changes runtime
firmware behavior in any of:

- `packages/*.yaml`;
- runtime sections of `hoymiles-inverter.yaml` or `examples/esphome/*.yaml`;
- source entities expected by `entity_catalog.json`;
- UART, Modbus or ESPHome API configuration.

A documentation comment or `dashboard_import`-only change does not require an
existing user to rebuild firmware. State that explicitly and describe the
adoption metadata as optional. Do not claim that HACS or `dashboard_import`
flashes firmware automatically.

The instructions must say that HACS updates the Home Assistant integration but
does not flash the ESP32. Users must use the top-level ESPHome file and the
compatible remote-package tag named in that release's notes. When firmware is
unchanged, retain and document the last compatible ESPHome tag.

When a release changes both Home Assistant control logic and firmware source
freshness or actuator semantics, update Home Assistant first while every
automatic writer is disabled and observation-only modes remain enabled. Verify
that the physical EMS mode and controlled registers did not change across the
required Home Assistant restart(s). Only then rebuild and upload ESPHome from
the matching immutable tag, and repeat the no-write verification before
restoring the previous automation policy.

## Parallel Master/Slave live-evidence gate

A field run accepts only the exact integration, managed Home Assistant package,
ESPHome firmware/project and candidate commit recorded for that run. Evidence
from an older deployed stack may validate wiring or protocol behaviour, but it
must not be promoted to software acceptance of a later release. Record local
timezone and exact versions before the first command.

For a shared-bus release acceptance:

- snapshot physical Master FC03 generation and registers `4300–4306`, GCF
  `258/259`, battery charge register `306`, SOC, ownership flags, timers and
  faults before, during and after the run;
- retain the exact Home Assistant start service-event timestamp, the first
  physical response, the newer Master FC03 mode code `5`, and at least two
  stable aggregate grid/LOAD/PV/battery samples with their source timestamps;
- retain separately timestamped manufacturer-application evidence for the
  Master and every Slave showing both mode and per-node power. An operator
  statement without retained screenshots or per-node power is supporting
  evidence only;
- retain the exact stop service-event timestamp and identify whether the stop
  was automatic, timer-driven or manual. Record the later Master FC03 code `0`,
  separate Master/Slave Self-Use evidence, physical safe-power time,
  timer/ownership release and final fault state; and
- report command-to-Master-ACK, command-to-each-vendor-node,
  command-to-safe-power and command-to-owner-release latencies separately. If
  the command timestamp is missing, publish only the observed time interval
  from the first available stop-side marker and state that exact stop latency
  is unknown.

A matching Master FC03 acknowledges the Master configuration only. Aggregate
power beyond one inverter's rating is strong physical corroboration under a
controlled power balance, but it is not acknowledgement from a named Slave.
The v1.5.6 post-command diagnostic preserves that boundary. After Master FC03
configuration acknowledgement it applies 20 seconds of transition grace, then
examines five newer complete generations with a maximum 20-second wait for each
and requires three consecutive stable generations. Its advertised horizon is
135 seconds. It reports a system-level physical response and never a per-Slave
protocol acknowledgement. RCE uses a frozen authoritative target, requires at
least 0.25 kW grid export and fails closed through the existing neutral rollback
if the response is not confirmed. Manual/manual-recovery and RCEm pre-discharge
without an authoritative total-kW target evaluate fresh stable battery-
discharge direction without an export or amplitude rejection. Self-Use rollback
must not wait for aggregate discharge confirmation.

Latch topology before Mode 5. Unknown topology must block that command, and a
changed topology must prevent confirmation rather than allowing a later live
value to validate the old transaction. Preserve the diagnostic state sequence
`pending` → `confirmed|not_confirmed|not_evaluable` and the best-effort peak
scope; the sampled peak is not a claim about the instantaneous maximum.

Do not classify a single transition sample as either successful steady-state
response or a failure. Record it diagnostically and restart the bounded stable
window. This rule is motivated by the specific test installation: history for
8–14 August, 19:00–22:00 local, contains an approximately 60 kW stop transient
on 8 August and an approximately 60 kW start transient aligned with the stored
mode-code change on 14 August. The 9–13 August windows show repeated discharge
plateaus and switching impulses, although recorder cadence may miss the full
peak. Separately, the 15 August live trace at 18:20 local captured 63.069 kW
battery / 65.910 kW inverter during a switch. Keep this claim installation-
and date-bounded; never turn it into a universal inverter characteristic.

The 2026-08-15 run documented in `docs/AUTOMATION_TEST_REPORT.md` used managed
package 1.5.4 and firmware/project 1.5.3 and lacks retained per-node power and
an exact manual-stop timestamp. It is therefore hardware/protocol evidence,
not acceptance of v1.5.5 or v1.5.6. Repeat the full gate on the exact v1.5.6
candidate before publication.

### Battery-balancing manual recovery

`RECOVERY_REQUIRED` means that an active/owned balancing cycle has no trusted
current-cycle snapshot. It is intentionally not migrated or restored
automatically. Keep balancing disabled, do not clear the active/lifecycle
helpers, and retain the transaction reason, cycle ID, current physical mode,
4303/4304 readbacks and a support bundle. A qualified operator must establish
the intended settings from commissioning evidence or manufacturer controls,
never from the untrusted legacy record, and obtain fresh physical readbacks.
Only after the balancing timers and verified-write scripts are idle and those
readbacks have been reviewed may a maintainer release the retained owner and
reset the internal lifecycle. Until then, leave the fail-closed reservation in
place. This is a support/commissioning procedure, not field proof and not an
automatic legacy-migration promise.

## Frontend asset startup contract

When a release changes managed dashboard or frontend assets:

- the release notes must require a Home Assistant restart and a hard refresh of
  any dashboard tab that remained open across that restart;
- copy every `/local` dependency before publishing its versioned URL, and load
  one canonical full module instead of competing bootstrap and bundle strategy
  implementations;
- update storage-mode resources through Lovelace's live resource collection;
  never mutate `.storage/lovelace.*` directly while Home Assistant is running;
- if `config/www` did not exist when frontend started, copy the assets but defer
  publication, raise a localized restart Repair and verify the next boot; and
- validate repeated module loading, fresh-no-`www`, storage and YAML modes
  offline. For a release candidate, also verify the exact versioned resource,
  dashboard render and absence of a strategy-registration timeout in a fresh
  browser session after restart; and
- record the exact commit SHA installed on every live test host before a
  documentation-only release finalization commit is created. If the eventual
  release/tag SHA differs, record it separately, verify that the delta contains
  no runtime changes, and never describe that later SHA as the deployed
  candidate.

## Repository rename cutover completed with v1.5.5

The v1.5.5 coordinated cutover establishes these exact public metadata values:

- **Repository:** `Kaluzaburza/hoymiles-hit-g3-ems`
- **Project:** `EMS for Hoymiles HIT-(5–20)L-G3`
- **GitHub description:** `Unofficial local EMS for Hoymiles HIT-G3 hybrid
  inverters — Home Assistant, ESPHome, Modbus, RCE, tariff optimization and
  RCEm.`

The description is also the English README tagline. Treat the repository
rename, metadata changes, release tag and HACS publication as one cutover: the
ESPHome remote package and `dashboard_import` URLs must remain fetchable at
every point.

The v1.5.5 cutover contract is:

1. Rename the GitHub repository and set its About/description field to the
   exact text above. Do not create a different repository under the old slug;
   GitHub's redirect must continue protecting installed configurations.
2. Update the local `origin` and every current repository URL, including HACS
   badges/instructions, manifest documentation and issue tracker, Repairs and
   dashboard links, issue templates, `NOTICE`, ESPHome `dashboard_import` and
   remote-package URLs, and their release-validator expectations.
3. Change the user-facing HACS/Home Assistant/dashboard project title to the
   exact project value above, update the canonical asset-generator sources,
   and regenerate all localized/bundled copies.
4. Keep all technical identities unchanged: the Home Assistant domain and
   component directory `hoymiles_hit_modbus`, entity/service/unique IDs,
   storage keys, dashboard strategy type `hoymiles-hit-xxl-g3`, ESPHome node
   names, and ESPHome `project.name: hoymiles.energy-storage-modbus`.
5. Run the complete release gate, then verify the new repository with HTTP,
   `git ls-remote`, HACS/hassfest, ESPHome `dashboard_import`, and a clean
   remote-package compile from the immutable v1.5.5 release tag.

Keep the old slug unclaimed after the cutover so GitHub's redirect continues
to protect existing HACS and ESPHome configurations. Never rename the stable
technical identities listed above as part of a later branding change.

## v1.5.8 accepted-base and release-delta contract

Release preparation starts from accepted N12 commit
`c736b69d985d6f2a75015abdacb8839a461ac7f1`, tree
`5c15dae9b4b429a22442de1b2fec953c3f89be2d`. The release validator accepts only
that product plus the exact file/mode/content set pinned by
`tools/release_manifests/v1_5_8_release_delta.json`. The branch name grants no
authority. The protected integration, packages, canonical HA assets, example
firmware and top-level device/dashboard sources must be byte/mode/type-identical
to N12. A runtime or firmware change is a new product candidate: reopen the
applicable validation and field acceptance instead of adding it to the
documentation delta.

For the later closed M01 evidence delta, first record the exact candidate and
external evidence, then select the smallest explicit documentation/report file
set. Review every byte, status and mode; pin the new file hashes and manifest
identity in a new commit; rerun the release-contract mutations, full validator
and every gate affected by the claims. Do not allow all of `docs/`, edit the
historical N12 evidence, or put the manifest's own hash/commit SHA inside the
same commit. Record the final commit and tree in the external handoff after the
commit exists. M01 documentation cannot authorize product bytes.

## Release checklist

1. Move the completed `Unreleased` notes to the new version heading.
2. Review the user steps and remove instructions that are not required for
   that particular version.
3. Update the integration version numbers. Update remote ESPHome package tags
   only when runtime firmware changes; otherwise keep the last compatible tag
   and explain explicitly that no ESP32 rebuild is required.
4. Run the release validators and tests:

   **Python 3.12 — structural/offline gate.** Run the generator, validator,
   focused contract and the remaining non-HA commands below with the reviewed
   Python 3.12 environment. Do not install Home Assistant into this environment.

   ```text
   python tools/build_hacs_assets.py
   git diff --exit-code
   python tools/validate_release.py
   python tools/test_rce_optimizer.py
   python tools/test_rce_history.py
   python tools/test_tariff_profiles.py
   python tools/test_tariff_optimizer.py
   python tools/test_rcm_history.py
   python tools/test_rcm_optimizer.py
   python tools/test_automation_plan_timeline.py
   python tools/test_rcm_timeline_model.py
   python tools/test_energy_data.py
   python tools/test_load_model.py
   python tools/test_power_balance.py
   python tools/test_firmware_readback_contract.py
   python tools/test_overview_control_poll_contract.py
   python tools/test_supervisor_transport_lifecycle_contract.py
   python tools/test_supervisor_adapter_response.py
   python tools/test_supervisor_not_queued_reprepare.py
   python tools/test_rce_run_end_extension.py
   python tools/test_rce_small_power_retarget.py
   python tools/test_optimizer_executor_contract.py
   python tools/test_optimizer_startup_contract.py
   python tools/test_source_device_rebind.py
   python tools/test_battery_balancing_contract.py
   python tools/test_automation_matrix.py
   python tools/test_diagnostics.py
   python tools/test_diagnostics_future.py
   python tools/test_pstryk_buy_refinement.py
   python tools/test_lease_acceptance_journal.py
   python tools/test_stable_release_contract.py
   python tools/test_stable_public_snapshot.py
   python tools/test_upgrade_from_157.py
   python tools/test_upgrade_from_rc2.py
   python tools/test_diagnostic_analyzer.py
   python tools/test_automation_matrix.py --exhaustive
   node tools/test_supervisor_aurora_ui_contract.js
   node tools/validate_rce_card.js
   ```

   **Python 3.14.7 + Home Assistant 2026.8.2 — isolated runtime gate.** In a
   separate disposable virtual environment whose interpreter reports exactly
   Python 3.14.7 and whose installed `homeassistant` reports exactly 2026.8.2,
   run only:

   ```text
   python tools/test_battery_balancing_ha_runtime.py
   python tools/test_shared_load_current_slot.py
   python tools/test_shared_load_rcm.py
   python tools/test_shared_load_execution_budget.py
   python tools/test_ems_notifications.py
   python -m pytest -q tests/test_timeline_platform_registration.py
   python tools/test_ems_initial_defaults.py
   python tools/test_ems_shared_input_migration.py
   python -m pytest -q tests/test_ems_initial_defaults_storage.py tests/test_ems_shared_input_migration_storage.py
   ```

   Do not merge this command into the Python 3.12 path and do not accept a
   different Home Assistant version as equivalent evidence.

   **Python 3.14.7 + Home Assistant 2026.9.2 — isolated notifier gate.** Also
   run the complete `tools/test_ems_notifications.py` in the retained isolated
   2026.9.2 environment. It is a separate compatibility result: neither the
   2026.8.2 runtime contract nor the 2026.9.2 notifier run substitutes for the
   other. Do not mount production `/config`, tokens, devices or notification
   services.

   For the v1.5.8 balancing gate, retain the exact service-boundary fixtures:
   generation drift before the first physical helper; SOC loss across the
   `HOLD_ARMING` timing write; sunrise, sunset and owner/hard-stop changes at
   the guarded lifecycle write; and 100 lower-priority triggers followed by a
   101st higher-priority trigger. The accepted steady lifecycle is conditional:
   the durable record remains raw `APPLYING` with a phase/mode token, and the
   canonical parser may expose the steady phase only while the live sun, mode,
   owner, cycle, timing and abort guards match. These are isolated offline
   checks, not real-inverter or field acceptance.

   Run the generator determinism check from a clean release-preparation tree;
   unrelated working-tree changes must not be mistaken for generated-asset
   drift.

   The exhaustive matrix must report 2064 passed scenarios unless its reviewed
   scenario set was intentionally changed. Record the new count in
   `docs/AUTOMATION_TEST_REPORT.md`.
   It must also report positive RCE power for every representative model,
   non-zero joint-solver and planned-export coverage, and all four explicit BMS
   fail-closed contracts. A scenario count without these coverage counters is
   not release evidence.
   Before publication, run `workflow_dispatch` for the exact candidate ref,
   record its SHA and require the conditional `firmware-compile` job to pass.
   To reproduce that CI job locally, use its exact pin and fixture:

   ```text
   python -m pip install --disable-pip-version-check "esphome==2026.9.0"
   python tools/test_esphome_entry_points.py
   python tools/test_esphome_2026_9_migration.py
   esphome config tools/esphome_verify_ci.yaml
   python tools/test_esphome_entry_points.py --compile esp32
   python tools/test_esphome_entry_points.py --compile flow-control
   python tools/test_esphome_entry_points.py --compile s3
   ```

   The public-entry test validates all three root device files and the example
   against this checkout's complete local package set, using placeholder
   secrets. It also reproduces the invalid UART list/`!extend` form from #33.
   When changing the manual-direction variant, additionally run
   `python tools/test_esphome_entry_points.py --compile-flow-control`.
   This builds the actual flow-control entry file with local package paths;
   it does not upload firmware or require the unpublished release tag.

5. Create the GitHub tag and release.
6. Copy the complete version notes, including the numbered bilingual user
   steps, into the GitHub Release body visible in HACS.
7. Confirm that HACS detects the new version and displays the instructions.
8. Confirm that `LICENSE`, `NOTICE`, `LICENSE_POLICY.md`, `CONTRIBUTING.md` and
   `.github/CODEOWNERS` are present and consistent with the MIT license.

## HACS and license compatibility

The project uses the OSI-approved MIT License. The official HACS Action,
Hassfest and all `project-checks` are mandatory and must pass without ignores
or `continue-on-error` before a release is published.

## Public 1.5.7 upgrade regression

Run `python tools/test_upgrade_from_157.py` on the exact candidate. It checks
immutable 1.5.7 PL/EN package delivery, backup, preservation of local edits,
294 existing source identities and seven copy-once legacy mappings. Run the
shared-input/default/storage/source-device suites as well. Keep numbered
user steps in CHANGELOG and the release body; link `docs/UPGRADE_1_5_7.md`
and `docs/ESP32_VARIANTS.md`. The firmware CI matrix compiles all three full
public profiles, not only a reduced smoke fixture.


## Public snapshot provenance

The stable public branch continues the public RC2 history rooted at 1.5.7.
Its exact parent, subject, paths, modes and bytes are pinned by the separate
stable contract. `rc2_public_provenance.json` retains the original runtime
hashes; the stable gate allows only five explicit version-metadata changes
and checks all remaining protected runtime bytes against that source.
Run `python tools/test_stable_public_snapshot.py` and the RC2 installer
regression `python tools/test_upgrade_from_rc2.py` on the exact candidate.

Public CI runs the unchanged RC2 and public 1.5.7 gates on their own historical
commits, plus the full current behavioral suite. Workstation-only N12/Task02 Git-lineage validators
remain historical tools for their original checkouts; their original results
are retained separately and are not relabelled as tests of the public parent.
Current Recorder/RCE comparisons use exact, SHA256-verified source fixtures,
not missing private Git objects. Keep unknown or modified fixtures fail-closed.
