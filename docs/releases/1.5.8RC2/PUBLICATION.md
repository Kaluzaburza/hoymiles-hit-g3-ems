# 1.5.8RC2 publication identity

Public version **1.5.8RC2**, tag **v1.5.8RC2**, normalized HA/package/project
**1.5.8rc2**, frontend **1.5.8rc2.122**, lease **2**.

The integration runtime base is `58437c5eeb36e8a52baa358687392a147c784e8b`,
tree `5fe3292d8ae50bf31d85f94877ab00a39bdca163`. The release preparation adds
public S3 hardware configuration, migration verification, documentation and
CI/tooling. It does not change the integration runtime or shared Modbus packages.
The new S3 entry point is a separately compiled hardware profile, not a claim
that all firmware files are byte-identical to the earlier release.

The tag and attached source-manifest identify the final commit/tree. The strict
`tools/release_manifests/rc2_local_contract.json` pins every tracked file and mode;
`tools/rc2_release_contract.py` pins that manifest and the exact parent/subject.
Historical release contracts retain their original fixtures and requirements.

Follow [RELEASING](../../../RELEASING.md) and the entire
[CI workflow](../../../.github/workflows/validate.yml). Run exact-candidate
`workflow_dispatch`, including all three firmware compile jobs, before tagging.
Build the full source archive using `tools/build_rc2_source_archive.py` outside
the repository. Record its hash and manifest; do not attach private diagnostics,
credentials or device-specific firmware binaries.

The [release body](../v1.5.8rc2.md) contains the bilingual numbered user steps.
[Upgrade from 1.5.7](../../UPGRADE_1_5_7.md) explains the extra package/restart
and encrypted-OTA requirements. HACS custom-repository delivery and default
catalog inclusion are separate. [Icon status](HACS_ICON.md) covers the upstream
HACS limitation; publishing this release does not change HACS's own code.


The public commit has the published 1.5.7 history as its parent. Runtime
byte equivalence to the reviewed source is pinned separately in
`tools/release_manifests/rc2_public_provenance.json`. The release tag and
source archive identify the new public commit, not a workstation branch.
