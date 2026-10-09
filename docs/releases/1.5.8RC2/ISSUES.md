# Known limitations — 1.5.8RC2

- HACS 2.0.5 can omit the repository-list icon for custom integrations using
  local branding. The README logo is a details-page mitigation; see [status](HACS_ICON.md).
- Custom managed YAML is preserved during upgrades. Repairs may require a
  deliberate replacement after backup. Older OTA may need an intermediate
  ESPHome build; see [1.5.7 migration](../../UPGRADE_1_5_7.md).
- Missing historical prices, counters or execution evidence remain gaps. The
  earnings model is not billing and does not reconstruct missing old data.
- A bounded lease journal lives in memory: restart, retention overflow or a
  missing export limits historical evidence. It does not add Recorder writes.
- RCEm remains experimental. A Master FC03 confirms Master settings, not an
  individual Slave ACK. Community model reports do not establish universal
  hardware compatibility. See [scope](../../COMPATIBILITY.md).

When reporting a new issue, include version, timing and a redacted diagnostic
ZIP through an appropriate private channel; do not post API/Wi-Fi secrets.
