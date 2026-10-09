# Public work state — 1.5.8

Stable integration/package **1.5.8**, frontend **1.5.8.122**, ESP lease **2**.
This release promotes the published RC2 functionality. Control algorithms and
firmware are unchanged. Compatible firmware retains tag `v1.5.8RC2` and project
version `1.5.8rc2`; no new OTA is needed solely for stable promotion.

Source parent: `c919ed9fbe8858d28f0dce0650ccfdb65af8b31c`, the public main revision
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
