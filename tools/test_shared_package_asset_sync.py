"""Focused fresh-install delivery check for the shared EMS package."""

from __future__ import annotations

from pathlib import Path
import tempfile

from test_supervisor_helpers_contract import (
    FakeHass,
    ROOT,
    _load_assets_module,
    _run_install,
    _write_helper_storage,
)


def test_fresh_install_delivers_localized_shared_package() -> None:
    assets = _load_assets_module()
    with tempfile.TemporaryDirectory(prefix="ems_shared_asset_") as tmp:
        config_path = Path(tmp)
        _write_helper_storage(config_path)
        hass = FakeHass(config_path)
        written = _run_install(assets, hass)
        destination = config_path / "packages" / "hoymiles_ems_shared_inputs.yaml"
        source = (
            ROOT
            / "custom_components"
            / "hoymiles_hit_modbus"
            / "resources"
            / "home_assistant"
            / "pl"
            / "hoymiles_ems_shared_inputs.yaml"
        )
        assert destination in written
        assert destination.read_bytes() == source.read_bytes()
        guide = config_path / "www" / "hoymiles-update-guide.js"
        assert guide in written
        assert guide.read_bytes() == (ROOT / "home_assistant/www/hoymiles-update-guide.js").read_bytes()
        assert len(written) == 9
        assert hass.store_payload["assets"][
            "packages/hoymiles_ems_shared_inputs.yaml"
        ] == assets._sha256(source)


def main() -> None:
    test_fresh_install_delivers_localized_shared_package()
    print("Shared package asset sync: fresh localized install passed")


if __name__ == "__main__":
    main()
