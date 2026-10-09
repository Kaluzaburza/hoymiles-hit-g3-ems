# 1.5.8 publication identity

Tag **v1.5.8**, integration/package **1.5.8**, frontend **1.5.8.122**, lease **2**.
Compatible unchanged firmware: **v1.5.8RC2**, project **1.5.8rc2**.

The source parent is `c919ed9fbe8858d28f0dce0650ccfdb65af8b31c`. The strict
`tools/stable_release_contract.py` and `tools/release_manifests/stable_1_5_8_contract.json`
pin the exact subject, parent, tracked bytes and modes. Protected runtime parity
allows only the five listed version-metadata files to replace `1.5.8rc2` with
`1.5.8`; all other reviewed runtime bytes and all firmware files stay unchanged.

Historical RC2 contracts run in a disposable checkout of their original public
commit, retaining the original 1.5.7 gate. Follow the entire [release procedure](../../../RELEASING.md)
and [CI workflow](../../../.github/workflows/validate.yml) on the new candidate,
including `workflow_dispatch` with all three firmware compiles before tagging.
Build the archive outside the checkout with `tools/build_stable_source_archive.py`;
attach the ZIP, source manifest and SHA256SUMS to the stable GitHub Release.
Never replace an existing tag or release asset. The tag and archive identify the
final commit/tree. Private diagnostics and device-specific binaries are excluded.

[Release body](../v1.5.8.md) · [Upgrade guide](../../UPGRADE_1_5_7.md).
HACS detects stable 1.5.8 through the normal update channel; detection is separate
from installation. Default-catalog inclusion and HACS icon support are separate.
