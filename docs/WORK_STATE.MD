# Public work state — 1.5.8RC2

## Documentation correction after publication

The original ESP32-S3 / TTL-RS485 / HIT wiring illustration from 20 September
2026 has been restored at the user's request, including the original PNG/SVG and
their source attribution. The follow-up changes documentation and its source
manifest only; integration, frontend, firmware and shared packages are unchanged.
The published `v1.5.8RC2` tag remains at
`ba91261597420ea3a58a6665fc996d7081f219e2`, with its original release assets.
Use that tag to reproduce the published archive; the current source contract
also records this later documentation commit separately.

## Published source

The public snapshot starts from published 1.5.7. Integration/package version is
1.5.8rc2, frontend revision 122 and ESP lease protocol 2. The strict RC2 source
contract pins the parent, subject, all tracked files and modes. The tag and
attached source manifest identify the published commit and tree.

The reviewed integration runtime and shared packages are pinned separately in
`tools/release_manifests/rc2_public_provenance.json`. Publication preparation
adds the S3 N16R8 entry point, all-three-profile compile matrix, actual 1.5.7
upgrade regression, portable test dependencies and public documentation.

Current scope: separate purchase/sale accounting, common household LOAD,
qualified RCE/Pstryk continuity and sale consolidation, PV Delay cooldown,
tariff continuity, additional SOC 0–90%, history-query compatibility and
ESPHome 2026.9 migration. No live deployment is performed by release tooling.

See [release notes](releases/v1.5.8rc2.md),
[upgrade instructions](UPGRADE_1_5_7.md),
[hardware profiles](ESP32_VARIANTS.md),
[publication identity](releases/1.5.8RC2/PUBLICATION.md) and
[release procedure](../RELEASING.md).

Use the full union of CI and RELEASING checks on the exact candidate. Simulation,
source parity and compilation do not prove physical inverter acceptance.
Private device identities, deployment timelines and operator decisions are
maintained outside the public repository.
