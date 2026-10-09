# Public work state — 1.5.8.1

## HACS upgrade-guide hotfix

Integration **1.5.8.1**, frontend **1.5.8.1.123**, EMS package **1.5.8**.
Parent: `dccf6629d695bf858ad33d9aa353799348f6621e` (public stable baseline).
The bounded administrator-only YAML preparation and persistent PL/EN guide
are described in [release notes](releases/v1.5.8.1.md). The exact contract pins
every tracked byte/mode and limits runtime changes to the guide, HTTP registration,
asset delivery and integration version metadata. Publication is confirmed by the
GitHub release/tag and attached source manifest, not by this document alone.

Firmware, scheduling, control and user settings remain unchanged. Prepared
configs use firmware SHA `ba91261597420ea3a58a6665fc996d7081f219e2`.
Existing 1.5.8, RC2 and 1.5.7 gates run separately on their original commits.
No private installation records or field-acceptance decisions belong here.
Future 1.5.9 development remains a separate line.

## Published baseline (unchanged)

Stable integration/package **1.5.8**, frontend **1.5.8.122**, ESP lease **2**.
This release promotes the published RC2 functionality. Control algorithms and
firmware are unchanged. Compatible firmware retains tag `v1.5.8RC2` and project
version `1.5.8rc2`; no new OTA is needed solely for stable promotion.

Source base: `c919ed9fbe8858d28f0dce0650ccfdb65af8b31c`, the public main revision
with the restored wiring illustration and coffee links. The new stable contract
pins every tracked file/mode and verifies that protected runtime changes consist
only of the explicit integration/package version strings. Historical RC2 and
1.5.7 contracts remain separate, unchanged and exercised on their own commits.

See [release notes](releases/v1.5.8.md), [upgrade from 1.5.7](UPGRADE_1_5_7.md),
[publication identity](releases/1.5.8/PUBLICATION.md), [ESP profiles](ESP32_VARIANTS.md)
and [release procedure](../RELEASING.md). The tag and attached archive manifest
record the final commit/tree. Use the full union of CI and RELEASING gates on
that exact commit; compilation and simulation do not prove physical acceptance.

No installation deployment or firmware upload is part of source publication.
Private installation records and operator decisions remain outside this repo.
Future 1.5.9 work starts from this stable public base as a separate development line.
