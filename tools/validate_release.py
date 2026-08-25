"""Structural release validation without requiring a Home Assistant checkout."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import io
import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "custom_components"
COMPONENT = COMPONENT_ROOT / "hoymiles_hit_modbus"
RESOURCES = COMPONENT / "resources"
EXPECTED_PROJECT_NAME = "EMS for Hoymiles HIT-(5–20)L-G3"
EXPECTED_REPOSITORY = "Kaluzaburza/hoymiles-hit-g3-ems"
EXPECTED_DESCRIPTION = (
    "Unofficial local EMS for Hoymiles HIT-G3 hybrid inverters — "
    "Home Assistant, ESPHome, Modbus, RCE, tariff optimization and RCEm."
)
LEGACY_REPOSITORY_SLUG = "Hoymiles_HIT_xxL_G3_ModBus"
VALIDATOR_PACKAGE_MARKER = "1.5.7-supervisor-1b3"
VALIDATOR_MARKER_ENTITY = "sensor.hoymiles_ems_package_version"
VALIDATOR_SCHEDULER_RELATIVE = "packages/hoymiles_ems_scheduler.yaml"
VALIDATOR_BACKUP_SUFFIX = ".pre-ems-supervisor-1b3.bak"
VALIDATOR_OLD_TEMP = ".hoymiles_ems_scheduler.yaml.hoymiles_hit_modbus.tmp"
VALIDATOR_HELPER_IDS = (
    "input_select.hoymiles_ems_supervisor_mode",
    "input_select.hoymiles_ems_supervisor_profile",
    "input_boolean.hoymiles_ems_supervisor_allow_rce",
    "input_boolean.hoymiles_ems_supervisor_allow_tariff",
    "input_boolean.hoymiles_ems_supervisor_allow_rcm",
)
VALIDATOR_NEW_HASHES = {
    "pl": "b66b591372c654424c491206e61815bc5457eca8514f4c1cffc5f205c7367691",
    "en": "3df7345f0ee9649a35160b4817d6d3dd9d0c95ecf93eed2bf07ec1fe2633886a",
}
VALIDATOR_HISTORICAL_HASHES = {
    "pl": "9846bfe0d0e9f8f707db7b3eb5b30fd349b663f8fb1c3777b460ef62696026a2",
    "en": "b76ba6a2a9f94d307d1582822101b2fc0951868ba7319394ce0886ee9fe9e07d",
}
VALIDATOR_HISTORICAL_REF = "v1.5.7"
REV28_HISTORICAL_COMMIT = "5fafc961e70b18b8677e58c8bfcc25613d1fd5c5"
AP1_CUMULATIVE_TASK_BASE = "5fafc961e70b18b8677e58c8bfcc25613d1fd5c5"
AP1E_CORRECTION_BASE = "f630529ed8ddce7ba5c45986d9484fc31b246070"
AP1_PUBLIC_BRANCH_BASE = "6617bc4de6592439ea2c64889b0a25bbe5bfa45e"
PHASE_2_TASK_PATHS = frozenset(
    {
        "dashboard_hoymiles.yaml",
        "home_assistant/www/hoymiles-rce-chart-card.js",
        "home_assistant/www/hoymiles-dashboard-strategy.js",
        "custom_components/hoymiles_hit_modbus/assets.py",
        "tools/build_hacs_assets.py",
        "tools/validate_rce_card.js",
        "tools/validate_release.py",
        "tools/test_supervisor_aurora_ui_contract.js",
        "custom_components/hoymiles_hit_modbus/resources/dashboard_hoymiles_pl.yaml",
        "custom_components/hoymiles_hit_modbus/resources/dashboard_hoymiles_en.yaml",
        "custom_components/hoymiles_hit_modbus/resources/www/dashboard_hoymiles_pl.json",
        "custom_components/hoymiles_hit_modbus/resources/www/dashboard_hoymiles_en.json",
        "custom_components/hoymiles_hit_modbus/resources/www/hoymiles-rce-chart-card.js",
        "custom_components/hoymiles_hit_modbus/resources/www/hoymiles-dashboard-strategy.js",
    }
)
REV28_CORRECTION_PATHS = frozenset(
    {
        "custom_components/hoymiles_hit_modbus/assets.py",
        "custom_components/hoymiles_hit_modbus/resources/www/hoymiles-dashboard-strategy.js",
        "custom_components/hoymiles_hit_modbus/resources/www/hoymiles-rce-chart-card.js",
        "home_assistant/www/hoymiles-dashboard-strategy.js",
        "home_assistant/www/hoymiles-rce-chart-card.js",
        "tools/test_supervisor_aurora_ui_contract.js",
        "tools/validate_rce_card.js",
        "tools/validate_release.py",
    }
)
REV28_PROTECTED_TASK_HASHES = {
    "dashboard_hoymiles.yaml": "69efcb7b93d1463e29f8f273b45d4c96bca80f7a56dd65d5599164ab5857ee4d",
    "tools/build_hacs_assets.py": "07b7890d8999d5d115984d4ab0dc1cb9935fcf9e28423fbcebb5f4c10b81060b",
    "custom_components/hoymiles_hit_modbus/resources/dashboard_hoymiles_pl.yaml": "e8113f54a671591fddb2fce9abb3ee7394fd997cfb87f3a5724c2a3ee875347b",
    "custom_components/hoymiles_hit_modbus/resources/dashboard_hoymiles_en.yaml": "e56a60f49b407775228d41500fd14cd2bd67a5aba69c53efa513f358ca8bd9c6",
    "custom_components/hoymiles_hit_modbus/resources/www/dashboard_hoymiles_pl.json": "dca8d5ceb63c979b9adfc9b2e9c6d0153593509bfa13227de7bb28e45a41f628",
    "custom_components/hoymiles_hit_modbus/resources/www/dashboard_hoymiles_en.json": "4868d931824557e5e68dca4c9c0f33f2a20bd8636a55b5a98caf8af5f1e72b1c",
}
REV28_PROTECTED_BACKEND_HASHES = {
    "custom_components/hoymiles_hit_modbus/ems_supervisor.py": "031d0abf8948d24708b960deb6ee71c7fe952fdebecd915ac6fc766596429ca7",
    "custom_components/hoymiles_hit_modbus/supervisor_runtime.py": "12cf54cb8baeb8a161b4a6b49ac19f91daeb46beef3f949d9782f689f3889d7d",
    "custom_components/hoymiles_hit_modbus/supervisor_sensor.py": "1c18ac1eef5d46e2512574ea3dfc7f0c4bd62c58aae6296b2e7b4c479ba743f8",
    "custom_components/hoymiles_hit_modbus/sensor.py": "fe83d62990150145c3db595e4983f598752751d2488375dd938db961908311bf",
    "custom_components/hoymiles_hit_modbus/const.py": "eee0ffebe1197f0f74b488bce4c7e51066a33b96255944672f3fee288fc1d956",
    "home_assistant/hoymiles_ems_scheduler.yaml": "b66b591372c654424c491206e61815bc5457eca8514f4c1cffc5f205c7367691",
    "custom_components/hoymiles_hit_modbus/resources/home_assistant/en/hoymiles_ems_scheduler.yaml": "3df7345f0ee9649a35160b4817d6d3dd9d0c95ecf93eed2bf07ec1fe2633886a",
    "custom_components/hoymiles_hit_modbus/resources/home_assistant/pl/hoymiles_ems_scheduler.yaml": "b66b591372c654424c491206e61815bc5457eca8514f4c1cffc5f205c7367691",
    "custom_components/hoymiles_hit_modbus/tariff_optimizer.py": "7a77f3885a9f393179d40777770de5eee4ac1918b84a0dba5433531ccc344459",
    "custom_components/hoymiles_hit_modbus/tariff_sensor.py": "f90a54afd2e9f001ad466bb55941e480bbb6dc25f0b2b2c37d7c30fbcfe39ff5",
    "custom_components/hoymiles_hit_modbus/rce_optimizer.py": "f95ca95d8290995016ced33f12a9feec8306ca7e6bf224da385c956774866870",
    "custom_components/hoymiles_hit_modbus/rce_sensor.py": "d2401157dc90ba76069d24cd7d4bdc7ee15947c5173efe209cf986d8e88fef98",
    "custom_components/hoymiles_hit_modbus/rcm_optimizer.py": "ca533110396a2d24c9bf99cc73bb2e8843f782e53b914044dea83c60715b410b",
    "custom_components/hoymiles_hit_modbus/rcm_sensor.py": "5a66f6cdf6eae5a07b877db49c72e498ac65427dace0f3e774b3339363a3a087",
}
SUPERVISOR_BRANCH_PATHS = frozenset(
    {
        "custom_components/hoymiles_hit_modbus/__init__.py",
        "custom_components/hoymiles_hit_modbus/assets.py",
        "custom_components/hoymiles_hit_modbus/const.py",
        "custom_components/hoymiles_hit_modbus/ems_supervisor.py",
        "custom_components/hoymiles_hit_modbus/resources/dashboard_hoymiles_en.yaml",
        "custom_components/hoymiles_hit_modbus/resources/dashboard_hoymiles_pl.yaml",
        "custom_components/hoymiles_hit_modbus/resources/home_assistant/en/hoymiles_ems_scheduler.yaml",
        "custom_components/hoymiles_hit_modbus/resources/home_assistant/pl/hoymiles_ems_scheduler.yaml",
        "custom_components/hoymiles_hit_modbus/resources/www/dashboard_hoymiles_en.json",
        "custom_components/hoymiles_hit_modbus/resources/www/dashboard_hoymiles_pl.json",
        "custom_components/hoymiles_hit_modbus/resources/www/hoymiles-dashboard-strategy.js",
        "custom_components/hoymiles_hit_modbus/resources/www/hoymiles-rce-chart-card.js",
        "custom_components/hoymiles_hit_modbus/sensor.py",
        "custom_components/hoymiles_hit_modbus/supervisor_runtime.py",
        "custom_components/hoymiles_hit_modbus/supervisor_sensor.py",
        "custom_components/hoymiles_hit_modbus/tariff_optimizer.py",
        "custom_components/hoymiles_hit_modbus/tariff_sensor.py",
        "custom_components/hoymiles_hit_modbus/translations/en.json",
        "custom_components/hoymiles_hit_modbus/translations/pl.json",
        "dashboard_hoymiles.yaml",
        "home_assistant/hoymiles_ems_scheduler.yaml",
        "home_assistant/www/hoymiles-dashboard-strategy.js",
        "home_assistant/www/hoymiles-rce-chart-card.js",
        "tools/build_hacs_assets.py",
        "tools/test_ems_supervisor.py",
        "tools/test_supervisor_aurora_ui_contract.js",
        "tools/test_supervisor_helpers_contract.py",
        "tools/test_supervisor_runtime_contract.py",
        "tools/test_supervisor_sensor_contract.py",
        "tools/test_tariff_optimizer.py",
        "tools/validate_rce_card.js",
        "tools/validate_release.py",
    }
)
AP1_COMMITTED_TASK_PATHS = frozenset(
    {
        "custom_components/hoymiles_hit_modbus/automation_plan_timeline.py",
        "custom_components/hoymiles_hit_modbus/rce_optimizer.py",
        "custom_components/hoymiles_hit_modbus/rce_sensor.py",
        "custom_components/hoymiles_hit_modbus/sensor.py",
        "custom_components/hoymiles_hit_modbus/tariff_optimizer.py",
        "custom_components/hoymiles_hit_modbus/tariff_sensor.py",
        "custom_components/hoymiles_hit_modbus/timeline_sensor.py",
        "custom_components/hoymiles_hit_modbus/translations/en.json",
        "custom_components/hoymiles_hit_modbus/translations/pl.json",
        "tools/build_hacs_assets.py",
        "tools/test_automation_plan_timeline.py",
        "tools/test_optimizer_executor_contract.py",
        "tools/test_optimizer_startup_contract.py",
        "tools/test_rce_optimizer.py",
        "tools/test_tariff_optimizer.py",
        "tools/validate_release.py",
    }
)
AP1_COMMITTED_BRANCH_PATHS = SUPERVISOR_BRANCH_PATHS | AP1_COMMITTED_TASK_PATHS
AP1E_CUMULATIVE_TASK_PATHS = AP1_COMMITTED_TASK_PATHS | {
    "custom_components/hoymiles_hit_modbus/__init__.py",
    "tests/test_timeline_platform_registration.py",
}
AP1E_CORRECTION_PATHS = frozenset(
    {
        "custom_components/hoymiles_hit_modbus/__init__.py",
        "custom_components/hoymiles_hit_modbus/timeline_sensor.py",
        "tests/test_timeline_platform_registration.py",
        "tools/validate_release.py",
    }
)
AP1E_BRANCH_PATHS = SUPERVISOR_BRANCH_PATHS | AP1E_CUMULATIVE_TASK_PATHS
VALIDATOR_STORE_CONTRACTS = {
    "input_select": (1, frozenset({1, 2})),
    "input_boolean": (1, frozenset({1})),
}
VALIDATOR_TRANSITIONS = {
    "fresh": "INSTALLED_FRESH",
    "current": "CURRENT",
    "modified": "PRESERVED_MODIFIED",
    "collision": "BLOCKED_COLLISION",
    "destination_changed": "ABORTED_DESTINATION_CHANGED",
}
VALIDATOR_BACKUP_FIXTURE = b"# validator exact pre-update bytes\nstate: custom\n"
PROTECTED_ASSETS_AST_BASE = "c65e8b73096cb64ff2d21b6e2b05602f71a4a2af"
PROTECTED_ASSETS_AST_SHA256 = {
    "assets.HelperClassification": "1c2d5cb92be71c0e34a14e1d9b721b62bb99d697411e3cd14d3a4a67d3b66d51",
    "assets.SchedulerFilesystemAction": "d9ff8d458dfb10b355afc52858bbc65d37ab78d847a90df8ef0737d6f9d8edbc",
    "assets.RollbackAction": "131b9a72f67c4613fc5c2acb1032b1efeaa81d8143ba2049fce1992be97390c4",
    "assets.DestinationAttestationAction": "ccc3668decc2da3cff2c5510cffb4c9dd6bd42db5b4ffb2deeec14d4c7dc5d70",
    "assets.FileIdentity": "325d334a0caf93a1bb9e7fe534222f2e6747cdc57ac8da83b417678bbda4284f",
    "assets.LiveHelperFact": "36c0602273ce64058dff08f915018f38efea256fbb678a5478441d79a305fdb7",
    "assets.SchedulerInstallAuthorization": "247724e5dc9fcb87fd12887fa6f9e7fe00d65c4e34155f1c00c2ea938e814680",
    "assets.FileSnapshot": "1e751060e17a5fb0f4bc16d3774ede9581bc8f812e61b0b878bd1001d28140f8",
    "assets.SchedulerFilesystemResult": "4c7b6d4b49e3efc41107cf9c341d0cc7bb8bda6080b363f0259ae8acc6a2a46c",
    "assets.AssetFilesystemResult": "d3b0a551e40fef56cf67791c0f0b90b9195023e0dc2510b4478564dbc866eeb7",
    "assets.RollbackResult": "c672715d9c70813c179a74055d6378b7ae539ab8d3bc3d7f453702f6b148c052",
    "assets.DestinationAttestationResult": "4259e3ad8887ff227d14aca9843a800017b639c894604c2c74d08da66cb74517",
    "assets._LiveHelperSnapshot": "2c460298847ce006bbfd503ed0462358948291e6b49b6796d4c951fa063d8196",
    "assets._DiskHelperEvidence": "1dd0679aa050af27c34fee17c2d66f1deffb75652fb32ce301754bb8c16ebede",
    "assets._FilesystemContractError": "bbd39f13fea484f089702c226875898a6700498ad73acbeb9d9d7f3bd9a057fb",
    "assets._stable_entity_id_map": "31b648814c1cc8858d151e5f6870196374cbff7fb15867aa7319887ee5bf0231",
    "assets._migrate_legacy_entity_ids": "de0b5d49f25ed253091179bf2f50cf11479667f1b376b44e8960c598ca857140",
    "assets._sha256": "1bac43b621d5b16f48868469dd62fcc1c2050bd1419fc513aa306c1aeac3374a",
    "assets._identity_from_stat": "380304c31497af1b285a505568dcddd9508037fdc08628f3dbdd0a64b7494c42",
    "assets._same_inode": "535b0b8eeaf492509af52db965feddc8ec5a86742cb12222649e5eef2544960d",
    "assets._read_fd_bytes": "d0efddadb19dc375a05d903db4ec3dcd4e5799bfc429e2d1ba1101baf7768202",
    "assets._open_read_flags": "d54eef5e1031651eb4f97f8ed43f3aaafe5e5d70b9cebe450473bd410408113a",
    "assets._capture_regular_file": "1061037ba7c57f09bee5b538fcd03cefb98efd1c3dc0ffe76c4adaa3f8aa43bb",
    "assets._bounded_path_snapshot": "cdcc9e070b7309d3de604ba1c698586c79b1654b9bbdcd21a23cb28776bd445f",
    "assets._unlink_owned_path": "fe3029303bdc9ae9eefdfe7006c5ccd8bb3c832d8ce9e76f8fe6d707e820e7ba",
    "assets._write_all_fd": "44890b6479641c1e8588cca5cf810d42807b98a0cc85aaf5b1899a7a72f71c59",
    "assets._create_verified_temp": "a9ea003c421aca92fdb84eb381c3181c2ab5fc2b3d1b0a62d1b508c43267deff",
    "assets._verify_temp_for_publish": "7b89399b099245fdbb1a49426bae370345359986751408774f43d85fae1c51ba",
    "assets._fsync_directory": "b46f9299b2b748229853a031e95bbb55cee662d243354eeae38494aead6fd828",
    "assets._atomic_copy": "69e7b07f7179cb10e58b751e5c9716c81f0b8345bdaa712dab5624069e4e1558",
    "assets._verify_fixed_backup": "95f1c792cfe905748bdd5abd65107c0a81c6f829693f766b18faacc248564690",
    "assets._ensure_fixed_backup": "dd2c5c558fa8dfdb577362ed29cf1a9a994756e6ecb1baff67b6d40b1a55772c",
    "assets._storage_helper_ids": "036a0e36b7f8a85c9d0552da74d130e6dc49738f7889b18e6015e72e4bf7fefb",
    "assets._read_disk_helper_evidence": "ef6cc382822e358ce91c6d88c039a271ab9786031db4a4edf9f3f1bf85ba1d4b",
    "assets._unverifiable_live_snapshot": "da3321ab2c0c5b2e38d2b4c0e2ac5255c44b1ac061fbdd8984a81df1b68aa1fb",
    "assets._capture_live_helper_snapshot": "fbe6dafca2cdc9f166b26ab4f8ccf54f03361380ad785fadd541caf2bd433965",
    "assets._build_scheduler_authorization": "4ec2b8a6db84a26febd1194c8ceba2fa39e88d18c0f172619a753be957bcfab3",
    "assets._async_scheduler_authorization": "d69a982c3277717c905b6cde7d3144b79a68691548a9a96e63953c743ab8ccbb",
    "assets._atomic_write_json": "e2d9b889b54440b334b7cf49c6b45608c11906f9e1864ce434490fd48c8abd53",
    "assets._backup_storage_once": "ee86c9c948350c0beb7472e47740970b173884d0a23cbe72b02978b3b4a7c692",
    "assets._replace_entities_cards": "1269b45ed3f33ad4c05c613b5d783f84ed9072bee3f5e829466538b6b8e42642",
    "assets._migrate_rce_load_rows": "3dcffaff1f6def40074b77820b6a8dfb2d61e3b26fc26b7c758ccd37a464aa48",
    "assets._is_hoymiles_dashboard": "1faf3f3e16e48dfa964b2ff810ae51dc082940acfa5da6a9167b1c39bca50fb0",
    "assets._async_sync_lovelace_resource": "73a6abbd8c8ca5308efe4156f07a95061a68739f6ee4205a7dab14803f4b518a",
    "assets._sync_lovelace_storage": "48aa5daf5234b57994533d5ff0836e9ff0ff38619dce2e4b43048139d51dec5c",
    "assets._migrate_inverter_image_paths": "7c0c04160889d7285c31989b6f15fcfa615375b104a150ffb8525b566e5c16b2",
    "assets._revalidate_destination": "50c0fc105c3c46074a8f398a16d45f460d34c5505971c860e479ec9ae14bd65c",
    "assets._remove_transaction_artifact": "ff70b40e1790b42f18a40dacfe1096bc1de374236bcb8512d8537c921bd5067f",
    "assets._aborted_scheduler_result": "bb27ecd462798ceb113c0439d080e993c9640d654b017de68d33b1bc24952efa",
    "assets._sync_ems_scheduler": "587396d7fb5ac88d5906a96d334264ed621293d01e87b9881466ea2b9cbfd7e3",
    "assets._rollback_scheduler": "6e1943ea85dd8e1a57cf2cdeb2c9c3f4cc463a682bc4955005d240c1eb0dbc44",
    "assets._sync_regular_asset": "0666273e715759fe5e88aee17493d238f30c8a1be42405b3bbb909a5964e74a0",
    "assets._sync_assets": "c234e26bdac983a15cf0b48e933ad0dcf70cb7d7eeb47c3c28e2e4acc3868301",
    "assets._get_install_lock": "d1625e977c0d72e69e6841eefc5a9d0146c073e5ccda30e6d9b11661b9e8027d",
    "assets._attest_scheduler_destination": "ac1c7c0478ff74b2e3ecdcfa6adf3fcd36684ce3047e43a1d3e1248f1f78c089",
    "assets._scheduler_metadata_after_transaction": "e5392d3a1de39219b9cc8b84503409c7bdf83f23b45c56f67533e5dbacde3daa",
    "assets._log_scheduler_outcome": "45e70bc47fc7ba9c48108115017a1116d64f22c27b02aa7002ee9d4af5697823",
    "assets.async_install_assets": "dce56901ecfb45304b8f418fdf23d2a419d058d59a59650fbe91c87d31377c02",
}


class ValidatorState:
    """Minimal public-compatible State fixture."""

    def __init__(self, entity_id: str, state: str, attributes: dict) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes


class ValidatorStateMachine:
    """Minimal exact StateMachine.get surface."""

    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def get(self, entity_id: str):
        return self.values.get(entity_id)

    def put(self, state: ValidatorState) -> None:
        self.values[state.entity_id] = state


class ValidatorRegistryEntry:
    """Minimal public RegistryEntry identity."""

    def __init__(self, entity_id: str, platform: str, unique_id: str) -> None:
        self.entity_id = entity_id
        self.platform = platform
        self.unique_id = unique_id


class ValidatorEntityRegistry:
    """Public exact and identity-index lookup surface."""

    def __init__(self) -> None:
        self.entries: dict[str, ValidatorRegistryEntry] = {}

    def async_get(self, entity_id: str):
        return self.entries.get(entity_id)

    def async_get_entity_id(
        self,
        domain: str,
        platform: str,
        unique_id: str,
    ) -> str | None:
        for entry in self.entries.values():
            if (
                entry.entity_id.partition(".")[0] == domain
                and entry.platform == platform
                and entry.unique_id == unique_id
            ):
                return entry.entity_id
        return None


class ValidatorStore:
    """Per-Hass managed metadata Store fixture."""

    def __init__(self, hass, *_args, **_kwargs) -> None:
        self.hass = hass

    async def async_load(self):
        self.hass.store_load_calls += 1
        return json.loads(json.dumps(self.hass.store_payload))

    async def async_save(self, payload: dict) -> None:
        self.hass.store_save_calls += 1
        if self.hass.metadata_failures:
            self.hass.metadata_failures -= 1
            raise OSError("injected validator metadata failure")
        self.hass.store_payload = json.loads(json.dumps(payload))


class ValidatorHass:
    """Only the public HA surfaces needed by async_install_assets."""

    def __init__(self, config_path: Path, language: str = "pl-PL") -> None:
        self.config = types.SimpleNamespace(
            config_dir=str(config_path),
            language=language,
        )
        self.data: dict = {}
        self.states = ValidatorStateMachine()
        self.entity_registry = ValidatorEntityRegistry()
        self.store_payload: dict = {}
        self.store_load_calls = 0
        self.store_save_calls = 0
        self.metadata_failures = 0
        self.executor_before = None
        self.executor_after = None
        self.pause_executor_name: str | None = None
        self.pause_entered: asyncio.Event | None = None
        self.pause_release: asyncio.Event | None = None
        self.replace_failure_path: Path | None = None

    async def async_add_executor_job(self, function, *args):
        if self.executor_before is not None:
            self.executor_before(function.__name__, args)
        if function.__name__ == self.pause_executor_name:
            require(
                self.pause_entered is not None
                and self.pause_release is not None,
                "Executor pause events are missing",
            )
            self.pause_entered.set()
            await self.pause_release.wait()
        module = sys.modules[function.__module__]
        original_replace = module.os.replace

        def replace_with_failure(source, destination) -> None:
            if (
                self.replace_failure_path is not None
                and Path(destination) == self.replace_failure_path
            ):
                raise OSError("injected validator replace failure")
            original_replace(source, destination)

        module.os.replace = replace_with_failure
        try:
            result = function(*args)
        finally:
            module.os.replace = original_replace
        if self.executor_after is not None:
            self.executor_after(function.__name__, result)
        return result


def validator_install(assets, hass: ValidatorHass, overwrite: bool = False):
    """Invoke only the public runtime transaction."""
    return asyncio.run(
        assets.async_install_assets(
            hass,
            overwrite=overwrite,
            publish_frontend=False,
        )
    )


def validator_set_ui_collision(
    hass: ValidatorHass,
    entity_id: str = VALIDATOR_HELPER_IDS[0],
) -> None:
    """Publish an exact editable helper before its delayed Store save."""
    domain, object_id = entity_id.split(".", 1)
    hass.states.put(ValidatorState(entity_id, "off", {"editable": True}))
    hass.entity_registry.entries[entity_id] = ValidatorRegistryEntry(
        entity_id,
        domain,
        object_id,
    )


def validator_scheduler_path(config_path: Path) -> Path:
    return config_path / VALIDATOR_SCHEDULER_RELATIVE


def validator_file_identity(path: Path) -> tuple[int, int, int, int, int, int]:
    info = path.lstat()
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_mode,
        info.st_nlink,
    )


def validator_metadata_hash(hass: ValidatorHass) -> str | None:
    values = hass.store_payload.get("assets", {})
    if not isinstance(values, dict):
        return None
    value = values.get(VALIDATOR_SCHEDULER_RELATIVE)
    return value if isinstance(value, str) else None


def require(condition: bool, message: str) -> None:
    """Raise a readable release validation error."""
    if not condition:
        raise RuntimeError(message)


def _git_path_set(*args: str) -> set[str]:
    output = subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8"
    )
    return {line.strip().replace("\\", "/") for line in output.splitlines() if line.strip()}


def _git_blob_sha256(reference: str, relative_path: str) -> str:
    """Hash exact historical bytes without substituting the current worktree."""

    content = subprocess.check_output(
        ["git", "show", f"{reference}:{relative_path}"],
        cwd=ROOT,
    )
    return hashlib.sha256(content).hexdigest()


def validate_historical_rev28_gate() -> None:
    """Validate the frozen REV28 fixture independently of current AP-1."""

    historical_branch_paths = _git_path_set(
        "diff",
        "--name-only",
        f"{VALIDATOR_HISTORICAL_REF}...{REV28_HISTORICAL_COMMIT}",
    )
    require(
        historical_branch_paths == SUPERVISOR_BRANCH_PATHS,
        "Historical REV28 branch manifest is not exactly 32 paths: "
        f"missing={sorted(SUPERVISOR_BRANCH_PATHS - historical_branch_paths)}, "
        f"extra={sorted(historical_branch_paths - SUPERVISOR_BRANCH_PATHS)}",
    )
    protected_task_paths = frozenset(REV28_PROTECTED_TASK_HASHES)
    require(
        len(REV28_CORRECTION_PATHS) == 8
        and PHASE_2_TASK_PATHS - protected_task_paths == REV28_CORRECTION_PATHS
        and PHASE_2_TASK_PATHS - REV28_CORRECTION_PATHS == protected_task_paths,
        "Revision 28 correction manifest is not exactly the authorized 8 paths",
    )
    for relative_path, expected_hash in {
        **REV28_PROTECTED_TASK_HASHES,
        **REV28_PROTECTED_BACKEND_HASHES,
    }.items():
        actual_hash = _git_blob_sha256(
            REV28_HISTORICAL_COMMIT,
            relative_path,
        )
        require(
            actual_hash == expected_hash,
            f"Historical REV28 protected bytes changed: {relative_path}",
        )

    exact = lambda actual, expected: actual == expected
    correction_anchor = sorted(REV28_CORRECTION_PATHS)[0]
    phase_anchor = sorted(PHASE_2_TASK_PATHS)[0]
    branch_anchor = sorted(SUPERVISOR_BRANCH_PATHS)[0]
    require(
        not exact(
            REV28_CORRECTION_PATHS - {correction_anchor},
            REV28_CORRECTION_PATHS,
        ),
        "Correction 7-path count self-test did not fail",
    )
    require(
        not exact(
            REV28_CORRECTION_PATHS | {"__unexpected_correction_path__"},
            REV28_CORRECTION_PATHS,
        ),
        "Correction 9-path count self-test did not fail",
    )
    require(
        not exact(PHASE_2_TASK_PATHS - {phase_anchor}, PHASE_2_TASK_PATHS),
        "Phase 2 13-path count self-test did not fail",
    )
    require(
        not exact(PHASE_2_TASK_PATHS | {"__unexpected_phase2_path__"}, PHASE_2_TASK_PATHS),
        "Phase 2 15-path count self-test did not fail",
    )
    require(
        not exact(SUPERVISOR_BRANCH_PATHS - {branch_anchor}, SUPERVISOR_BRANCH_PATHS),
        "Branch 31-path count self-test did not fail",
    )
    require(
        not exact(SUPERVISOR_BRANCH_PATHS | {"__unexpected_branch_path__"}, SUPERVISOR_BRANCH_PATHS),
        "Branch 33-path count self-test did not fail",
    )


def _effective_path_set(
    committed_paths: set[str] | frozenset[str],
    overlay_paths: set[str] | frozenset[str],
) -> set[str]:
    """Combine committed and overlay paths exactly once."""

    return set(committed_paths) | set(overlay_paths)


def _current_overlay_paths() -> set[str]:
    """Return every tracked or untracked path in the current overlay."""

    return _git_path_set("diff", "--name-only", "HEAD") | _git_path_set(
        "ls-files", "--others", "--exclude-standard"
    )


def _effective_git_manifest(base: str, overlay_paths: set[str]) -> set[str]:
    """Return committed base-to-HEAD paths plus the current overlay."""

    committed_paths = _git_path_set("diff", "--name-only", f"{base}..HEAD")
    return _effective_path_set(committed_paths, overlay_paths)


def _ap1_committed_manifests_match(
    task_paths: set[str] | frozenset[str],
    branch_paths: set[str] | frozenset[str],
) -> bool:
    """Return the exact frozen committed AP-1 baseline verdict."""

    return (
        task_paths == AP1_COMMITTED_TASK_PATHS
        and branch_paths == AP1_COMMITTED_BRANCH_PATHS
    )


def _ap1e_manifests_match(
    correction_paths: set[str] | frozenset[str],
    task_paths: set[str] | frozenset[str],
    branch_paths: set[str] | frozenset[str],
) -> bool:
    """Return the exact current AP-1E candidate verdict."""

    return (
        correction_paths == AP1E_CORRECTION_PATHS
        and task_paths == AP1E_CUMULATIVE_TASK_PATHS
        and branch_paths == AP1E_BRANCH_PATHS
    )


def validate_current_ap1_manifests() -> str:
    """Validate frozen AP-1 and effective AP-1E manifests independently."""

    committed_ap1_task = _git_path_set(
        "diff",
        "--name-only",
        f"{AP1_CUMULATIVE_TASK_BASE}..{AP1E_CORRECTION_BASE}",
    )
    committed_ap1_branch = _git_path_set(
        "diff",
        "--name-only",
        f"{AP1_PUBLIC_BRANCH_BASE}..{AP1E_CORRECTION_BASE}",
    )
    require(
        _ap1_committed_manifests_match(
            committed_ap1_task,
            committed_ap1_branch,
        ),
        "Committed AP-1 baseline is not exactly 16/40 paths",
    )

    staged_paths = _git_path_set("diff", "--cached", "--name-only")
    overlay_paths = _current_overlay_paths()
    correction_paths = _effective_git_manifest(
        AP1E_CORRECTION_BASE,
        overlay_paths,
    )
    task_paths = _effective_git_manifest(
        AP1_CUMULATIVE_TASK_BASE,
        overlay_paths,
    )
    branch_paths = _effective_git_manifest(
        AP1_PUBLIC_BRANCH_BASE,
        overlay_paths,
    )
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
    ).strip()

    if not correction_paths:
        require(
            head == AP1E_CORRECTION_BASE
            and not overlay_paths
            and _ap1_committed_manifests_match(task_paths, branch_paths),
            "Clean AP-1 baseline gate differs from exact f630529 16/40 state",
        )
        gate_state = "clean_committed_ap1"
    else:
        require(
            _ap1e_manifests_match(
                correction_paths,
                task_paths,
                branch_paths,
            ),
            "Current AP-1E manifests are not exactly 4/18/41 paths: "
            f"correction_missing={sorted(AP1E_CORRECTION_PATHS - correction_paths)}, "
            f"correction_extra={sorted(correction_paths - AP1E_CORRECTION_PATHS)}, "
            f"task_missing={sorted(AP1E_CUMULATIVE_TASK_PATHS - task_paths)}, "
            f"task_extra={sorted(task_paths - AP1E_CUMULATIVE_TASK_PATHS)}, "
            f"branch_missing={sorted(AP1E_BRANCH_PATHS - branch_paths)}, "
            f"branch_extra={sorted(branch_paths - AP1E_BRANCH_PATHS)}",
        )
        gate_state = "ap1e_candidate"
    require(not staged_paths, "Current AP-1E validation requires zero staged paths")

    # Effective-manifest self-tests cover clean committed AP-1, an uncommitted
    # AP-1E overlay, and the same AP-1E paths after a future clean commit.
    require(
        _ap1_committed_manifests_match(
            _effective_path_set(AP1_COMMITTED_TASK_PATHS, set()),
            _effective_path_set(AP1_COMMITTED_BRANCH_PATHS, set()),
        ),
        "Clean committed AP-1 manifest self-test failed",
    )
    require(
        _ap1e_manifests_match(
            _effective_path_set(set(), AP1E_CORRECTION_PATHS),
            _effective_path_set(
                AP1_COMMITTED_TASK_PATHS,
                AP1E_CORRECTION_PATHS,
            ),
            _effective_path_set(
                AP1_COMMITTED_BRANCH_PATHS,
                AP1E_CORRECTION_PATHS,
            ),
        ),
        "Uncommitted AP-1E overlay manifest self-test failed",
    )
    require(
        _ap1e_manifests_match(
            _effective_path_set(AP1E_CORRECTION_PATHS, set()),
            _effective_path_set(AP1E_CUMULATIVE_TASK_PATHS, set()),
            _effective_path_set(AP1E_BRANCH_PATHS, set()),
        ),
        "Committed-clean AP-1E manifest self-test failed",
    )

    missing_init = AP1E_CORRECTION_PATHS - {
        "custom_components/hoymiles_hit_modbus/__init__.py"
    }
    missing_test = AP1E_CORRECTION_PATHS - {
        "tests/test_timeline_platform_registration.py"
    }
    missing_timeline_sensor = AP1E_CORRECTION_PATHS - {
        "custom_components/hoymiles_hit_modbus/timeline_sensor.py"
    }
    require(
        not _ap1e_manifests_match(
            missing_init,
            AP1E_CUMULATIVE_TASK_PATHS
            - {"custom_components/hoymiles_hit_modbus/__init__.py"},
            AP1E_BRANCH_PATHS,
        ),
        "Missing __init__.py correction survived the AP-1E gate",
    )
    require(
        not _ap1e_manifests_match(
            missing_test,
            AP1E_CUMULATIVE_TASK_PATHS
            - {"tests/test_timeline_platform_registration.py"},
            AP1E_BRANCH_PATHS
            - {"tests/test_timeline_platform_registration.py"},
        ),
        "Missing real HA test survived the AP-1E gate",
    )
    require(
        not _ap1e_manifests_match(
            missing_timeline_sensor,
            AP1E_CUMULATIVE_TASK_PATHS,
            AP1E_BRANCH_PATHS,
        ),
        "Missing timeline_sensor.py correction survived the AP-1E gate",
    )
    require(
        not _ap1e_manifests_match(
            AP1E_CORRECTION_PATHS | {"__unexpected_ap1e_path__"},
            AP1E_CUMULATIVE_TASK_PATHS | {"__unexpected_ap1e_path__"},
            AP1E_BRANCH_PATHS | {"__unexpected_ap1e_path__"},
        ),
        "Extra fifth AP-1E correction path survived the gate",
    )
    task_anchor = sorted(AP1E_CUMULATIVE_TASK_PATHS)[0]
    require(
        not _ap1e_manifests_match(
            AP1E_CORRECTION_PATHS,
            AP1E_CUMULATIVE_TASK_PATHS - {task_anchor},
            AP1E_BRANCH_PATHS,
        ),
        "AP-1E task 17-path self-test did not fail",
    )
    require(
        not _ap1e_manifests_match(
            AP1E_CORRECTION_PATHS,
            AP1E_CUMULATIVE_TASK_PATHS | {"__unexpected_ap1e_task_path__"},
            AP1E_BRANCH_PATHS,
        ),
        "AP-1E task 19-path self-test did not fail",
    )
    branch_anchor = "tests/test_timeline_platform_registration.py"
    require(
        not _ap1e_manifests_match(
            AP1E_CORRECTION_PATHS,
            AP1E_CUMULATIVE_TASK_PATHS,
            AP1E_BRANCH_PATHS - {branch_anchor},
        ),
        "AP-1E branch 40-path self-test did not fail",
    )
    require(
        not _ap1e_manifests_match(
            AP1E_CORRECTION_PATHS,
            AP1E_CUMULATIVE_TASK_PATHS,
            AP1E_BRANCH_PATHS | {"__unexpected_ap1e_branch_path__"},
        ),
        "AP-1E branch 42-path self-test did not fail",
    )
    require(
        not _ap1e_manifests_match(
            PHASE_2_TASK_PATHS,
            PHASE_2_TASK_PATHS,
            SUPERVISOR_BRANCH_PATHS,
        ),
        "Historical REV28 PASS was accepted as current AP-1E validation",
    )
    return gate_state


def _ap1e_r2_identity_contracts(
    timeline_source: str,
    init_source: str,
) -> bool:
    """Return whether current sources keep timeline identity pre-registration."""

    try:
        normalizer_source = init_source.split(
            "def _async_prepare_timeline_entity_registry(", 1
        )[1].split("\n\nasync def ", 1)[0]
        setup_source = init_source.split("async def async_setup_entry(", 1)[1].split(
            "\n\nasync def async_unload_entry", 1
        )[0]
        reconcile_source = init_source.split(
            "def _async_reconcile_entity_registry(", 1
        )[1].split("\n\ndef _async_prepare_timeline_entity_registry(", 1)[0]
    except (IndexError, ValueError):
        return False

    prepare_call = "_async_prepare_timeline_entity_registry(hass, entry)"
    forward_call = "await hass.config_entries.async_forward_entry_setups("
    reconcile_call = "_async_reconcile_entity_registry("
    forbidden_normalizer = (
        ".storage",
        "deleted_entities.clear",
        "deleted_entities.values",
        "async_reload",
        "async_call_later",
        "async_track_time_interval",
        "sleep(",
    )
    return (
        'TIMELINE_POLICY_IDS = ("rce", "tariff")' in timeline_source
        and "from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN"
        in timeline_source
        and 'return f"{SENSOR_DOMAIN}.hoymiles_hit_{policy_id}_automation_plan_timeline"'
        in timeline_source
        and "self.entity_id = timeline_entity_id(policy_id)" in timeline_source
        and "self._attr_unique_id = timeline_unique_id(entry.entry_id, policy_id)"
        in timeline_source
        and "def suggested_object_id(self)" not in timeline_source
        and "async_update_entity" not in timeline_source
        and prepare_call in setup_source
        and forward_call in setup_source
        and reconcile_call in setup_source
        and setup_source.index(prepare_call) < setup_source.index(forward_call)
        and setup_source.index(forward_call) < setup_source.index(reconcile_call)
        and "for policy_id in TIMELINE_POLICY_IDS:" in normalizer_source
        and 'deleted_key = ("sensor", DOMAIN, unique_id)' in normalizer_source
        and "deleted_entities.get(deleted_key)" in normalizer_source
        and "deleted_entry.config_entry_id != entry.entry_id" in normalizer_source
        and "deleted_entry.__replace__(entity_id=desired_entity_id)"
        in normalizer_source
        and "entity_registry.async_schedule_save()" in normalizer_source
        and not any(token in normalizer_source for token in forbidden_normalizer)
        and reconcile_source.count(
            'active_translation_keys.add("rce_automation_plan_timeline")'
        )
        == 1
        and reconcile_source.count(
            'active_translation_keys.add("tariff_automation_plan_timeline")'
        )
        == 1
    )


def validate_current_ap1_contract() -> None:
    """Run current AP-1 contracts and inspect the actual current adapters."""

    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    for test_name in (
        "test_automation_plan_timeline.py",
        "test_optimizer_startup_contract.py",
        "test_optimizer_executor_contract.py",
    ):
        completed = subprocess.run(
            [sys.executable, "-B", str(ROOT / "tools" / test_name)],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )
        require(
            completed.returncode == 0,
            f"Current AP-1 contract failed: {test_name}\n"
            f"{completed.stdout}{completed.stderr}",
        )

    timeline_source = (COMPONENT / "timeline_sensor.py").read_text(
        encoding="utf-8"
    )
    common_source = (COMPONENT / "automation_plan_timeline.py").read_text(
        encoding="utf-8"
    )
    sensor_source = (COMPONENT / "sensor.py").read_text(encoding="utf-8")
    init_source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    registration_test_path = ROOT / "tests" / "test_timeline_platform_registration.py"
    require(
        registration_test_path.is_file(),
        "Current AP-1E real Home Assistant registration test is missing",
    )
    registration_test_source = registration_test_path.read_text(encoding="utf-8")
    require(
        "_unrecorded_attributes = frozenset({MATCH_ALL})" in timeline_source
        and "RestoreEntity" not in timeline_source
        and "_attr_should_poll = False" in timeline_source,
        "Current AP-1 Recorder/Restore/polling contract differs",
    )
    require(
        "MAX_POINTS = 192" in common_source
        and "MAX_SERIALIZED_BYTES = 262_144" in common_source
        and "MAX_SOURCES = 16" in common_source
        and 'PLAN_REVISION_SCOPE = "runtime"' in common_source
        and 'ACTIVE_SCOPE = "publication_snapshot"' in common_source,
        "Current AP-1 schema or bounded-limit contract differs",
    )
    require(
        'policy_id="rce"' in sensor_source
        and 'policy_id="tariff"' in sensor_source
        and _ap1e_r2_identity_contracts(timeline_source, init_source),
        "Current AP-1 timeline IDs or unique-ID construction differs",
    )
    require(
        not _ap1e_r2_identity_contracts(
            timeline_source.replace(
                "self.entity_id = timeline_entity_id(policy_id)",
                "# suggested_object_id-only mutation",
                1,
            ),
            init_source,
        ),
        "Suggested-object-ID-only mutation survived the AP-1E-R2 gate",
    )
    require(
        not _ap1e_r2_identity_contracts(
            timeline_source,
            init_source.replace(
                "for policy_id in TIMELINE_POLICY_IDS:",
                "for deleted_entry in entity_registry.deleted_entities.values():",
                1,
            ),
        ),
        "Broad deleted-row normalization survived the AP-1E-R2 gate",
    )
    require(
        not _ap1e_r2_identity_contracts(
            timeline_source + "\nentity_registry.async_update_entity(timeline.entity_id)\n",
            init_source,
        ),
        "Post-add timeline rename survived the AP-1E-R2 gate",
    )
    forbidden = (
        "async_track_time_interval",
        "async_call_later",
        "services.async_call",
        "modbus.write",
        "owner_acquire",
        "grant_execution",
        "handover_execution",
    )
    require(
        not any(token in timeline_source for token in forbidden),
        "Current AP-1 gained polling, timers or physical authority",
    )
    reconcile_source = init_source.split(
        "def _async_reconcile_entity_registry(", 1
    )[1].split("\n\nasync def ", 1)[0]
    require(
        reconcile_source.count(
            'active_translation_keys.add("rce_automation_plan_timeline")'
        )
        == 1
        and reconcile_source.count(
            'active_translation_keys.add("tariff_automation_plan_timeline")'
        )
        == 1
        and reconcile_source.index(
            'active_translation_keys.add("rce_automation_plan_timeline")'
        )
        < reconcile_source.index("for registry_entry in")
        and reconcile_source.index(
            'active_translation_keys.add("tariff_automation_plan_timeline")'
        )
        < reconcile_source.index("for registry_entry in")
        and not any(
            token in reconcile_source
            for token in ("startswith(", "endswith(", "re.search(", "re.match(")
        ),
        "Current AP-1E exact timeline reconciliation keys differ",
    )
    for key in (
        "rce_automation_plan_timeline",
        "tariff_automation_plan_timeline",
    ):
        require(
            not _ap1e_r2_identity_contracts(
                timeline_source,
                init_source.replace(
                    f'    active_translation_keys.add("{key}")\n',
                    "",
                    1,
                ),
            ),
            f"Missing {key} survived the AP-1E-R2 static gate",
        )
    real_test_tokens = (
        'HA_VERSION == "2026.8.2"',
        "ConfigEntry(",
        "EntityPlatform(",
        "sensor_platform.async_setup_entry",
        "entity_registry.async_get_or_create",
        "device_registry.async_get_or_create",
        "hass.states.get",
        "_async_prepare_timeline_entity_registry",
        "_async_reconcile_entity_registry",
        "platform.async_reset",
        "pref_disable_new_entities=disable_new_entities",
        "RegistryEntryDisabler.INTEGRATION",
        "DeletedRegistryEntry",
        "TimelineIdentityCollisionError",
        "EVENT_ENTITY_REGISTRY_UPDATED",
        "reconciliation_changed_active_timeline",
    )
    require(
        all(token in registration_test_source for token in real_test_tokens)
        and "class FakeEntityPlatform" not in registration_test_source
        and "class StubEntityPlatform" not in registration_test_source,
        "Current AP-1E registration test does not freeze the real HA lifecycle",
    )


def _write_historical_paths(
    destination: Path,
    reference: str,
    paths: set[str] | frozenset[str],
) -> None:
    """Overlay exact tracked blobs from one frozen historical reference."""

    for relative_path in paths:
        target = destination / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            subprocess.check_output(
                ["git", "show", f"{reference}:{relative_path}"],
                cwd=ROOT,
            )
        )


def build_historical_rev28_frontend_fixture(destination: Path) -> None:
    """Build the original 14/32 REV28 validator fixture outside the repo."""

    archive = subprocess.check_output(
        ["git", "archive", "--format=tar", VALIDATOR_HISTORICAL_REF],
        cwd=ROOT,
    )
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        bundle.extractall(destination, filter="fully_trusted")

    quiet = {"cwd": destination, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    subprocess.check_call(["git", "init"], **quiet)
    subprocess.check_call(["git", "config", "user.name", "AP-1 validator"], **quiet)
    subprocess.check_call(
        ["git", "config", "user.email", "validator@example.invalid"],
        **quiet,
    )
    subprocess.check_call(["git", "add", "--all"], **quiet)
    subprocess.check_call(["git", "commit", "-m", "v1.5.7 fixture"], **quiet)
    subprocess.check_call(["git", "tag", VALIDATOR_HISTORICAL_REF], **quiet)

    historical_base_paths = _git_path_set(
        "diff",
        "--name-only",
        f"{VALIDATOR_HISTORICAL_REF}...{PROTECTED_ASSETS_AST_BASE}",
    )
    _write_historical_paths(
        destination,
        PROTECTED_ASSETS_AST_BASE,
        historical_base_paths,
    )
    subprocess.check_call(["git", "add", "--all"], **quiet)
    subprocess.check_call(["git", "commit", "-m", "Supervisor fixture"], **quiet)

    _write_historical_paths(
        destination,
        REV28_HISTORICAL_COMMIT,
        PHASE_2_TASK_PATHS,
    )


def _require_protected_assets_ast(module: ast.Module) -> None:
    """Require the exact c65e8b73 function/class AST declaration set."""
    actual_nodes = {
        f"assets.{node.name}": node
        for node in module.body
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        )
    }
    require(
        set(actual_nodes) == set(PROTECTED_ASSETS_AST_SHA256),
        "Protected assets.py function/class declaration set changed",
    )
    for qualified_name, expected_hash in PROTECTED_ASSETS_AST_SHA256.items():
        node = actual_nodes[qualified_name]
        canonical = ast.dump(node, annotate_fields=True, include_attributes=False)
        actual_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        require(
            actual_hash == expected_hash,
            f"Protected scheduler-delivery AST changed: {qualified_name}",
        )


def validate_protected_assets_ast(assets_source: str) -> None:
    """Protect all assets.py declarations and prove the S29 detector fires."""
    require(
        PROTECTED_ASSETS_AST_BASE
        == "c65e8b73096cb64ff2d21b6e2b05602f71a4a2af",
        "Protected assets.py AST base is not the reviewed exact commit",
    )
    _require_protected_assets_ast(ast.parse(assets_source))

    # S29: a semantic edit to final destination attestation must be rejected
    # by the same literal AST oracle used for the real current worktree.
    mutation = ast.parse(assets_source)
    target = next(
        (
            node
            for node in mutation.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_attest_scheduler_destination"
        ),
        None,
    )
    require(target is not None, "S29 mutation target is missing")
    target.body.append(ast.Pass())
    try:
        _require_protected_assets_ast(mutation)
    except RuntimeError:
        pass
    else:
        raise RuntimeError("S29 protected scheduler-delivery mutation survived")


def load_json(path: Path) -> dict | list:
    """Load and validate UTF-8 JSON."""
    with path.open(encoding="utf-8") as json_file:
        return json.load(json_file)


def iter_mappings(value):
    """Yield every mapping in a nested dashboard payload."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_mappings(child)


def dashboard_structure(value, parent_key: str = ""):
    """Return a locale-neutral dashboard structure signature."""
    if isinstance(value, dict):
        return {
            key: dashboard_structure(child, key)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [dashboard_structure(child, parent_key) for child in value]
    if isinstance(value, str) and (
        parent_key in {"type", "entity", "action", "service"}
        or parent_key.endswith("_entity")
        or re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", value)
    ):
        return value
    return type(value).__name__


def load_localization_module():
    """Load the standalone localization module without Home Assistant."""
    path = COMPONENT / "localization.py"
    spec = importlib.util.spec_from_file_location("hoymiles_localization", path)
    require(spec is not None and spec.loader is not None, "Cannot load localization")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_assets_module():
    """Load assets.py with only documented public HA surfaces stubbed."""
    homeassistant = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    lovelace = types.ModuleType("homeassistant.components.lovelace")
    lovelace_const = types.ModuleType("homeassistant.components.lovelace.const")
    ha_const = types.ModuleType("homeassistant.const")
    core = types.ModuleType("homeassistant.core")
    helpers = types.ModuleType("homeassistant.helpers")
    storage = types.ModuleType("homeassistant.helpers.storage")
    entity_registry = types.ModuleType(
        "homeassistant.helpers.entity_registry"
    )
    core.HomeAssistant = object
    lovelace_const.CONF_RESOURCE_TYPE_WS = "res_type"
    lovelace_const.LOVELACE_DATA = "lovelace"
    lovelace_const.MODE_STORAGE = "storage"
    ha_const.ATTR_EDITABLE = "editable"
    ha_const.CONF_ID = "id"
    ha_const.CONF_TYPE = "type"
    ha_const.CONF_URL = "url"
    storage.Store = ValidatorStore
    entity_registry.async_get = lambda hass: hass.entity_registry
    homeassistant.components = components
    components.lovelace = lovelace
    lovelace.const = lovelace_const
    homeassistant.const = ha_const
    homeassistant.core = core
    homeassistant.helpers = helpers
    helpers.storage = storage
    helpers.entity_registry = entity_registry
    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.components"] = components
    sys.modules["homeassistant.components.lovelace"] = lovelace
    sys.modules["homeassistant.components.lovelace.const"] = lovelace_const
    sys.modules["homeassistant.const"] = ha_const
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.storage"] = storage
    sys.modules["homeassistant.helpers.entity_registry"] = entity_registry

    custom_components = types.ModuleType("custom_components")
    package = types.ModuleType("custom_components.hoymiles_hit_modbus")
    package.__path__ = [str(COMPONENT)]
    sys.modules.setdefault("custom_components", custom_components)
    sys.modules.setdefault("custom_components.hoymiles_hit_modbus", package)

    const_module = types.ModuleType("custom_components.hoymiles_hit_modbus.const")
    const_module.DOMAIN = "hoymiles_hit_modbus"
    const_module.VERSION = json.loads(
        (COMPONENT / "manifest.json").read_text(encoding="utf-8")
    )["version"]
    const_module.EMS_PACKAGE_VERSION = VALIDATOR_PACKAGE_MARKER
    const_module.EMS_PACKAGE_VERSION_ENTITY = VALIDATOR_MARKER_ENTITY
    sys.modules[const_module.__name__] = const_module

    path = COMPONENT / "assets.py"
    spec = importlib.util.spec_from_file_location(
        "custom_components.hoymiles_hit_modbus.assets",
        path,
    )
    require(spec is not None and spec.loader is not None, "Cannot load assets")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def validate_fresh_asset_install() -> None:
    """Exercise managed assets only through async_install_assets."""
    assets = load_assets_module()
    assets_source = (COMPONENT / "assets.py").read_text(encoding="utf-8")
    module = ast.parse(assets_source)
    symbols = {
        node.name
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    require("_copy_assets" not in symbols, "Removed _copy_assets symbol returned")
    validator_module = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    validator_calls = {
        (node.func.value.id, node.func.attr)
        for node in ast.walk(validator_module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
    }
    require(
        ("assets", "_copy_assets") not in validator_calls,
        "Validator calls removed path",
    )
    require(
        ("assets", "_sync_assets") not in validator_calls,
        "Validator bypasses the actual async state machine",
    )
    require(
        VALIDATOR_STORE_CONTRACTS
        == {
            "input_select": (1, frozenset({1, 2})),
            "input_boolean": (1, frozenset({1})),
        },
        "Literal Store contract oracle differs",
    )
    require(
        tuple(VALIDATOR_HELPER_IDS)
        == (
            "input_select.hoymiles_ems_supervisor_mode",
            "input_select.hoymiles_ems_supervisor_profile",
            "input_boolean.hoymiles_ems_supervisor_allow_rce",
            "input_boolean.hoymiles_ems_supervisor_allow_tariff",
            "input_boolean.hoymiles_ems_supervisor_allow_rcm",
        ),
        "Literal canonical helper oracle differs",
    )

    with tempfile.TemporaryDirectory(prefix="hoymiles_hacs_install_") as tmp:
        config_path = Path(tmp)
        dashboard_path = config_path / "dashboard_hoymiles.yaml"
        package_path = validator_scheduler_path(config_path)
        hass = ValidatorHass(config_path)
        results: list = []

        def capture(name: str, result) -> None:
            if name == "_sync_assets":
                results.append(result)

        hass.executor_after = capture
        polish_written = validator_install(assets, hass)
        require(
            len(polish_written) == 7,
            "Fresh Polish async installation did not write seven assets",
        )
        require(
            dashboard_path.read_bytes()
            == (RESOURCES / "dashboard_hoymiles_pl.yaml").read_bytes(),
            "Fresh async installation copied wrong Polish dashboard",
        )
        require(
            package_path.read_bytes()
            == (
                RESOURCES
                / "home_assistant"
                / "pl"
                / "hoymiles_ems_scheduler.yaml"
            ).read_bytes(),
            "Fresh async installation copied wrong scheduler",
        )
        require(
            results[0].scheduler.action.value
            == VALIDATOR_TRANSITIONS["fresh"],
            "Fresh literal transition differs",
        )
        require(
            validator_metadata_hash(hass) == VALIDATOR_NEW_HASHES["pl"],
            "Fresh scheduler metadata differs",
        )
        for filename in assets.LOCAL_FRONTEND_ASSETS:
            require(
                (config_path / "www" / filename).read_bytes()
                == (RESOURCES / "www" / filename).read_bytes(),
                f"Fresh async installation copied wrong /local asset: {filename}",
            )
        inode = package_path.stat().st_ino
        saves = hass.store_save_calls
        results.clear()
        second = validator_install(assets, hass)
        require(second == [], "Second async run is not idempotent")
        require(package_path.stat().st_ino == inode, "Second async run replaced scheduler")
        require(hass.store_save_calls == saves, "Second async run churned Store")
        require(
            results[0].scheduler.action.value
            == VALIDATOR_TRANSITIONS["current"],
            "Current literal transition differs",
        )

    for language in ("pl", "en"):
        with tempfile.TemporaryDirectory(prefix=f"hoymiles_historical_{language}_") as tmp:
            config_path = Path(tmp)
            package_path = validator_scheduler_path(config_path)
            package_path.parent.mkdir(parents=True)
            relative = (
                "custom_components/hoymiles_hit_modbus/resources/"
                f"home_assistant/{language}/hoymiles_ems_scheduler.yaml"
            )
            historical = subprocess.check_output(
                ["git", "show", f"{VALIDATOR_HISTORICAL_REF}:{relative}"],
                cwd=ROOT,
            )
            require(
                hashlib.sha256(historical).hexdigest()
                == VALIDATOR_HISTORICAL_HASHES[language],
                f"Literal historical {language} hash differs",
            )
            package_path.write_bytes(historical)
            hass = ValidatorHass(
                config_path,
                "pl-PL" if language == "pl" else "en-GB",
            )
            written = validator_install(assets, hass)
            require(package_path in written, f"Historical {language} did not update")
            require(
                package_path.with_name(
                    package_path.name + VALIDATOR_BACKUP_SUFFIX
                ).read_bytes()
                == historical,
                f"Historical {language} backup differs",
            )
            require(
                validator_metadata_hash(hass) == VALIDATOR_NEW_HASHES[language],
                f"Historical {language} metadata differs",
            )

    with tempfile.TemporaryDirectory(prefix="hoymiles_final_attestation_foreign_") as tmp:
        config_path = Path(tmp)
        package_path = validator_scheduler_path(config_path)
        package_path.parent.mkdir(parents=True)
        historical = subprocess.check_output(
            [
                "git",
                "show",
                f"{VALIDATOR_HISTORICAL_REF}:custom_components/"
                "hoymiles_hit_modbus/resources/"
                "home_assistant/pl/hoymiles_ems_scheduler.yaml",
            ],
            cwd=ROOT,
        )
        old_hash = hashlib.sha256(historical).hexdigest()
        package_path.write_bytes(historical)
        backup = package_path.with_name(
            package_path.name + VALIDATOR_BACKUP_SUFFIX
        )
        foreign = b"validator foreign destination after filesystem transaction\n"
        hass = ValidatorHass(config_path)
        hass.store_payload = {
            "integration_version": "1.5.7",
            "assets": {VALIDATOR_SCHEDULER_RELATIVE: old_hash},
        }
        observed: dict = {}

        def install_foreign_after_sync(name: str, result) -> None:
            if name == "_sync_assets":
                observed["backup"] = (
                    validator_file_identity(backup),
                    backup.read_bytes(),
                )
                winner = package_path.parent / "validator-foreign-winner"
                winner.write_bytes(foreign)
                winner.replace(package_path)
                observed["foreign_identity"] = validator_file_identity(package_path)
            elif name == "_attest_scheduler_destination":
                observed["attestation"] = result

        hass.executor_after = install_foreign_after_sync
        written = validator_install(assets, hass)
        require(
            package_path not in written,
            "Final destination mismatch reported scheduler write",
        )
        require(
            package_path.read_bytes() == foreign,
            "Final destination mismatch overwrote foreign bytes",
        )
        require(
            validator_file_identity(package_path) == observed["foreign_identity"],
            "Final destination mismatch changed foreign identity",
        )
        require(
            validator_metadata_hash(hass) == old_hash,
            "Final destination mismatch advanced scheduler metadata",
        )
        require(
            (validator_file_identity(backup), backup.read_bytes())
            == observed["backup"],
            "Final destination mismatch changed fixed backup",
        )
        require(
            observed["attestation"].action.value
            == VALIDATOR_TRANSITIONS["destination_changed"],
            "Final destination mismatch category differs",
        )
        require(
            not list(
                package_path.parent.glob(
                    f".{package_path.name}.hoymiles_hit_modbus.rollback.*.tmp"
                )
            ),
            "Final destination mismatch left rollback artifact",
        )

    with tempfile.TemporaryDirectory(prefix="hoymiles_final_attestation_inode_") as tmp:
        config_path = Path(tmp)
        package_path = validator_scheduler_path(config_path)
        package_path.parent.mkdir(parents=True)
        historical = subprocess.check_output(
            [
                "git",
                "show",
                f"{VALIDATOR_HISTORICAL_REF}:custom_components/"
                "hoymiles_hit_modbus/resources/"
                "home_assistant/pl/hoymiles_ems_scheduler.yaml",
            ],
            cwd=ROOT,
        )
        old_hash = hashlib.sha256(historical).hexdigest()
        package_path.write_bytes(historical)
        hass = ValidatorHass(config_path)
        hass.store_payload = {
            "integration_version": "1.5.7",
            "assets": {VALIDATOR_SCHEDULER_RELATIVE: old_hash},
        }
        observed = {}

        def replace_with_same_hash(name: str, result) -> None:
            if name == "_sync_assets":
                observed["installed_identity"] = result.scheduler.after.identity
                winner = package_path.parent / "validator-same-hash-new-inode"
                winner.write_bytes(
                    (
                        RESOURCES
                        / "home_assistant"
                        / "pl"
                        / "hoymiles_ems_scheduler.yaml"
                    ).read_bytes()
                )
                winner.replace(package_path)
                observed["winner_identity"] = validator_file_identity(package_path)
            elif name == "_attest_scheduler_destination":
                observed["attestation"] = result

        hass.executor_after = replace_with_same_hash
        written = validator_install(assets, hass)
        require(
            package_path not in written,
            "Hash-only final attestation reported scheduler write",
        )
        require(
            hashlib.sha256(package_path.read_bytes()).hexdigest()
            == VALIDATOR_NEW_HASHES["pl"],
            "Same-hash replacement bytes differ",
        )
        require(
            validator_file_identity(package_path) == observed["winner_identity"],
            "Same-hash replacement identity changed after attestation",
        )
        require(
            observed["attestation"].resulting_snapshot.identity
            != observed["installed_identity"],
            "Same-hash replacement did not exercise identity mismatch",
        )
        require(
            observed["attestation"].action.value
            == VALIDATOR_TRANSITIONS["destination_changed"],
            "Hash-only final attestation accepted a different inode",
        )
        require(
            validator_metadata_hash(hass) == old_hash,
            "Same-hash identity mismatch advanced scheduler metadata",
        )

    with tempfile.TemporaryDirectory(prefix="hoymiles_dashboard_managed_") as tmp:
        config_path = Path(tmp)
        dashboard_path = config_path / "dashboard_hoymiles.yaml"
        package_path = validator_scheduler_path(config_path)
        package_path.parent.mkdir(parents=True)
        package_path.write_bytes(
            (
                RESOURCES
                / "home_assistant"
                / "pl"
                / "hoymiles_ems_scheduler.yaml"
            ).read_bytes()
        )
        old_dashboard = b"title: previous managed release\n"
        dashboard_path.write_bytes(old_dashboard)
        old_hash = hashlib.sha256(old_dashboard).hexdigest()
        hass = ValidatorHass(config_path)
        hass.store_payload = {
            "integration_version": "1.5.7",
            "assets": {
                "dashboard_hoymiles.yaml": old_hash,
                VALIDATOR_SCHEDULER_RELATIVE: VALIDATOR_NEW_HASHES["pl"],
            },
        }
        written = validator_install(assets, hass)
        require(dashboard_path in written, "Managed dashboard was not upgraded")
        require(
            dashboard_path.read_bytes()
            == (RESOURCES / "dashboard_hoymiles_pl.yaml").read_bytes(),
            "Managed dashboard async update bytes differ",
        )
        custom = b"title: user customization\n"
        dashboard_path.write_bytes(custom)
        stale = dict(hass.store_payload)
        written = validator_install(assets, hass)
        require(dashboard_path not in written, "Customized dashboard was overwritten")
        require(dashboard_path.read_bytes() == custom, "Customized dashboard changed")
        require(
            hass.store_payload.get("assets", {}).get("dashboard_hoymiles.yaml")
            is None,
            "Customized dashboard retained false managed metadata",
        )
        require(stale != hass.store_payload, "Customized dashboard metadata did not reconcile")

    with tempfile.TemporaryDirectory(prefix="hoymiles_english_overwrite_") as tmp:
        config_path = Path(tmp)
        hass = ValidatorHass(config_path, "en-GB")
        written = validator_install(assets, hass, overwrite=True)
        require(len(written) == 7, "Fresh English async overwrite count differs")
        require(
            (config_path / "dashboard_hoymiles.yaml").read_bytes()
            == (RESOURCES / "dashboard_hoymiles_en.yaml").read_bytes(),
            "English async overwrite copied wrong dashboard",
        )
        require(
            validator_metadata_hash(hass) == VALIDATOR_NEW_HASHES["en"],
            "English scheduler metadata differs",
        )

    with tempfile.TemporaryDirectory(prefix="hoymiles_live_collision_") as tmp:
        config_path = Path(tmp)
        package_path = validator_scheduler_path(config_path)
        package_path.parent.mkdir(parents=True)
        package_path.write_bytes(
            (
                RESOURCES
                / "home_assistant"
                / "pl"
                / "hoymiles_ems_scheduler.yaml"
            ).read_bytes()
        )
        hass = ValidatorHass(config_path)
        results: list = []

        def capture_collision(name: str, result) -> None:
            if name == "_sync_assets":
                results.append(result)

        hass.executor_after = capture_collision
        validator_set_ui_collision(hass)
        written = validator_install(assets, hass)
        require(package_path not in written, "Live editable collision was missed")
        require(validator_metadata_hash(hass) is None, "Collision repaired missing metadata")
        require(
            not package_path.with_name(package_path.name + VALIDATOR_BACKUP_SUFFIX).exists(),
            "Current collision created backup",
        )
        require(
            results[0].scheduler.action.value
            == VALIDATOR_TRANSITIONS["collision"],
            "Literal collision transition differs",
        )

    with tempfile.TemporaryDirectory(prefix="hoymiles_backup_exact_") as tmp:
        config_path = Path(tmp)
        package_path = validator_scheduler_path(config_path)
        package_path.parent.mkdir(parents=True)
        package_path.write_bytes(VALIDATOR_BACKUP_FIXTURE)
        backup = package_path.with_name(package_path.name + VALIDATOR_BACKUP_SUFFIX)
        backup.write_bytes(VALIDATOR_BACKUP_FIXTURE)
        before = (backup.stat().st_ino, backup.stat().st_mtime_ns, backup.read_bytes())
        hass = ValidatorHass(config_path)
        written = validator_install(assets, hass, overwrite=True)
        require(package_path in written, "Exact existing backup blocked overwrite")
        require(
            (backup.stat().st_ino, backup.stat().st_mtime_ns, backup.read_bytes())
            == before,
            "Exact existing backup was overwritten",
        )

    with tempfile.TemporaryDirectory(prefix="hoymiles_backup_foreign_") as tmp:
        config_path = Path(tmp)
        package_path = validator_scheduler_path(config_path)
        package_path.parent.mkdir(parents=True)
        package_path.write_bytes(VALIDATOR_BACKUP_FIXTURE)
        backup = package_path.with_name(package_path.name + VALIDATOR_BACKUP_SUFFIX)
        backup.write_bytes(b"foreign backup")
        hass = ValidatorHass(config_path)
        written = validator_install(assets, hass, overwrite=True)
        require(package_path not in written, "Foreign backup allowed overwrite")
        require(package_path.read_bytes() == VALIDATOR_BACKUP_FIXTURE, "Foreign backup changed scheduler")
        require(backup.read_bytes() == b"foreign backup", "Foreign backup was overwritten")
        require(validator_metadata_hash(hass) is None, "Foreign backup advanced metadata")

    with tempfile.TemporaryDirectory(prefix="hoymiles_legacy_temp_") as tmp:
        config_path = Path(tmp)
        package_path = validator_scheduler_path(config_path)
        package_path.parent.mkdir(parents=True)
        package_path.write_bytes(VALIDATOR_BACKUP_FIXTURE)
        legacy = package_path.parent / VALIDATOR_OLD_TEMP
        legacy.write_bytes(b"protected legacy temp")
        identity = (legacy.stat().st_ino, legacy.stat().st_mtime_ns)
        written = validator_install(assets, ValidatorHass(config_path), overwrite=True)
        require(package_path in written, "Legacy fixed temp blocked unique-temp transaction")
        require(legacy.read_bytes() == b"protected legacy temp", "Legacy temp bytes changed")
        require(
            (legacy.stat().st_ino, legacy.stat().st_mtime_ns) == identity,
            "Legacy temp identity changed",
        )

    with tempfile.TemporaryDirectory(prefix="hoymiles_metadata_recovery_") as tmp:
        config_path = Path(tmp)
        package_path = validator_scheduler_path(config_path)
        package_path.parent.mkdir(parents=True)
        package_path.write_bytes(VALIDATOR_BACKUP_FIXTURE)
        old_hash = hashlib.sha256(VALIDATOR_BACKUP_FIXTURE).hexdigest()
        hass = ValidatorHass(config_path)
        hass.store_payload = {
            "integration_version": "1.5.7",
            "assets": {VALIDATOR_SCHEDULER_RELATIVE: old_hash},
        }
        hass.metadata_failures = 1
        try:
            validator_install(assets, hass, overwrite=True)
        except OSError:
            pass
        else:
            raise RuntimeError("Injected metadata failure was swallowed")
        inode = package_path.stat().st_ino
        require(
            validator_metadata_hash(hass) == old_hash,
            "Metadata failure advanced scheduler hash",
        )
        written = validator_install(assets, hass)
        require(package_path not in written, "Metadata self-heal replaced current file")
        require(package_path.stat().st_ino == inode, "Metadata self-heal changed identity")
        require(
            validator_metadata_hash(hass) == VALIDATOR_NEW_HASHES["pl"],
            "Metadata self-heal did not commit exact new hash",
        )

    with tempfile.TemporaryDirectory(prefix="hoymiles_modified_policy_") as tmp:
        config_path = Path(tmp)
        package_path = validator_scheduler_path(config_path)
        package_path.parent.mkdir(parents=True)
        modified = b"# user modified scheduler\nstate: custom\n"
        package_path.write_bytes(modified)
        hass = ValidatorHass(config_path)
        written = validator_install(assets, hass, overwrite=False)
        require(package_path not in written, "overwrite false replaced modified scheduler")
        require(package_path.read_bytes() == modified, "modified scheduler changed")
        require(
            not package_path.with_name(package_path.name + VALIDATOR_BACKUP_SUFFIX).exists(),
            "modified hold created backup",
        )
        written = validator_install(assets, hass, overwrite=True)
        require(package_path in written, "explicit overwrite did not replace modified scheduler")
        require(
            package_path.with_name(
                package_path.name + VALIDATOR_BACKUP_SUFFIX
            ).read_bytes()
            == modified,
            "Explicit overwrite backup differs",
        )

    with tempfile.TemporaryDirectory(prefix="hoymiles_replace_failure_") as tmp:
        config_path = Path(tmp)
        package_path = validator_scheduler_path(config_path)
        package_path.parent.mkdir(parents=True)
        package_path.write_bytes(VALIDATOR_BACKUP_FIXTURE)
        hass = ValidatorHass(config_path)
        hass.replace_failure_path = package_path
        written = validator_install(assets, hass, overwrite=True)
        require(package_path not in written, "Replace failure reported success")
        require(
            package_path.read_bytes() == VALIDATOR_BACKUP_FIXTURE,
            "Replace failure changed scheduler",
        )
        require(
            validator_metadata_hash(hass) is None,
            "Replace failure advanced scheduler metadata",
        )

    async def validate_executor_pause_collision() -> None:
        with tempfile.TemporaryDirectory(prefix="hoymiles_pause_collision_") as tmp:
            config_path = Path(tmp)
            hass = ValidatorHass(config_path)
            hass.pause_executor_name = "_sync_assets"
            hass.pause_entered = asyncio.Event()
            hass.pause_release = asyncio.Event()
            task = asyncio.create_task(
                assets.async_install_assets(
                    hass,
                    overwrite=False,
                    publish_frontend=False,
                )
            )
            await hass.pause_entered.wait()
            validator_set_ui_collision(hass)
            hass.pause_release.set()
            written = await task
            package_path = validator_scheduler_path(config_path)
            require(package_path not in written, "Paused collision reported write")
            require(not package_path.exists(), "Paused collision did not remove fresh file")
            require(
                validator_metadata_hash(hass) is None,
                "Paused collision advanced scheduler metadata",
            )

    asyncio.run(validate_executor_pause_collision())

    require(
        "hass.services.async_call" not in assets_source
        and "write_register" not in assets_source
        and "grant_execution" not in assets_source,
        "Managed-package delivery added a physical execution path",
    )

    class FakeResourceCollection:
        """Exercise the same live collection contract as Lovelace websocket."""

        def __init__(self, items: list[dict]) -> None:
            self.data = {item["id"]: dict(item) for item in items}
            self.loaded = False

        async def async_get_info(self) -> dict[str, int]:
            self.loaded = True
            return {"resources": len(self.data)}

        def async_items(self) -> list[dict]:
            require(self.loaded, "Resource items were read before lazy loading")
            return list(self.data.values())

        async def async_update_item(self, item_id: str, updates: dict) -> dict:
            require(self.loaded, "Resource was updated before lazy loading")
            normalized = dict(updates)
            if "res_type" in normalized:
                normalized["type"] = normalized.pop("res_type")
            self.data[item_id].update(normalized)
            return self.data[item_id]

        async def async_create_item(self, data: dict) -> dict:
            require(self.loaded, "Resource was created before lazy loading")
            item = dict(data)
            if "res_type" in item:
                item["type"] = item.pop("res_type")
            item["id"] = "generated-hoymiles-resource"
            self.data[item["id"]] = item
            return item

        async def async_delete_item(self, item_id: str) -> None:
            require(self.loaded, "Resource was deleted before lazy loading")
            del self.data[item_id]

    def fake_hass(
        collection: FakeResourceCollection,
        mode: str = "storage",
    ) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            data={
                "lovelace": types.SimpleNamespace(
                    resource_mode=mode,
                    resources=collection,
                )
            }
        )

    fresh_collection = FakeResourceCollection([])
    fresh_hass = fake_hass(fresh_collection)
    require(
        asyncio.run(assets._async_sync_lovelace_resource(fresh_hass)),
        "Fresh storage setup did not create the live Lovelace resource",
    )
    require(
        fresh_collection.async_items()
        == [
            {
                "id": "generated-hoymiles-resource",
                "url": assets.FRONTEND_RESOURCE_URL,
                "type": "module",
            }
        ],
        "Fresh setup created an invalid live Lovelace resource",
    )
    require(
        not asyncio.run(assets._async_sync_lovelace_resource(fresh_hass)),
        "Live Lovelace resource setup is not idempotent",
    )

    bootstrap_only = FakeResourceCollection(
        [
            {
                "id": "legacy-bootstrap",
                "url": (
                    "/local/hoymiles-dashboard-strategy.js"
                    "?v=1.5.2.15"
                ),
                "type": "module",
            }
        ]
    )
    require(
        asyncio.run(
            assets._async_sync_lovelace_resource(fake_hass(bootstrap_only))
        )
        and bootstrap_only.async_items()
        == [
            {
                "id": "legacy-bootstrap",
                "url": assets.FRONTEND_RESOURCE_URL,
                "type": "module",
            }
        ],
        "A lone legacy bootstrap was not upgraded to the full module",
    )

    yaml_collection = FakeResourceCollection(
        [
            {
                "id": "yaml-bootstrap",
                "url": "/local/hoymiles-dashboard-strategy.js?v=old",
                "type": "module",
            }
        ]
    )
    require(
        not asyncio.run(
            assets._async_sync_lovelace_resource(
                fake_hass(yaml_collection, mode="yaml")
            )
        )
        and not yaml_collection.loaded
        and yaml_collection.data["yaml-bootstrap"]["url"]
        == "/local/hoymiles-dashboard-strategy.js?v=old",
        "YAML resource mode was mutated instead of using the global module",
    )

    with tempfile.TemporaryDirectory(prefix="hoymiles_storage_upgrade_") as tmp:
        config_path = Path(tmp)
        storage_path = config_path / ".storage"
        storage_path.mkdir()
        resources_path = storage_path / "lovelace_resources"
        resources_payload = {
            "version": 1,
            "minor_version": 1,
            "key": "lovelace_resources",
            "data": {
                "items": [
                    {
                        "id": "legacy-hoymiles-bootstrap",
                        "url": (
                            "/api/hoymiles_hit_modbus/static-r2/"
                            "hoymiles-dashboard-strategy.js?v=1.5.2.15"
                        ),
                        "type": "module",
                    },
                    {
                        "id": "canonical-hoymiles-resource",
                        "url": assets.FRONTEND_RESOURCE_URL,
                        "type": "module",
                    },
                    {
                        "id": "duplicate-hoymiles-resource",
                        "url": (
                            "/api/hoymiles_hit_modbus/static-r2/"
                            "hoymiles-rce-chart-card.js?v=1.5.2.15"
                        ),
                        "type": "module",
                    },
                    {
                        "id": "unrelated-resource",
                        "url": "/local/user-card.js",
                        "type": "module",
                    },
                ]
            },
        }
        resources_path.write_text(
            json.dumps(resources_payload), encoding="utf-8"
        )

        dashboard_path = storage_path / "lovelace.hoymiles_test"
        dashboard_payload = {
            "version": 1,
            "minor_version": 1,
            "key": "lovelace.hoymiles_test",
            "data": {
                "config": {
                    "title": "Custom user layout",
                    "views": [
                        {
                            "cards": [
                                {
                                    "type": "entities",
                                    "title": "User card",
                                    "entities": [
                                        "sensor.hoymiles_hit_overview_pv_total_power",
                                        "sensor.hoymiles_hit_overview_battery_power",
                                        "sensor.unrelated_user_entity",
                                        {
                                            "entity": "sensor.hoymiles_rce_pv_self_consumption_today",
                                            "name": "PV → odbiorniki dzisiaj",
                                        },
                                        {
                                            "entity": "sensor.hoymiles_rce_battery_to_load_today",
                                            "name": "Bateria → odbiorniki dzisiaj",
                                        },
                                        {
                                            "entity": "sensor.hoymiles_rce_grid_to_load_today",
                                            "name": "Sieć → odbiorniki dzisiaj",
                                        },
                                    ],
                                },
                                {
                                    "type": "markdown",
                                    "content": "User content",
                                },
                                {
                                    "type": "custom:hoymiles-power-flow-card",
                                    "inverter_image": (
                                        "/api/hoymiles_hit_modbus/static/"
                                        "hoymiles-inverter.png"
                                    ),
                                },
                            ]
                        }
                    ],
                }
            },
        }
        dashboard_path.write_text(
            json.dumps(dashboard_payload), encoding="utf-8"
        )

        unrelated_path = storage_path / "lovelace.unrelated"
        unrelated_payload = {
            "version": 1,
            "minor_version": 1,
            "key": "lovelace.unrelated",
            "data": {
                "config": {
                    "views": [
                        {"cards": [{"type": "entities", "entities": []}]}
                    ]
                }
            },
        }
        unrelated_text = json.dumps(unrelated_payload)
        unrelated_path.write_text(unrelated_text, encoding="utf-8")

        migrated = assets._sync_lovelace_storage(config_path)
        require(
            migrated == [dashboard_path],
            "Storage-mode dashboard migration changed unexpected files",
        )
        migrated_dashboard = json.loads(
            dashboard_path.read_text(encoding="utf-8")
        )
        cards = migrated_dashboard["data"]["config"]["views"][0]["cards"]
        require(
            cards[0]["type"] == assets.ZEBRA_CARD_TYPE,
            "Storage-mode entities cards were not upgraded to zebra cards",
        )
        require(
            cards[0]["title"] == "User card"
            and "sensor.unrelated_user_entity" in cards[0]["entities"]
            and cards[1]["content"] == "User content"
            and cards[2]["inverter_image"] == assets.INVERTER_IMAGE_PATH,
            "Storage-mode migration did not preserve user customizations",
        )
        migrated_rows = [
            row
            for row in cards[0]["entities"]
            if isinstance(row, dict)
        ]
        require(
            migrated_rows[0]
            == {
                "entity": "sensor.hoymiles_actual_load_energy_today",
                "name": "Rzeczywiste zużycie odbiorników dzisiaj",
            }
            and migrated_rows[1]["name"]
            == "PV → odbiorniki — rejestr diagnostyczny"
            and migrated_rows[2]["name"]
            == "Energia oddana przez baterię — diagnostycznie"
            and migrated_rows[3]["name"]
            == "Energia pobrana z sieci — diagnostycznie",
            "Storage-mode RCE LOAD rows were not migrated safely",
        )
        require(
            json.loads(resources_path.read_text(encoding="utf-8"))
            == resources_payload,
            "Dashboard migration edited Lovelace resources behind HA's live collection",
        )
        collection = FakeResourceCollection(resources_payload["data"]["items"])
        require(
            asyncio.run(assets._async_sync_lovelace_resource(fake_hass(collection))),
            "Legacy integration-static resource was not migrated live",
        )
        migrated_resources = collection.async_items()
        require(
            migrated_resources[0]["id"] == "canonical-hoymiles-resource"
            and migrated_resources[0]["url"] == assets.FRONTEND_RESOURCE_URL
            and migrated_resources[0]["type"] == "module"
            and migrated_resources[1]["url"] == "/local/user-card.js",
            "Managed Lovelace resources were not reduced to one full module",
        )
        require(
            not asyncio.run(
                assets._async_sync_lovelace_resource(fake_hass(collection))
            ),
            "Migrated live Lovelace resource is not idempotent",
        )
        require(
            dashboard_path.with_name(
                f"{dashboard_path.name}.pre-{assets.VERSION}.bak"
            ).is_file(),
            "Dashboard storage migration did not create a rollback backup",
        )
        require(
            unrelated_path.read_text(encoding="utf-8") == unrelated_text,
            "Storage migration touched an unrelated dashboard",
        )
        require(
            assets._sync_lovelace_storage(config_path) == [],
            "Storage-mode migration is not idempotent",
        )

    require(
        assets.FRONTEND_RESOURCE_URL
        == f"/local/hoymiles-rce-chart-card.js?v={assets.VERSION}.28"
        and assets.FRONTEND_BOOTSTRAP_URL
        == f"/local/hoymiles-dashboard-strategy.js?v={assets.VERSION}.28"
        and "/local/hoymiles-dashboard-strategy.js"
        in assets.MANAGED_FRONTEND_RESOURCE_PATHS,
        "Frontend revision 28 or bootstrap migration paths changed",
    )


def validate_frontend_asset_failure_isolation(init_source: str) -> None:
    """Prove optional asset failures cannot disable integration-wide setup."""
    source_tree = ast.parse(init_source)
    selected = [
        node
        for node in source_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_async_prepare_frontend_assets", "async_setup"}
    ]
    require(
        {node.name for node in selected}
        == {"_async_prepare_frontend_assets", "async_setup"},
        "Frontend failure-isolation functions are missing",
    )
    executable = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            *selected,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(executable)

    class FakeIssueRegistry:
        def __init__(self) -> None:
            self.created: list[tuple] = []
            self.deleted: list[tuple] = []

        def async_create_issue(self, *args, **kwargs) -> None:
            self.created.append((args, kwargs))

        def async_delete_issue(self, *args, **kwargs) -> None:
            self.deleted.append((args, kwargs))

        class IssueSeverity:
            WARNING = "warning"

    class FakeLogger:
        def exception(self, *args, **kwargs) -> None:
            return None

        def info(self, *args, **kwargs) -> None:
            return None

    class FakeHttp:
        def __init__(self) -> None:
            self.static_paths: list = []
            self.views: list = []

        async def async_register_static_paths(self, paths) -> None:
            self.static_paths.extend(paths)

        def register_view(self, view) -> None:
            self.views.append(view)

    class FakeServices:
        def __init__(self) -> None:
            self.registrations: list[tuple] = []

        def async_register(self, *args, **kwargs) -> None:
            self.registrations.append((args, kwargs))

    class FakeHass:
        def __init__(self, config_dir: Path) -> None:
            self.config = types.SimpleNamespace(
                config_dir=str(config_dir),
                language="en",
            )
            self.data: dict = {}
            self.http = FakeHttp()
            self.services = FakeServices()

        async def async_add_executor_job(self, target, *args):
            return target(*args)

    issues = FakeIssueRegistry()
    module_globals = {
        "Path": Path,
        "HomeAssistant": object,
        "ServiceCall": object,
        "_LOGGER": FakeLogger(),
        "ir": issues,
        "DOMAIN": "hoymiles_hit_modbus",
        "EMS_PACKAGE_DOCS_URL": "https://example.invalid/docs",
        "FRONTEND_ASSETS_RESTART_ISSUE_ID": "frontend_restart",
        "FRONTEND_ASSETS_INSTALL_FAILED_ISSUE_ID": "frontend_failed",
        "STATIC_URL": "/api/hoymiles/static-r2",
        "RESOURCE_ROOT": Path("resources"),
        "FRONTEND_MODULE_URL": "/local/card.js?v=test",
        "ATTR_OVERWRITE": "overwrite",
        "SERVICE_INSTALL_ASSETS": "install_assets",
        "INSTALL_ASSETS_SCHEMA": object(),
        "StaticPathConfig": lambda *args, **kwargs: (args, kwargs),
        "HoymilesSupportBundleView": lambda: object(),
    }
    exec(compile(executable, "<frontend-startup-contract>", "exec"), module_globals)

    async def exercise_failure(error: Exception) -> None:
        async def failing_install(*args, **kwargs):
            raise error

        module_globals["async_install_assets"] = failing_install
        with tempfile.TemporaryDirectory(prefix="hoymiles_asset_failure_") as tmp:
            hass = FakeHass(Path(tmp))
            result = await module_globals["_async_prepare_frontend_assets"](hass)
        require(
            result == ([], False, False),
            f"Optional asset error was not isolated: {type(error).__name__}",
        )
        require(
            any(
                args[2] == "frontend_failed"
                for args, _kwargs in issues.created
            ),
            "Optional asset failure did not create a Repair issue",
        )

    asyncio.run(exercise_failure(OSError("disk full")))
    asyncio.run(exercise_failure(RuntimeError("live resource failed")))

    async def setup_failure_tuple(_hass):
        return [], False, False

    extra_urls: list[str] = []
    module_globals["_async_prepare_frontend_assets"] = setup_failure_tuple
    module_globals["add_extra_js_url"] = (
        lambda _hass, url: extra_urls.append(url)
    )
    module_globals["async_install_assets"] = lambda *args, **kwargs: None
    with tempfile.TemporaryDirectory(prefix="hoymiles_setup_failure_") as tmp:
        failed_hass = FakeHass(Path(tmp))
        require(
            asyncio.run(module_globals["async_setup"](failed_hass, {})) is True,
            "Asset failure disabled integration-wide setup",
        )
    require(
        len(failed_hass.http.static_paths) == 1
        and len(failed_hass.http.views) == 1
        and len(failed_hass.services.registrations) == 1
        and not extra_urls,
        "Asset failure did not preserve setup or published an unsafe module",
    )

    async def setup_success_tuple(_hass):
        return [Path("asset")], True, True

    module_globals["_async_prepare_frontend_assets"] = setup_success_tuple
    with tempfile.TemporaryDirectory(prefix="hoymiles_setup_success_") as tmp:
        success_hass = FakeHass(Path(tmp))
        require(
            asyncio.run(module_globals["async_setup"](success_hass, {})) is True,
            "Successful frontend setup did not complete",
        )
    require(
        extra_urls == ["/local/card.js?v=test"],
        "Successful frontend setup did not publish exactly one canonical module",
    )


def validate_ui_count_self_tests(
    node_executable: str,
    validation_root: Path = ROOT,
) -> None:
    """Prove both exact UI counters reject one-less and one-more oracles."""
    ui_test = validation_root / "tools" / "test_supervisor_aurora_ui_contract.js"
    source = ui_test.read_text(encoding="utf-8")
    cases = (
        ("const EXPECTED_GROUP_COUNT = 65;", "const EXPECTED_GROUP_COUNT = 64;"),
        ("const EXPECTED_GROUP_COUNT = 65;", "const EXPECTED_GROUP_COUNT = 66;"),
        ("const EXPECTED_CHECK_COUNT = 913;", "const EXPECTED_CHECK_COUNT = 912;"),
        ("const EXPECTED_CHECK_COUNT = 913;", "const EXPECTED_CHECK_COUNT = 914;"),
    )
    with tempfile.TemporaryDirectory(prefix="hoymiles-rev28-counts-") as directory:
        target = Path(directory) / ui_test.name
        for index, (anchor, replacement) in enumerate(cases, start=1):
            require(
                source.count(anchor) == 1,
                f"UI count self-test {index} anchor is not unique",
            )
            target.write_text(
                source.replace(anchor, replacement, 1),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["HOYMILES_UI_TEST_ROOT"] = str(validation_root)
            completed = subprocess.run(
                [node_executable, str(target)],
                cwd=validation_root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            require(
                completed.returncode != 0
                and "UI "
                in f"{completed.stdout}{completed.stderr}",
                f"UI count ±1 self-test {index} unexpectedly survived",
            )


def validate_rev28_visual_mutations(
    node_executable: str,
    validation_root: Path = ROOT,
) -> int:
    """Run V01–V12 against both canonical and packaged card bytes."""

    def replace_once(text: str, old: str, new: str, mutation_id: str) -> str:
        require(
            text.count(old) == 1,
            f"{mutation_id} mutation anchor count is {text.count(old)}, expected 1",
        )
        return text.replace(old, new, 1)

    def remove_last_reduced_motion(text: str) -> str:
        marker = "  @media (prefers-reduced-motion: reduce) {"
        component_start = text.find("const HOYMILES_EMS_SUPERVISOR_CSS")
        component_end = text.find(
            "class HoymilesEmsSupervisorPanel", component_start
        )
        start = text.rfind(marker, component_start, component_end)
        end = text.find(
            "  @container (max-width: 1024px)", start, component_end
        )
        require(start >= 0 and end > start, "V08 reduced-motion block anchor missing")
        return text[:start] + text[end:]

    mutations = (
        (
            "V01",
            lambda text: text.replace(
                "--policy-accent: var(--supervisor-rce);",
                "--policy-accent: var(--supervisor-cyan);",
            )
            .replace(
                "--policy-accent: var(--supervisor-tariff);",
                "--policy-accent: var(--supervisor-cyan);",
            )
            .replace(
                "--policy-accent: var(--supervisor-rcm);",
                "--policy-accent: var(--supervisor-cyan);",
            ),
        ),
        (
            "V02",
            lambda text: replace_once(
                text,
                '  .supervisor-panel[data-tone="shadow-selected"] {\n'
                "    --supervisor-tone: var(--supervisor-violet);",
                '  .supervisor-panel[data-tone="shadow-selected"] {\n'
                "    --supervisor-tone: var(--supervisor-ready);",
                "V02",
            ),
        ),
        (
            "V03",
            lambda text: replace_once(
                text,
                'this._copyElement("span", "", "physicalAuthority")',
                'this._copyElement("span", "", "heroIntro")',
                "V03",
            ),
        ),
        (
            "V04",
            lambda text: replace_once(
                text,
                '      "details",\n'
                '      ("supervisor-knowledge-detail " + className).trim()',
                '      "section",\n'
                '      ("supervisor-knowledge-detail " + className).trim()',
                "V04",
            ),
        ),
        (
            "V05",
            lambda text: replace_once(
                text,
                'this._copyElement("span", "", "safetyStrip")',
                'this._copyElement("span", "", "heroIntro")',
                "V05",
            ),
        ),
        (
            "V06",
            lambda text: replace_once(
                replace_once(
                    text,
                    "--supervisor-tariff: #49a5ff;",
                    "--supervisor-tariff: #f2b84b;",
                    "V06",
                ),
                "--supervisor-rcm: #b07cff;",
                "--supervisor-rcm: #f2b84b;",
                "V06",
            ),
        ),
        (
            "V07",
            lambda text: replace_once(
                text,
                "  .supervisor-title {\n"
                "    color: var(--hoymiles-aurora-text);",
                "  .supervisor-title {\n"
                "    color: #12ab34;",
                "V07",
            ),
        ),
        ("V08", remove_last_reduced_motion),
        (
            "V09",
            lambda text: replace_once(
                text,
                "  @container (max-width: 390px) {\n"
                "    .supervisor-hero { gap: 17px; padding: 15px 12px; }",
                "  @container (max-width: 390px) {\n"
                "    .supervisor-scope { display: none; }\n"
                "    .supervisor-hero { gap: 17px; padding: 15px 12px; }",
                "V09",
            ),
        ),
        (
            "V10",
            lambda text: replace_once(
                text,
                "      hero,\n"
                "      liveGrid,\n"
                "      policySection,\n"
                "      howSection,\n"
                "      safetyStrip,\n"
                "      knowledgeSection",
                "      hero,\n"
                "      knowledgeSection,\n"
                "      liveGrid,\n"
                "      policySection,\n"
                "      howSection,\n"
                "      safetyStrip",
                "V10",
            ),
        ),
        (
            "V11",
            lambda text: replace_once(
                text,
                '  rce: "mdi:chart-line",',
                '  rce: "mdi:circle-outline",',
                "V11",
            ),
        ),
        (
            "V12",
            lambda text: replace_once(
                text,
                "  .supervisor-backdrop,\n"
                "  .supervisor-blob,\n"
                "  .supervisor-points,\n"
                "  .supervisor-vignette {\n"
                "    inset: 0;\n"
                "    pointer-events: none;",
                "  .supervisor-backdrop,\n"
                "  .supervisor-blob,\n"
                "  .supervisor-points,\n"
                "  .supervisor-vignette {\n"
                "    inset: 0;\n"
                "    pointer-events: auto;",
                "V12",
            ),
        ),
    )

    validation_component = (
        validation_root / "custom_components" / "hoymiles_hit_modbus"
    )
    canonical_card = (
        validation_root / "home_assistant" / "www" / "hoymiles-rce-chart-card.js"
    )
    packaged_card = (
        validation_component / "resources" / "www" / "hoymiles-rce-chart-card.js"
    )
    baseline = canonical_card.read_text(encoding="utf-8")
    require(
        packaged_card.read_text(encoding="utf-8") == baseline,
        "Visual mutation baseline lacks generated card parity",
    )
    with tempfile.TemporaryDirectory(prefix="hoymiles-rev28-visual-") as directory:
        fixture_root = Path(directory)
        shutil.copytree(
            validation_component,
            fixture_root / "custom_components" / "hoymiles_hit_modbus",
        )
        for relative_path in (
            "dashboard_hoymiles.yaml",
            "home_assistant/hoymiles_ems_scheduler.yaml",
            "home_assistant/www/hoymiles-dashboard-strategy.js",
            "home_assistant/www/hoymiles-rce-chart-card.js",
            "tools/build_hacs_assets.py",
            "tools/validate_rce_card.js",
            "tools/validate_release.py",
        ):
            source_path = validation_root / relative_path
            target_path = fixture_root / relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)

        ui_test = (
            validation_root / "tools" / "test_supervisor_aurora_ui_contract.js"
        )
        environment = os.environ.copy()
        environment["HOYMILES_UI_TEST_ROOT"] = str(fixture_root)
        detected = 0
        for mutation_id, mutate in mutations:
            mutated = mutate(baseline)
            require(mutated != baseline, f"{mutation_id} did not alter the card")
            for relative_path in (
                "home_assistant/www/hoymiles-rce-chart-card.js",
                "custom_components/hoymiles_hit_modbus/resources/www/"
                "hoymiles-rce-chart-card.js",
            ):
                (fixture_root / relative_path).write_text(
                    mutated,
                    encoding="utf-8",
                )
            completed = subprocess.run(
                [node_executable, str(ui_test)],
                cwd=fixture_root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            if completed.returncode != 0:
                detected += 1
        require(
            detected == len(mutations) == 12,
            f"Visual mutation result is {detected}/12, expected 12/12",
        )
    return detected


def png_dimensions(path: Path) -> tuple[int, int]:
    """Return PNG dimensions using only the Python standard library."""
    header = path.read_bytes()[:24]
    require(
        len(header) == 24
        and header[:8] == b"\x89PNG\r\n\x1a\n"
        and header[12:16] == b"IHDR",
        f"{path.name} is not a valid PNG",
    )
    return struct.unpack(">II", header[16:24])


def entity_translation_keys(translations: dict) -> dict[str, set[str]]:
    """Return translation keys grouped by entity domain."""
    return {
        domain: set(entries)
        for domain, entries in translations.get("entity", {}).items()
    }


def main() -> int:
    """Validate HACS layout, translations, Python and bundled assets."""
    validate_historical_rev28_gate()
    current_ap1e_gate = validate_current_ap1_manifests()
    validate_current_ap1_contract()
    integration_dirs = [
        path for path in COMPONENT_ROOT.iterdir() if path.is_dir()
    ]
    require(
        integration_dirs == [COMPONENT],
        "HACS repositories may contain only one custom integration",
    )

    hacs = load_json(ROOT / "hacs.json")
    require(
        hacs.get("name") == EXPECTED_PROJECT_NAME,
        "hacs.json has the wrong public project name",
    )

    manifest = load_json(COMPONENT / "manifest.json")
    required_manifest = {
        "domain",
        "documentation",
        "issue_tracker",
        "codeowners",
        "name",
        "version",
    }
    require(
        required_manifest <= set(manifest),
        f"manifest.json is missing: {sorted(required_manifest - set(manifest))}",
    )
    require(manifest["domain"] == "hoymiles_hit_modbus", "Unexpected domain")
    require(
        manifest["name"] == EXPECTED_PROJECT_NAME
        and manifest["documentation"]
        == f"https://github.com/{EXPECTED_REPOSITORY}"
        and manifest["issue_tracker"]
        == f"https://github.com/{EXPECTED_REPOSITORY}/issues",
        "Manifest project metadata does not match the public rename contract",
    )
    require(
        re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"]) is not None,
        "Release version must use semantic MAJOR.MINOR.PATCH format",
    )

    entity_source = (COMPONENT / "entity.py").read_text(encoding="utf-8")
    init_source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    assets_source = (COMPONENT / "assets.py").read_text(encoding="utf-8")
    validate_protected_assets_ast(assets_source)
    config_flow_source = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
    sensor_platform_source = (COMPONENT / "sensor.py").read_text(encoding="utf-8")
    const_source = (COMPONENT / "const.py").read_text(encoding="utf-8")
    require(
        f'VERSION = "{manifest["version"]}"' in const_source,
        "const.py VERSION does not match manifest.json",
    )
    require(
        f'NAME = "{EXPECTED_PROJECT_NAME}"' in const_source,
        "const.py NAME does not match the public project name",
    )
    require(
        "async_track_state_report_event" in entity_source
        and "EventStateReportedData" in entity_source
        and "_async_source_state_reported" in entity_source,
        "Proxy entities must forward unchanged source reports for signed freshness",
    )
    firmware_core_source = (ROOT / "packages" / "core.yaml").read_text(
        encoding="utf-8"
    )
    firmware_version_match = re.search(
        r'^\s*version: "(\d+\.\d+\.\d+)"$',
        firmware_core_source,
        re.MULTILINE,
    )
    require(
        firmware_version_match is not None,
        "ESPHome project version is missing from packages/core.yaml",
    )
    compatible_firmware_version = firmware_version_match.group(1)
    require(
        'name: "hoymiles.energy-storage-modbus"' in firmware_core_source,
        "Stable ESPHome project.name must not change during a marketing rename",
    )
    ems_package_version_match = re.search(
        r'^EMS_PACKAGE_VERSION = "([^"]+)"$', const_source, re.MULTILINE
    )
    require(
        ems_package_version_match is not None,
        "const.py is missing EMS_PACKAGE_VERSION",
    )
    expected_ems_package_version = ems_package_version_match.group(1)
    ems_package_source = (
        ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml"
    ).read_text(encoding="utf-8")
    require(
        'EMS_PACKAGE_SENTINEL = "input_boolean.hoymiles_rce_discharge_enabled"'
        in const_source
        and 'EMS_PACKAGE_VERSION_ENTITY = "sensor.hoymiles_ems_package_version"'
        in const_source
        and "EMS_PACKAGE_VERSION_ENTITY," in sensor_platform_source
        and "EMS_PACKAGE_SENTINEL," in init_source
        and "hoymiles_rce_discharge_enabled:" in ems_package_source
        and "hoymiles_rce_automation_enabled" not in init_source
        and "hoymiles_rce_automation_enabled" not in sensor_platform_source,
        "Setup status and Repairs must use an existing shared EMS package sentinel",
    )
    require(
        "ems_package_restart_required" in init_source
        and "_ems_package_restart_issue_id" in init_source
        and "package_version.state == EMS_PACKAGE_VERSION" in init_source
        and "EMS_PACKAGE_VERSION," in sensor_platform_source
        and '"expected_ems_package_version": EMS_PACKAGE_VERSION'
        in sensor_platform_source
        and '"restart_required": self._ems_restart_required'
        in sensor_platform_source,
        "Managed EMS package updates do not expose the required restart state",
    )
    require(
        "def suggested_object_id(self)" in entity_source,
        "Proxy entities need an explicit stable suggested_object_id property",
    )
    require(
        "_async_reconcile_entity_registry(" in init_source
        and "entity_registry.async_remove" in init_source,
        "Localized entity ids and stale catalog proxies are not reconciled",
    )
    require(
        init_source.index("async_forward_entry_setups")
        < init_source.index("_async_reconcile_entity_registry", init_source.index("async_forward_entry_setups")),
        "Entity registry must be reconciled after platform setup",
    )
    require(
        "firmware_update_required" in entity_source
        and "matched.source is not None" in entity_source,
        "Missing-firmware proxy entities are not represented safely",
    )
    require(
        "StaticPathConfig" in init_source
        and 'STATIC_URL = f"/api/{DOMAIN}/{FRONTEND_STATIC_ROUTE}"'
        in init_source
        and 'FRONTEND_STATIC_ROUTE = "static-r2"' in assets_source,
        "Integration assets are not exposed through the stable no-cache URL",
    )
    require(
        "add_extra_js_url" in init_source
        and "FRONTEND_MODULE_URL" in init_source
        and "FRONTEND_RESOURCE_URL" in init_source
        and "?v={VERSION}" in assets_source,
        "Dashboard strategy module is not registered with versioned cache busting",
    )
    setup_body = init_source.split(
        "async def async_setup(hass: HomeAssistant, config: dict) -> bool:", 1
    )[1].split("async def async_setup_entry", 1)[0]
    require(
        setup_body.index("await _async_prepare_frontend_assets")
        < setup_body.index("add_extra_js_url"),
        "Local frontend assets must be prepared before their URL is published",
    )
    require(
        "Availability is restart-gated" in setup_body
        and "frontend_assets_ready and frontend_local_ready" in setup_body
        and setup_body.count("add_extra_js_url(hass,") == 1
        and "FRONTEND_MODULE_URL = FRONTEND_BOOTSTRAP_URL" in init_source
        and "FRONTEND_BOOTSTRAP_URL" in init_source,
        "Restart-gated /local startup or early strategy loader is missing",
    )
    require(
        "frontend" in manifest.get("dependencies", []),
        "Frontend dependency is required for automatic strategy registration",
    )
    require(
        "lovelace" in manifest.get("dependencies", [])
        and "_async_sync_lovelace_resource" in assets_source
        and "async_update_item" in assets_source
        and "async_create_item" in assets_source,
        "Managed resource migration must use Lovelace's live storage collection",
    )
    require(
        "_sync_lovelace_storage" not in init_source
        and "_sync_lovelace_storage" not in assets_source.split(
            "async def async_install_assets", 1
        )[1],
        "Runtime asset install must not mutate .storage/lovelace.* behind HA",
    )
    require(
        "_async_default_source_device_id" in config_flow_source
        and "CONF_COPY_ASSETS: True" in config_flow_source
        and "BooleanSelector" not in config_flow_source,
        "Config flow still exposes avoidable asset-copy choices",
    )
    require(
        "ems_package_not_loaded" in init_source
        and "issue_registry" in init_source
        and "HoymilesSetupStatusSensor" in sensor_platform_source
        and "firmware_coverage_percent" in sensor_platform_source,
        "Beginner setup status or EMS package Repair is missing",
    )

    catalog = load_json(COMPONENT / "entity_catalog.json")
    require(isinstance(catalog, list), "Entity catalog must be a list")
    require(len(catalog) >= 250, "The generated catalog is unexpectedly small")
    identities = {
        (entry["domain"], entry["translation_key"]) for entry in catalog
    }
    require(
        len(identities) == len(catalog),
        "Duplicate domain/translation_key in entity catalog",
    )
    require(
        ("button", "clear_fault") in identities,
        "Generated catalog is missing the Clear Fault button",
    )
    system_package = (ROOT / "packages" / "system.yaml").read_text(
        encoding="utf-8"
    )
    require(
        "create_write_single_command" in system_package
        and "controller, 3004, 1" in system_package,
        "Clear Fault must write value 1 to holding register 3004",
    )
    meter_package = (ROOT / "packages" / "meters.yaml").read_text(
        encoding="utf-8"
    )
    for power_name in (
        "Meter Grid Active Power L1",
        "Meter Grid Active Power L2",
        "Meter Grid Active Power L3",
        "Meter Grid Total Active Power",
    ):
        require(
            re.search(
                rf"modbus_controller_id: \$\{{modbus_fast_controller_id\}}\s+"
                rf'name: "{re.escape(power_name)}"',
                meter_package,
            )
            is not None,
            f"{power_name} must use the fast Modbus polling controller",
        )

    english = load_json(COMPONENT / "translations" / "en.json")
    polish = load_json(COMPONENT / "translations" / "pl.json")
    require(
        "ems_package_restart_required" in english.get("issues", {})
        and "ems_package_restart_required" in polish.get("issues", {}),
        "EMS package restart Repair is not translated in both languages",
    )
    require(
        entity_translation_keys(english) == entity_translation_keys(polish),
        "English and Polish entity translation keys differ",
    )
    for entry in catalog:
        domain = entry["domain"]
        key = entry["translation_key"]
        require(key in english["entity"][domain], f"Missing English key {domain}.{key}")
        require(key in polish["entity"][domain], f"Missing Polish key {domain}.{key}")

    required_assets = [
        RESOURCES / "dashboard_hoymiles_en.yaml",
        RESOURCES / "dashboard_hoymiles_pl.yaml",
        RESOURCES / "home_assistant" / "en" / "hoymiles_ems_scheduler.yaml",
        RESOURCES / "home_assistant" / "pl" / "hoymiles_ems_scheduler.yaml",
        RESOURCES / "www" / "hoymiles-rce-chart-card.js",
        RESOURCES / "www" / "hoymiles-dashboard-strategy.js",
        RESOURCES / "www" / "hoymiles-inverter.png",
        RESOURCES / "www" / "dashboard_hoymiles_en.json",
        RESOURCES / "www" / "dashboard_hoymiles_pl.json",
    ]
    for asset in required_assets:
        require(asset.is_file(), f"Missing bundled asset: {asset.relative_to(ROOT)}")

    for package_path in (
        ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml",
        required_assets[2],
        required_assets[3],
    ):
        package_text = package_path.read_text(encoding="utf-8")
        require(
            "unique_id: hoymiles_ems_package_version" in package_text
            and f'state: "{expected_ems_package_version}"' in package_text,
            "EMS package version marker does not match "
            f"{expected_ems_package_version} "
            f"in {package_path.relative_to(ROOT)}",
        )

    english_package = required_assets[2].read_text(encoding="utf-8")
    polish_package = required_assets[3].read_text(encoding="utf-8")
    require(
        "No active automation" in english_package
        and "Balancing" in english_package
        and "Manual control" in english_package
        and "Enabled — waiting for a selected slot" in english_package
        and "Active — grid charging" in english_package
        and "Unavailable — initializing" in english_package
        and "Enabled — blocked: RCE policy is enabled" in english_package,
        "English EMS package lacks the human ownership/tariff policy states",
    )
    require(
        "Brak aktywnej automatyki" in polish_package
        and "Balansowanie" in polish_package
        and "Sterowanie ręczne" in polish_package
        and "Włączone — oczekuje na wybrany blok" in polish_package
        and "Aktywne — ładowanie z sieci" in polish_package
        and "Niedostępne — trwa inicjalizacja" in polish_package
        and "Włączone — zablokowane: włączona polityka RCE" in polish_package,
        "Polish EMS package lacks the human ownership/tariff policy states",
    )

    dashboard_source = (
        ROOT / "dashboard_hoymiles.yaml"
    ).read_text(encoding="utf-8")

    def require_owner_and_tariff_rows(text: str, label: str) -> None:
        owner_marker = "entity: sensor.hoymiles_ems_control_owner"
        tariff_marker = "entity: sensor.hoymiles_tariff_charge_status"
        conflict_marker = "entity: binary_sensor.hoymiles_ems_control_conflict"
        main_start = text.index(owner_marker)
        main_end = text.index("\n      - type:", main_start)
        main_card = text[main_start:main_end]
        require(
            main_card.index(owner_marker)
            < main_card.index(tariff_marker)
            < main_card.index(conflict_marker),
            f"{label} main EMS card does not pair owner and tariff policy rows",
        )
        tariff_view = text.split("path: ladowanie-taryfowe", 1)[1].split(
            "\n  - title:", 1
        )[0]
        require(
            tariff_view.count(owner_marker) == 1
            and tariff_view.count(tariff_marker) == 1
            and tariff_view.count(conflict_marker) == 1
            and tariff_view.index(owner_marker)
            < tariff_view.index(tariff_marker)
            < tariff_view.index(conflict_marker),
            f"{label} tariff view must show one always-visible owner/policy pair",
        )

    require_owner_and_tariff_rows(dashboard_source, "Source dashboard")
    require_owner_and_tariff_rows(
        required_assets[0].read_text(encoding="utf-8"),
        "English dashboard",
    )
    require_owner_and_tariff_rows(
        required_assets[1].read_text(encoding="utf-8"),
        "Polish dashboard",
    )
    expected_zebra_cards = dashboard_source.count(
        "type: custom:hoymiles-zebra-entities-card"
    )
    require(
        expected_zebra_cards >= 56,
        "Source dashboard unexpectedly lost zebra entity cards",
    )
    rce_plan_index = dashboard_source.index("title: Plan rozładowań RCE")
    rce_details_index = dashboard_source.index(
        "title: RCE — szczegóły i diagnostyka"
    )
    rce_details_end = dashboard_source.index(
        "\n      - type: markdown\n",
        rce_details_index,
    )
    require(
        rce_plan_index < rce_details_index
        and "position: sidebar"
        not in dashboard_source[rce_details_index:rce_details_end],
        "RCE details must remain in the main column below the discharge plan",
    )
    dashboard_payloads = []
    supervisor_bindings = {
        "supervisor_entity": "sensor.hoymiles_hit_ems_supervisor",
        "supervisor_mode_entity": "input_select.hoymiles_ems_supervisor_mode",
        "supervisor_profile_entity": "input_select.hoymiles_ems_supervisor_profile",
        "supervisor_allow_rce_entity": (
            "input_boolean.hoymiles_ems_supervisor_allow_rce"
        ),
        "supervisor_allow_tariff_entity": (
            "input_boolean.hoymiles_ems_supervisor_allow_tariff"
        ),
        "supervisor_allow_rcm_entity": (
            "input_boolean.hoymiles_ems_supervisor_allow_rcm"
        ),
    }
    for dashboard_json in required_assets[-2:]:
        dashboard_data = load_json(dashboard_json)
        dashboard_payloads.append(dashboard_data)
        require(
            isinstance(dashboard_data, dict)
            and isinstance(dashboard_data.get("views"), list)
            and len(dashboard_data["views"]) >= 10,
            f"Invalid dashboard strategy payload: {dashboard_json.name}",
        )
        dashboard_json_text = json.dumps(dashboard_data)
        require(
            dashboard_json_text.count(
                '"type": "custom:hoymiles-zebra-entities-card"'
            )
            == expected_zebra_cards
            and '"type": "entities"' not in dashboard_json_text,
            f"{dashboard_json.name} does not use all "
            f"{expected_zebra_cards} zebra entity cards",
        )
        require(
            sum(
                item.get("type")
                == "custom:hoymiles-aurora-frame-card"
                for item in iter_mappings(dashboard_data)
            )
            == 4,
            f"{dashboard_json.name} must contain four authored Aurora frames",
        )
        start_view = next(
            (
                view
                for view in dashboard_data["views"]
                if view.get("path") == "start"
            ),
            None,
        )
        supervisor_view = next(
            (
                view
                for view in dashboard_data["views"]
                if view.get("path") == "ems-supervisor"
            ),
            None,
        )
        expected_supervisor_title = (
            "EMS Supervisor"
            if dashboard_json.name.endswith("_en.json")
            else "Nadzorca EMS"
        )
        energy_card = next(
            (
                card
                for card in (start_view or {}).get("cards", [])
                if card.get("type")
                == "custom:hoymiles-aurora-energy-card"
            ),
            None,
        )
        supervisor_cards = (supervisor_view or {}).get("cards", [])
        supervisor_card = (
            supervisor_cards[0] if len(supervisor_cards) == 1 else None
        )
        require(
            energy_card is not None
            and all(key not in energy_card for key in supervisor_bindings)
            and dashboard_data["views"].index(start_view) == 0
            and dashboard_data["views"].index(supervisor_view) == 1
            and dashboard_data["views"][2].get("path") == "automatyka-ems"
            and supervisor_view.get("title") == expected_supervisor_title
            and supervisor_view.get("icon") == "mdi:eye-circle-outline"
            and supervisor_view.get("type") == "panel"
            and supervisor_card is not None
            and supervisor_card.get("type")
            == "custom:hoymiles-ems-supervisor-card"
            and set(supervisor_card) == {"type", *supervisor_bindings}
            and all(
                supervisor_card.get(key) == entity_id
                for key, entity_id in supervisor_bindings.items()
            ),
            f"{dashboard_json.name} lacks the exact second Supervisor view",
        )
    require(
        dashboard_structure(dashboard_payloads[0])
        == dashboard_structure(dashboard_payloads[1]),
        "English and Polish strategy payloads have different card structure",
    )

    card_source = required_assets[4].read_text(encoding="utf-8")
    require(
        "ll-strategy-dashboard-hoymiles-hit-xxl-g3" in card_source
        and "dashboard_hoymiles_${language}.json" in card_source
        and "import.meta.url" in card_source
        and "/api/hoymiles_hit_modbus/static/dashboard_hoymiles_" not in card_source
        and "window.customStrategies" in card_source,
        "Dashboard strategy is not update-safe or hard-codes a stale asset route",
    )
    bootstrap_source = required_assets[5].read_text(encoding="utf-8")
    require(
        "ll-strategy-dashboard-hoymiles-hit-xxl-g3" in bootstrap_source
        and "document.currentScript" in bootstrap_source
        and "document.scripts" in bootstrap_source
        and "canonicalModuleUrl" in bootstrap_source
        and "import(canonicalModuleUrl.href)" in bootstrap_source
        and "/api/hoymiles_hit_modbus/static-r2/" not in bootstrap_source
        and "import.meta" not in bootstrap_source,
        "Classic dashboard bootstrap is missing or uses ES-module-only syntax",
    )
    require(
        card_source
        == (ROOT / "home_assistant" / "www" / "hoymiles-rce-chart-card.js")
        .read_text(encoding="utf-8")
        and bootstrap_source
        == (
            ROOT
            / "home_assistant"
            / "www"
            / "hoymiles-dashboard-strategy.js"
        ).read_text(encoding="utf-8"),
        "Generated frontend resources differ from their canonical sources",
    )
    require(
        "futureNoData" in card_source
        and "will recalculate automatically after publication" in card_source,
        "RCE chart does not explain automatic replanning after tomorrow's data",
    )
    require(
        "unitMultipliers" in card_source
        and "kWh: 1000" in card_source
        and "_resolveBatteryEnergy" in card_source,
        "Power-flow card does not convert the capacity entity from kWh to Wh",
    )
    require(
        "class HoymilesZebraEntitiesCard" in card_source
        and 'customElements.define(\n    "hoymiles-zebra-entities-card"' in card_source
        and "color-mix(" in card_source
        and "var(--hoymiles-aurora-accent) 9%" in card_source,
        "Theme-aware zebra entities card is not registered",
    )
    require(
        "class HoymilesAuroraFrameCard" in card_source
        and "class HoymilesAuroraStatusCard" in card_source
        and "class HoymilesAuroraHistoryCard" in card_source
        and "class HoymilesAuroraFinanceCard" in card_source,
        "Complete Aurora dashboard card set is not registered",
    )
    supervisor_start = card_source.find("const HOYMILES_SUPERVISOR_BINDINGS")
    supervisor_end = card_source.find("class HoymilesAuroraEnergyCard")
    require(
        supervisor_start >= 0 and supervisor_end > supervisor_start,
        "Internal EMS Supervisor panel source is missing",
    )
    supervisor_source = card_source[supervisor_start:supervisor_end]
    reason_start = supervisor_source.find(
        "const HOYMILES_SUPERVISOR_REASON_COPY"
    )
    reason_end = supervisor_source.find(
        "const HOYMILES_SUPERVISOR_COPY", reason_start
    )
    reason_source = supervisor_source[reason_start:reason_end]
    require(
        len(re.findall(r"^  [a-z0-9_]+: Object\.freeze\(", reason_source, re.M))
        == 45,
        "EMS Supervisor reason map is not 45/45",
    )
    energy_end = card_source.find("class HoymilesPowerFlowCard", supervisor_end)
    energy_source = card_source[supervisor_end:energy_end]
    aurora_markup = energy_source.find('<div class="aurora">')
    daily_markup = energy_source.find('<div class="daily">', aurora_markup)
    require(
        0 <= aurora_markup < daily_markup
        and "data-supervisor" not in energy_source
        and "_supervisorPanel" not in energy_source
        and "HOYMILES_SUPERVISOR_BINDINGS" not in energy_source,
        "Start was not restored to Aurora followed directly by daily energy",
    )
    require(
        "class HoymilesEmsSupervisorPanel" in supervisor_source
        and "class HoymilesEmsSupervisorCard extends HTMLElement"
        in supervisor_source
        and supervisor_source.count(
            '"hoymiles-ems-supervisor-card",\n    HoymilesEmsSupervisorCard'
        )
        == 1
        and "data-supervisor-card" in supervisor_source
        and 'customElements.define("hoymiles-ems-supervisor-panel"'
        not in card_source
        and 'Object.freeze(["Off", "Shadow"])' in supervisor_source
        and "Observation only" in supervisor_source
        and "Tylko obserwacja" in supervisor_source
        and "Profiles currently affect only the observation decision"
        in supervisor_source
        and "Profile wpływają obecnie wyłącznie na decyzję obserwacyjną"
        in supervisor_source,
        "EMS Supervisor standalone observation-only contract is incomplete",
    )
    for required_ui_token in (
        "heroIntro",
        "howItWorksTitle",
        "modesTitle",
        "modeActiveDescription",
        "profilesTitle",
        "permissionsTitle",
        "readResultTitle",
        "safetyTitle",
        "technicalTitle",
        "actionWarning",
        "max-width: 1440px",
        "supervisor-blob-three",
        "@media (prefers-reduced-motion: reduce)",
        'activeCard.setAttribute("aria-disabled", "true")',
        "SUPERVISOR_SEMANTIC_PALETTE_REV28",
        "supervisor-authority",
        "supervisor-core-orb",
        "supervisor-hero-result",
        "supervisor-policy-identity",
        "supervisor-policy-permission",
        "supervisor-permission-context",
        "supervisor-safety-strip",
        "supervisor-knowledge-detail",
        "supervisor-core-ring { animation: none",
        'content: "↓"',
    ):
        require(
            required_ui_token in supervisor_source,
            f"EMS Supervisor standalone UI token missing: {required_ui_token}",
        )
    css_start = supervisor_source.find("const HOYMILES_EMS_SUPERVISOR_CSS")
    css_end = supervisor_source.find(
        "class HoymilesEmsSupervisorPanel", css_start
    )
    supervisor_css = supervisor_source[css_start:css_end]
    palette_marker = "SUPERVISOR_SEMANTIC_PALETTE_REV28"
    palette_start = supervisor_css.find(palette_marker)
    palette_end = supervisor_css.find("  }", palette_start)
    require(
        supervisor_css.count(palette_marker) == 1
        and palette_start >= 0
        and palette_end > palette_start,
        "Revision 28 does not contain one centralized Supervisor palette",
    )
    palette_block = supervisor_css[palette_start:palette_end]
    css_outside_palette = (
        supervisor_css[:palette_start] + supervisor_css[palette_end:]
    )
    for name, value in (
        ("cyan", "#43d5ff"),
        ("blue", "#4c91ff"),
        ("violet", "#9b7cff"),
        ("rce", "#f2b84b"),
        ("tariff", "#49a5ff"),
        ("rcm", "#b07cff"),
        ("ready", "#47df91"),
        ("warning", "#f1b84b"),
        ("error", "#ff647c"),
    ):
        require(
            f"--supervisor-{name}: {value}" in palette_block,
            f"Revision 28 palette lost {name}",
        )
    require(
        re.search(r"#[0-9a-fA-F]{3,8}", css_outside_palette) is None,
        "Revision 28 has an ad-hoc raw color outside its semantic palette",
    )
    require(
        supervisor_source.count("this._knowledgeDetail(") == 5
        and 'details.setAttribute("open"' not in supervisor_source
        and "accordionState" not in supervisor_source
        and "toggleDetails" not in supervisor_source,
        "Revision 28 knowledge area is not exactly five closed native details",
    )
    content_start = supervisor_source.find("    content.append(")
    content_end = supervisor_source.find("    panel.append(", content_start)
    content_append = supervisor_source[content_start:content_end]
    require(
        re.search(
            r"hero,[\s\S]*liveGrid,[\s\S]*policySection,[\s\S]*"
            r"howSection,[\s\S]*safetyStrip,[\s\S]*knowledgeSection",
            content_append,
        )
        is not None
        and not re.search(
            r"modesSection|profilesSection|permissionsSection|"
            r"readSection|safetySection|technical\b",
            content_append,
        ),
        "Revision 28 documentation wall appears before live information",
    )
    selected_start = supervisor_css.find(
        '.supervisor-panel[data-tone="shadow-selected"]'
    )
    selected_end = supervisor_css.find(
        '.supervisor-panel[data-tone="blocked"]', selected_start
    )
    require(
        selected_start >= 0
        and selected_end > selected_start
        and "supervisor-ready"
        not in supervisor_css[selected_start:selected_end]
        and '.supervisor-policy-badge[data-tone="ready"]'
        in supervisor_css,
        "Revision 28 selected Shadow uses readiness green",
    )
    light_start = supervisor_css.find("@media (prefers-color-scheme: light)")
    light_end = supervisor_css.find(
        "@media (prefers-reduced-motion: reduce)", light_start
    )
    light_css = supervisor_css[light_start:light_end]
    require(
        light_start >= 0
        and light_end > light_start
        and all(
            token in light_css
            for token in (
                "--supervisor-page-base: color-mix(in srgb, "
                "var(--primary-background-color, var(--supervisor-on-deep)) "
                "94%, var(--supervisor-blue) 6%)",
                "--supervisor-page-cyan-glow: color-mix(in srgb, "
                "var(--supervisor-cyan) 5%, transparent)",
                "--supervisor-page-violet-glow: color-mix(in srgb, "
                "var(--supervisor-violet) 4%, transparent)",
                ".supervisor-blob { opacity: .08; }",
                ".supervisor-points { opacity: .1; }",
            )
        ),
        "Revision 28 light theme is not independently pale and restrained",
    )
    require(
        re.search(r"opacity:\s*\.(?:64|72)\b", supervisor_css) is None
        and ".supervisor-info-disabled { border-style: dashed; opacity: 1; }"
        in supervisor_css
        and '.supervisor-policy[data-permitted="false"] '
        '{ filter: saturate(.68); opacity: 1; }' in supervisor_css,
        "Revision 28 disabled content loses light-theme contrast",
    )
    undersized_supervisor_text = [
        float(match.group(1))
        for match in re.finditer(
            r"font-size:\s*(\d+(?:\.\d+)?)px", supervisor_css
        )
        if float(match.group(1)) < 11
    ]
    require(
        not undersized_supervisor_text,
        "Revision 28 contains Supervisor text below the accepted 11px minimum",
    )
    for forbidden in (
        "setInterval",
        "setTimeout",
        "requestAnimationFrame",
        "number.set_value",
        "select.select_option",
        "button.press",
        "input_boolean.toggle",
        "modbus.write",
    ):
        require(
            forbidden not in supervisor_source,
            f"EMS Supervisor contains forbidden scope: {forbidden}",
        )
    ui_contract_test = ROOT / "tools" / "test_supervisor_aurora_ui_contract.js"
    require(
        ui_contract_test.is_file(),
        "Missing EMS Supervisor Aurora UI contract test",
    )
    ui_contract_source = ui_contract_test.read_text(encoding="utf-8")
    require(
        "const EXPECTED_GROUP_COUNT = 65;" in ui_contract_source
        and "const EXPECTED_CHECK_COUNT = 913;" in ui_contract_source
        and all(
            f'group("{group_name}"' in ui_contract_source
            for group_name in (
                "CENTRALIZED_AURORA_PALETTE",
                "COLOR_SEMANTICS",
                "HERO_VISUAL_HIERARCHY",
                "HERO_STATE_COPY",
                "POLICY_VISUAL_IDENTITIES",
                "POLICY_SWITCH_ACCENTS",
                "DECISION_HIERARCHY",
                "VISIBLE_SAFETY_STRIP",
                "KNOWLEDGE_DETAILS",
                "FIRST_SCREEN_PRIORITY",
                "AURORA_BACKGROUND_REV28",
                "REDUCED_MOTION_REV28",
                "LIGHT_DARK_CONTRAST",
                "MOBILE_REV28",
                "FUNCTIONAL_INERTNESS",
            )
        ),
        "Revision 28 UI contract group/check freeze is incomplete",
    )
    for dashboard_path in required_assets[:2]:
        dashboard_text = dashboard_path.read_text(encoding="utf-8")
        expected_title = (
            "EMS Supervisor"
            if dashboard_path.name.endswith("_en.yaml")
            else "Nadzorca EMS"
        )
        start_index = dashboard_text.index("  - title: Start")
        supervisor_index = dashboard_text.index(
            f"  - title: {expected_title}"
        )
        rce_index = dashboard_text.index(
            "  - title: RCE and Results"
            if dashboard_path.name.endswith("_en.yaml")
            else "  - title: RCE i Wyniki"
        )
        start_yaml = dashboard_text[start_index:supervisor_index]
        supervisor_yaml = dashboard_text[supervisor_index:rce_index]
        require(
            "entity: sensor.hoymiles_rce_day_tomorrow\n"
            "            future_data: true" in dashboard_text,
            f"{dashboard_path.name} does not mark the tomorrow chart as future data",
        )
        require(
            start_index < supervisor_index < rce_index
            and "path: ems-supervisor" in supervisor_yaml
            and "icon: mdi:eye-circle-outline" in supervisor_yaml
            and "type: panel" in supervisor_yaml
            and supervisor_yaml.count(
                "type: custom:hoymiles-ems-supervisor-card"
            )
            == 1
            and all(
                f"{key}: {entity_id}" in supervisor_yaml
                for key, entity_id in supervisor_bindings.items()
            )
            and all(key not in start_yaml for key in supervisor_bindings),
            f"{dashboard_path.name} lacks exact isolated Supervisor view",
        )
        require(
            "type: custom:hoymiles-aurora-energy-card" in dashboard_text
            and "type: custom:hoymiles-aurora-status-card" in dashboard_text
            and "type: custom:hoymiles-aurora-history-card" in dashboard_text
            and "type: custom:hoymiles-aurora-finance-card" in dashboard_text
            and "type: custom:hoymiles-aurora-frame-card" in dashboard_text
            and "battery_soc_entity: sensor.hoymiles_hit_overview_battery_soc"
            in dashboard_text
            and "forecast_remaining_entity: "
            "sensor.hoymiles_solcast_forecast_remaining_today"
            in dashboard_text,
            f"{dashboard_path.name} does not contain the complete Aurora card",
        )
        require(
            "path: automatyka-ems" in dashboard_text
            and "sensor.hoymiles_rce_revenue_total" in dashboard_text
            and "sensor.hoymiles_rce_grid_export_energy_total" in dashboard_text,
            f"{dashboard_path.name} lacks the consolidated RCE results view",
        )
        for hidden_path in ("zyski", "falownik", "generator", "stany-alarmy"):
            require(
                re.search(
                    rf"path:\s+{re.escape(hidden_path)}"
                    rf"[\s\S]{{0,120}}?subview:\s+true",
                    dashboard_text,
                ),
                f"{dashboard_path.name} still exposes {hidden_path} in navigation",
            )
        require(
            "invert_power:" not in dashboard_text,
            (
                f"{dashboard_path.name} incorrectly inverts the normalized "
                "Hoymiles battery power sign"
            ),
        )
        require(
            dashboard_text.count(
                "type: custom:hoymiles-zebra-entities-card"
            )
            == expected_zebra_cards
            and not re.search(r"^\s*-?\s*type:\s+entities\s*$", dashboard_text, re.M),
            f"{dashboard_path.name} does not use all "
            f"{expected_zebra_cards} zebra entity cards",
        )
        require(
            dashboard_text.count("type: statistics-graph") >= 12
            and "period: 5minute" in dashboard_text
            and "min_y_axis: 220" in dashboard_text
            and 'color: "#FF1744"' in dashboard_text
            and 'color: "#00B0FF"' in dashboard_text
            and 'color: "#FFD600"' in dashboard_text,
            f"{dashboard_path.name} lacks the native statistics graph set",
        )
        load_graph_title = (
            "Odbiorniki — moc ostatnie 24 godziny [W]"
            if dashboard_path.name.endswith("_pl.yaml")
            else "Loads — power over the last 24 hours [W]"
        )
        load_energy_title = (
            "Zużycie domu — ostatnie 30 dni [kWh]"
            if dashboard_path.name.endswith("_pl.yaml")
            else "Home consumption — last 30 days [kWh]"
        )
        require(
            load_graph_title in dashboard_text
            and load_energy_title in dashboard_text
            and "entity: sensor.hoymiles_actual_load_energy_total"
            in dashboard_text,
            f"{dashboard_path.name} lacks the LOAD power/energy graphs",
        )

    generator_source = (ROOT / "tools" / "build_hacs_assets.py").read_text(
        encoding="utf-8"
    )
    require(
        '"  - title: Nadzorca EMS\\n"' in generator_source
        and '"    path: ems-supervisor\\n"' in generator_source
        and '"    icon: mdi:eye-circle-outline\\n"' in generator_source
        and '"  - title: EMS Supervisor\\n"' in generator_source
        and '"Nadzorca EMS": "EMS Supervisor"' not in generator_source,
        "Generator lacks the narrow full-view Supervisor title translation",
    )

    rce_sensor_source = (
        COMPONENT / "rce_sensor.py"
    ).read_text(encoding="utf-8")
    require(
        '"planning_scope": (' in rce_sensor_source
        and '"tomorrow_data_pending": not tomorrow_rows_complete' in rce_sensor_source
        and '"automatic_replan": True' in rce_sensor_source
        and "[*today_rows, *usable_tomorrow_rows]" in rce_sensor_source,
        "RCE sensor lacks the safe today-only planning fallback",
    )
    rce_optimizer_source = (
        COMPONENT / "rce_optimizer.py"
    ).read_text(encoding="utf-8")
    rce_test_source = (ROOT / "tools" / "test_rce_optimizer.py").read_text(
        encoding="utf-8"
    )
    require(
        "def _solve_joint_horizon_exports(" in rce_optimizer_source
        and "def maximum_feasible(" in rce_optimizer_source
        and 'solver_method: str = "joint_horizon_bounded_active_set"'
        in rce_optimizer_source
        and "optimality_verified: bool = False" in rce_optimizer_source
        and "exports[candidate.start] = round(low, 2)" not in rce_optimizer_source
        and "test_solver_matches_independent_random_oracle"
        in rce_test_source
        and "test_real_horizon_solver_runtime_is_bounded" in rce_test_source,
        "RCE optimizer lacks the bounded joint-horizon/oracle safety contract",
    )
    require(
        "minimum_price_pln_kwh" not in rce_optimizer_source
        and "hoymiles_rce_price_threshold" not in rce_sensor_source,
        "RCE optimizer still depends on a manually configured price threshold",
    )
    tariff_optimizer_source = (
        COMPONENT / "tariff_optimizer.py"
    ).read_text(encoding="utf-8")
    tariff_sensor_source = (
        COMPONENT / "tariff_sensor.py"
    ).read_text(encoding="utf-8")
    require(
        "first_shortage_index" in tariff_optimizer_source
        and "accepted_support_kwh" in tariff_optimizer_source
        and "for index in range(len(starts))" in tariff_optimizer_source
        and "trial_simulation.shortage_kwh" in tariff_optimizer_source
        and "minimum_saving_pln_kwh" in tariff_optimizer_source,
        "Tariff optimizer lacks deficit-reducing direct support or charging",
    )
    require(
        "bms_charge_power_limit_kw" in tariff_sensor_source
        and "effective_charge_power_percent" in tariff_sensor_source
        and "forecast_tomorrow_kwh" in tariff_sensor_source,
        "Tariff sensor lacks forecast or BMS-safe charge-power diagnostics",
    )

    stable_entity_assets = [
        ROOT / "dashboard_hoymiles.yaml",
        ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml",
        required_assets[0],
        required_assets[1],
        required_assets[2],
        required_assets[3],
    ]
    stable_entity_pattern = re.compile(
        r"\b(button|sensor|number|select)\.hoymiles_hit_([a-z0-9_]+)\b"
    )
    legacy_entity_pattern = re.compile(
        r"\b(?:button|sensor|number|select)\."
        r"[a-z0-9_]*hoymiles_inverter[a-z0-9_]*\b"
    )
    native_integration_entities = {
        ("sensor", "rce_optimized_plan"),
        ("sensor", "tariff_charge_plan"),
        ("sensor", "rcm_voltage_plan"),
        ("sensor", "setup_status"),
        ("sensor", "ems_supervisor"),
    }
    for asset_path in stable_entity_assets:
        asset_text = asset_path.read_text(encoding="utf-8")
        require(
            not legacy_entity_pattern.search(asset_text),
            "Installation-specific entity id remains in "
            f"{asset_path.relative_to(ROOT)}",
        )
        for domain, translation_key in stable_entity_pattern.findall(
            asset_text
        ):
            require(
                (domain, translation_key) in identities
                or (domain, translation_key) in native_integration_entities,
                f"Asset references an entity absent from the catalog: "
                f"{domain}.hoymiles_hit_{translation_key}",
            )

    for package_path in required_assets[2:4]:
        package_text = package_path.read_text(encoding="utf-8")
        dynamic_reserve_markers = (
            "hoymiles_ems_push_notifications_enabled:",
            "hoymiles_ems_push_notify_target:",
            "unique_id: hoymiles_ems_push_notification_status",
            "id: hoymiles_ems_push_status_notification",
            "action: notify.send_message",
            "input_boolean.hoymiles_rce_dynamic_soc_enabled",
            "input_number.hoymiles_rce_soc_safety_margin",
            "input_text.hoymiles_solcast_forecast_today_entity",
            "input_text.hoymiles_solcast_forecast_tomorrow_entity",
            "hoymiles_rce_inverter_rated_power:",
            "hoymiles_rce_fallback_daily_load:",
            "hoymiles_rce_export_efficiency:",
            "sensor.solcast_pv_forecast_forecast_today",
            "sensor.solcast_pv_forecast_forecast_tomorrow",
            "sensor.solcast_pv_forecast_prognoza_na_dzisiaj",
            "sensor.solcast_pv_forecast_prognoza_na_jutro",
            "unique_id: hoymiles_rce_day_tomorrow",
            "state_characteristic: sum_differences_nonnegative",
            "max_age:\n      days: 4",
            "hoymiles_tariff_charge_enabled:",
            "hoymiles_tariff_type:",
            "hoymiles_tariff_latched_target_soc:",
            "hoymiles_tariff_latched_slot_end:",
            "input_number.hoymiles_tariff_latched_target_soc",
            "input_datetime.hoymiles_tariff_latched_slot_end",
            "'current_slot_end'",
            'for: "00:00:45"',
            "unique_id: hoymiles_tariff_planned_charge_slot",
            "id: hoymiles_automatic_ems_mode_interlock",
            "id: hoymiles_tariff_grid_charge_control",
            "hoymiles_rcm_pre_discharge_enabled:",
            "hoymiles_rcm_pre_discharge_active:",
            "unique_id: hoymiles_rcm_natural_headroom_before_risk",
            "unique_id: hoymiles_rcm_planned_grid_discharge",
            "unique_id: hoymiles_rcm_pre_discharge_power",
            "unique_id: hoymiles_rcm_pre_discharge_target_soc",
            "id: hoymiles_rcm_pre_discharge_control",
            "input_boolean.hoymiles_rcm_shadow_mode",
            "binary_sensor.hoymiles_sale_block_active",
            "number.hoymiles_hit_maximum_discharge_power",
            "number.hoymiles_hit_force_discharge_soc",
            "hoymiles_battery_balancing_enabled:",
            "hoymiles_battery_balancing_active:",
            "hoymiles_battery_balancing_interval_days:",
            "hoymiles_battery_balancing_hold_hours:",
            "unique_id: hoymiles_battery_balancing_slow_charge_power",
            "unique_id: hoymiles_battery_balancing_due",
            "hoymiles_start_battery_balancing:",
            "hoymiles_stop_battery_balancing:",
            "id: hoymiles_battery_balancing_control",
            "timer.hoymiles_battery_balancing_watchdog",
            "input_boolean.hoymiles_battery_balancing_active",
            "sensor.hoymiles_actual_load_energy_today",
            "unique_id: hoymiles_actual_load_energy_total",
            "unique_id: hoymiles_actual_load_energy_daily",
            "source: sensor.hoymiles_actual_load_energy_total",
            "source_entity: sensor.hoymiles_actual_load_power",
            "sensor.hoymiles_actual_load_power",
            "sensor.hoymiles_hit_load_power_l1n",
            "sensor.hoymiles_hit_load_power_l2n",
            "sensor.hoymiles_hit_load_power_l3n",
            "sensor.hoymiles_night_protected_load_power",
            "sensor.hoymiles_night_protection_window_remaining",
            "sensor.hoymiles_protected_window_expected_load",
            "{% set upcoming_start = setting - buffer %}",
            "{% set upcoming_end = rising + buffer %}",
            "upcoming_end - upcoming_start",
            "sensor.hoymiles_rce_protected_home_energy",
            "state_attr('sun.sun', 'next_rising')",
            "state_attr('sun.sun', 'next_setting')",
            "sensor.hoymiles_hit_battery_capacity",
            "sensor.hoymiles_hit_ems_self_use_soc_readback",
            "sensor.hoymiles_hit_rce_optimized_plan",
            "sensor.hoymiles_rce_dynamic_minimum_soc",
            "sensor.hoymiles_rce_effective_minimum_soc",
            "hoymiles_rce_accounting_date:",
            "hoymiles_rce_grid_sell_checkpoint:",
            "hoymiles_rce_realized_controlled_export_store:",
            "hoymiles_rce_realized_natural_export_store:",
            "hoymiles_rce_realized_controlled_revenue_store:",
            "hoymiles_rce_realized_natural_revenue_store:",
            "hoymiles_rce_unclassified_export_store:",
            "id: hoymiles_rce_export_accounting",
            "unique_id: hoymiles_rce_self_use_baseline_export",
            "unique_id: hoymiles_rce_realized_controlled_export_today",
            "unique_id: hoymiles_rce_realized_natural_export_today",
            "unique_id: hoymiles_rce_realized_controlled_revenue_today",
            "unique_id: hoymiles_rce_realized_natural_revenue_today",
            "unique_id: hoymiles_rce_realized_revenue_today",
            "unique_id: hoymiles_rce_unclassified_export_today",
            "([grid / 1000, 0] | max)",
            "binary_sensor.hoymiles_rce_reserve_ready",
            "or is_state('binary_sensor.hoymiles_rce_reserve_ready', 'off')",
        )
        for marker in dynamic_reserve_markers:
            require(
                marker in package_text,
                f"Dynamic RCE reserve marker missing in {package_path.name}: {marker}",
            )
        require(
            'source_registers: "2129 + 2130 + 2131"' not in package_text
            and "sensor.hoymiles_hit_load_energy_use_l1n_today"
            not in package_text
            and "sensor.hoymiles_hit_load_energy_use_l2n_today"
            not in package_text
            and "sensor.hoymiles_hit_load_energy_use_l3n_today"
            not in package_text,
            f"Clean home-energy calculation regressed to inverter daily LOAD "
            f"counters in {package_path.name}",
        )
        require(
            "or not is_state(\n"
            "                       'binary_sensor.hoymiles_tariff_planned_charge_slot', 'on')"
            not in package_text,
            f"Tariff control still stops on a transient live-plan change in "
            f"{package_path.name}",
        )
        require(
            "[-grid / 1000, 0]" not in package_text,
            f"Grid export power still uses the reversed sign in {package_path.name}",
        )
        require(
            not re.search(
                r"device_class:\s*energy\s+state_class:\s*measurement",
                package_text,
            ),
            f"Energy helper uses invalid measurement state class in {package_path.name}",
        )

    rcm_optimizer_source = (
        COMPONENT / "rcm_optimizer.py"
    ).read_text(encoding="utf-8")
    rcm_sensor_source = (
        COMPONENT / "rcm_sensor.py"
    ).read_text(encoding="utf-8")
    for marker in (
        "expected_natural_headroom_kwh",
        "planned_grid_discharge_kwh",
        "pre_discharge_target_soc_percent",
        "pre_discharge_power_percent",
        "pre_discharge_ready",
        "protected_minimum_soc",
        "export_capacity_kw > 0.1",
    ):
        require(
            marker in rcm_optimizer_source,
            f"RCEm morning-discharge safety marker missing: {marker}",
        )
    require(
        "sensor.hoymiles_hit_maximum_discharge_current" in rcm_sensor_source
        and "minutes_to_risk" in rcm_sensor_source
        and "risk_day_offset" in rcm_sensor_source,
        "RCEm sensor lacks BMS/time inputs for safe morning discharge",
    )

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_pl = (ROOT / "README.pl.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    github_release_notes = (
        ROOT / "docs" / "releases" / f"v{manifest['version']}.md"
    ).read_text(encoding="utf-8")
    release_match = re.search(
        rf"^## \[{re.escape(manifest['version'])}\][^\n]*\n(.*?)(?=^## \[|\Z)",
        changelog,
        re.M | re.S,
    )
    require(
        release_match is not None,
        f"CHANGELOG lacks the {manifest['version']} release section",
    )
    release_notes = release_match.group(1)
    normalized_readme = " ".join(readme.split())
    require(
        readme.startswith(f"# {EXPECTED_PROJECT_NAME}\n")
        and readme_pl.startswith(f"# {EXPECTED_PROJECT_NAME}\n")
        and EXPECTED_DESCRIPTION in normalized_readme,
        "README project title or exact GitHub description is inconsistent",
    )
    translations_en = load_json(COMPONENT / "translations" / "en.json")
    translations_pl = load_json(COMPONENT / "translations" / "pl.json")
    require(
        translations_en.get("title") == EXPECTED_PROJECT_NAME
        and translations_pl.get("title") == EXPECTED_PROJECT_NAME,
        "Localized integration titles do not match the public project name",
    )
    require(
        "### User update steps / Kroki po aktualizacji" in release_notes,
        "Release changelog lacks the HACS-visible user update steps",
    )
    for update_step in (
        "1. **HACS:**",
        "2. **Home Assistant:**",
        "3. **ESP32 / ESPHome:**",
        "4. **Verification / Weryfikacja:**",
    ):
        require(
            update_step in release_notes,
            f"Release changelog lacks required user step: {update_step}",
        )
    release_procedure = (ROOT / "RELEASING.md").read_text(encoding="utf-8")
    require(
        "GitHub Release body visible in HACS" in release_procedure
        and "does not flash the ESP32" in release_procedure,
        "Release procedure does not require complete HACS/ESP32 instructions",
    )
    readme_images = [ROOT / "docs" / "images" / "dashboard-overview.png"]
    for image in readme_images:
        require(image.is_file(), f"Missing README image: {image.relative_to(ROOT)}")
        require(
            str(image.relative_to(ROOT)).replace("\\", "/") in readme,
            f"README does not reference {image.name}",
        )
        width, height = png_dimensions(image)
        require(
            width >= 1000 and height >= 700,
            f"README image is unexpectedly small: {image.name}",
        )
    require(
        "docs/QUICK_START.md" in readme
        and "docs/QUICK_START.md" in readme_pl
        and (ROOT / "docs" / "QUICK_START.md").is_file(),
        "English and Polish READMEs must expose the beginner quick-start guide",
    )
    require(
        "## User update steps / Kroki po aktualizacji" in github_release_notes
        and "ESP32 / ESPHome" in github_release_notes
        and "2064/2064" in github_release_notes,
        "GitHub Release notes are incomplete for HACS users",
    )
    require(
        "[English](README.md) · [Polski](README.pl.md)" in readme
        and "[English](README.md) · [Polski](README.pl.md)" in readme_pl,
        "README language switch is missing or inconsistent",
    )
    for documentation_text, language, documentation_markers in (
        (
            readme,
            "English",
            (
                "/releases/latest",
                "## Compatibility and requirements",
                "## Safety",
                "### RCE market-price optimization",
                "### Tariff-aware grid charging",
                "### Experimental RCEm 253 V+ voltage management",
                "### LiFePO4 battery balancing",
                "## Parallel inverter systems",
                "docs/AUTOMATION_TEST_REPORT.md",
            ),
        ),
        (
            readme_pl,
            "Polish",
            (
                "/releases/latest",
                "## Zgodność i wymagania",
                "## Bezpieczeństwo",
                "### Optymalizacja cen RCE",
                "### Automatyczne ładowanie taryfowe",
                "### Eksperymentalne zarządzanie napięciem RCEm 253 V+",
                "### Wyrównywanie baterii LiFePO4",
                "## Instalacje z falownikami połączonymi równolegle",
                "docs/AUTOMATION_TEST_REPORT.md",
            ),
        ),
    ):
        for documentation_marker in documentation_markers:
            require(
                documentation_marker in documentation_text,
                f"{language} README is missing documentation: {documentation_marker}",
            )
    require(
        "README.pl.md" in readme and len(readme_pl.split()) >= len(readme.split()) * 0.8,
        "Polish README is not a complete edition of the English documentation",
    )

    esphome_entry_files = [
        ROOT / "hoymiles-inverter.yaml",
        ROOT / "examples" / "esphome" / "hoymiles-hit-g3.yaml",
    ]
    required_esphome_packages = {
        f"packages/{path.name}"
        for path in (ROOT / "packages").glob("*.yaml")
        if not path.name.startswith("optional_")
    }
    workflow_source = (ROOT / ".github" / "workflows" / "validate.yml").read_text(
        encoding="utf-8"
    )
    firmware_ci_path = ROOT / "tools" / "esphome_verify_ci.yaml"
    firmware_ci_source = firmware_ci_path.read_text(encoding="utf-8")
    require(
        "firmware-compile:" in workflow_source
        and "github.event_name == 'workflow_dispatch'" in workflow_source
        and "startsWith(github.ref, 'refs/tags/v')" in workflow_source
        and '"esphome==2026.7.2"' in workflow_source
        and "esphome config tools/esphome_verify_ci.yaml" in workflow_source
        and "esphome compile tools/esphome_verify_ci.yaml" in workflow_source,
        "Release workflow lacks the pinned full ESPHome compile gate",
    )
    require(
        "CI-only full firmware fixture" in firmware_ci_source
        and 'wifi_ssid: "ci-placeholder-network"' in firmware_ci_source
        and 'wifi_password: "ci-placeholder-password"' in firmware_ci_source
        and 'ota_password: "ci-placeholder-ota"' in firmware_ci_source,
        "Firmware CI fixture must contain only documented placeholder credentials",
    )
    firmware_ci_packages = set(
        re.findall(r"!include ../(packages/[a-z0-9_]+\.yaml)", firmware_ci_source)
    )
    require(
        firmware_ci_packages == required_esphome_packages,
        "Firmware CI fixture does not compile the complete required package set",
    )
    for entry_file in esphome_entry_files:
        entry_text = entry_file.read_text(encoding="utf-8")
        require(
            "!include packages/" not in entry_text,
            f"Public ESPHome entry point uses local includes: {entry_file.name}",
        )
        require(
            f"url: https://github.com/{EXPECTED_REPOSITORY}"
            in entry_text,
            f"Public ESPHome entry point has no remote package: {entry_file.name}",
        )
        require(
            f"ref: v{compatible_firmware_version}" in entry_text,
            "ESPHome entry point is not pinned to the compatible firmware version: "
            f"{entry_file.name}",
        )
        require(
            "dashboard_import:" in entry_text
            and f"package_import_url: github://{EXPECTED_REPOSITORY}/"
            in entry_text
            and "import_full_config: true" in entry_text,
            f"ESPHome adoption/update metadata is missing in {entry_file.name}",
        )
        included_packages = set(
            re.findall(r"^\s*-\s+(packages/[a-z0-9_]+\.yaml)\s*$", entry_text, re.M)
        )
        require(
            included_packages == required_esphome_packages,
            f"ESPHome package list differs in {entry_file.name}",
        )
        require(
            entry_text.index("- packages/parallel_network.yaml")
            < entry_text.index("- packages/settings.yaml"),
            f"Parallel topology must load before EMS settings in {entry_file.name}",
        )

    active_repository_files = [
        ROOT / "README.md",
        ROOT / "README.pl.md",
        ROOT / "NOTICE",
        ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml",
        COMPONENT / "manifest.json",
        COMPONENT / "__init__.py",
        ROOT / "home_assistant" / "www" / "hoymiles-dashboard-strategy.js",
        ROOT / "home_assistant" / "www" / "hoymiles-rce-chart-card.js",
        RESOURCES / "www" / "hoymiles-dashboard-strategy.js",
        RESOURCES / "www" / "hoymiles-rce-chart-card.js",
        *esphome_entry_files,
    ]
    for active_repository_file in active_repository_files:
        require(
            LEGACY_REPOSITORY_SLUG
            not in active_repository_file.read_text(encoding="utf-8"),
            f"Active file still references the legacy repository slug: "
            f"{active_repository_file.relative_to(ROOT)}",
        )

    parallel_source = (ROOT / "packages" / "parallel_network.yaml").read_text(
        encoding="utf-8"
    )
    settings_source = (ROOT / "packages" / "settings.yaml").read_text(
        encoding="utf-8"
    )
    for marker in (
        "address: 6048",
        "address: 6049",
        'name: "Parallel Topology"',
        'name: "Parallel EMS Control Status"',
        'name: "Parallel Topology Readback Generation"',
        'name: "EMS Verified Hardware Readback Supported"',
        'name: "Direct Register Verified Readback Supported"',
        "count < 2 || count > 10",
        "return count >= 2 && count <= 10 ? 1.0f : 0.0f;",
        "Gotowe - broadcast EMS, odczyt Mastera",
    ):
        require(
            marker in parallel_source,
            f"Parallel topology marker missing: {marker}",
        )
    for marker in (
        "machine_type == 2",
        "machine_count < 2 || machine_count > 10",
        'name: "EMS Control Readback Generation"',
        'name: "GCF Control Readback Generation"',
        'name: "Battery Charge Power Readback Generation"',
        "id(ems_verified_hardware_readback_supported).state",
        "id(direct_register_verified_readback_supported).state",
        "id: ems_write_complete_block_4300_4306",
        "0x00, 0x10, 0x10, 0xCC, 0x00, 0x07, 0x0E",
        "id(modbus_1).send_raw(payload);",
        "create_write_multiple_command",
    ):
        require(
            marker in settings_source,
            f"Parallel EMS safety marker missing: {marker}",
        )
    require(
        settings_source.count("send_raw(payload)") == 1
        and settings_source.count("0x00, 0x10, 0x10, 0xCC") == 1,
        "Parallel EMS broadcast must have one canonical complete-block writer",
    )
    require(
        'name: "Hoymiles Direct Register Execution Ready"' in ems_package_source
        and "sensor.hoymiles_hit_direct_register_verified_readback_supported"
        in ems_package_source
        and "system_broadcast_with_master_fc03" in ems_package_source,
        "HA package does not separate broadcast EMS from direct registers",
    )
    require(
        "hoymiles_modbus_slave_" not in parallel_source
        and "hoymiles_modbus_slave_" not in settings_source,
        "External Modbus must not poll or write internal parallel Slave addresses",
    )

    overview_source = (ROOT / "packages" / "overview.yaml").read_text(
        encoding="utf-8"
    )

    def esphome_sensor_block(source: str, sensor_id: str) -> str:
        """Return one ESPHome sensor block for structural assertions."""
        marker = f"    id: {sensor_id}\n"
        start = source.index(marker)
        end = source.find("\n  - platform:", start)
        return source[start:] if end == -1 else source[start:end]

    overview_ids = (
        "pv_total_power_30001",
        "inv_active_power_30007",
        "battery_power_30009",
        "grid_total_active_power_30011",
        "load_active_power_30015",
        "battery_soc_30020",
    )
    for overview_id in overview_ids:
        overview_block = esphome_sensor_block(overview_source, overview_id)
        require(
            "update_interval: never" in overview_block
            and "force_update: true" in overview_block
            and "lambda:" not in overview_block,
            f"{overview_id} must be published only by physical FC03 callbacks",
        )

    pv_source = (ROOT / "packages" / "pv.yaml").read_text(encoding="utf-8")
    load_source = (ROOT / "packages" / "backup_load.yaml").read_text(
        encoding="utf-8"
    )
    grid_source = (ROOT / "packages" / "meters.yaml").read_text(
        encoding="utf-8"
    )
    battery_source = (ROOT / "packages" / "battery.yaml").read_text(
        encoding="utf-8"
    )
    physical_publications = {
        "pv_total_power_30001": (overview_source, pv_source),
        "inv_active_power_30007": (overview_source, load_source),
        "battery_power_30009": (overview_source, load_source),
        "grid_total_active_power_30011": (overview_source, grid_source),
        "load_active_power_30015": (overview_source, load_source),
        "battery_soc_30020": (overview_source, battery_source),
    }
    for overview_id, sources in physical_publications.items():
        marker = f"id({overview_id}).publish_state("
        require(
            all(marker in source for source in sources),
            f"{overview_id} lacks one single/Master physical publication path",
        )

    require(
        "bat_total_power_8546" not in load_source,
        "Parallel overview power must not use the signed 16-bit register 2162",
    )
    for marker in (
        "id(pv_total_power_8528)",
        "id(grid_total_active_power_1814)",
        "static_cast<uint32_t>(now_ms - grid_ms) <= 30000U",
        "static_cast<uint32_t>(now_ms - pv_ms) <= 30000U",
    ):
        require(
            marker in load_source,
            f"Parallel event-driven balance is missing input/coherence: {marker}",
        )
    require(
        "const float inverter_power =" in load_source
        and "x + id(grid_total_active_power_1814).state" in load_source,
        "Parallel inverter overview must equal LOAD plus grid export",
    )
    require(
        "inverter_power - id(pv_total_power_8528).state" in load_source,
        "Parallel battery overview must equal LOAD plus grid export minus PV",
    )
    require(
        "65 535 W" in overview_source
        and "nie pozwala wykryć zawinięcia do zera" in overview_source,
        "Parallel overview must document the U_WORD range limitation",
    )

    pv_system_block = esphome_sensor_block(
        pv_source,
        "pv_total_power_8528",
    )
    load_system_block = esphome_sensor_block(
        load_source,
        "load_power_total_8553",
    )
    grid_system_block = esphome_sensor_block(
        grid_source,
        "grid_total_active_power_1814",
    )
    require(
        "address: 2150" in pv_system_block
        and "value_type: U_WORD" in pv_system_block
        and "address: 2169" in load_system_block
        and "value_type: U_WORD" in load_system_block
        and "address: 1814" in grid_system_block
        and "value_type: S_DWORD" in grid_system_block,
        "Parallel overview balance input widths no longer match the documented map",
    )

    for asset in required_assets[:4]:
        text = asset.read_text(encoding="utf-8")
        require(
            not legacy_entity_pattern.search(text),
            f"Installation-specific entity id remains in {asset.name}",
        )
        require(
            not re.search(r"\bPV[56]\b", text, flags=re.IGNORECASE),
            f"Unsupported PV5/PV6 reference remains in {asset.name}",
        )

    polish_dashboard = required_assets[1].read_text(encoding="utf-8")
    english_dashboard = required_assets[0].read_text(encoding="utf-8")
    for dashboard_text, language in (
        (polish_dashboard, "Polish"),
        (english_dashboard, "English"),
    ):
        dashboard_lines = dashboard_text.splitlines()
        require(
            not re.search(
                r"^\s*-\s+(?:button|sensor|number|select)\.hoymiles_hit_[a-z0-9_]+\s*$",
                dashboard_text,
                re.M,
            ),
            f"{language} dashboard contains entity rows without short names",
        )
        for index, line in enumerate(dashboard_lines):
            entity_match = re.match(
                r"^(?P<indent>\s*)-\s+entity:\s+"
                r"(?:button|sensor|number|select)\.hoymiles_hit_[a-z0-9_]+\s*$",
                line,
            )
            if not entity_match:
                continue
            following = (
                dashboard_lines[index + 1]
                if index + 1 < len(dashboard_lines)
                else ""
            )
            require(
                following.startswith(
                    f"{entity_match.group('indent')}  name:"
                ),
                f"{language} dashboard entity row on line {index + 1} "
                "has no dashboard-only short name",
            )
        require(
            not re.search(
                r"^\s*name:\s*[\"']?Hoymiles Inverter\b",
                dashboard_text,
                re.M | re.I,
            ),
            f"{language} dashboard still displays the Hoymiles Inverter prefix",
        )
        require(
            "<table>" not in dashboard_text,
            f"{language} dashboard still contains non-clickable HTML tables",
        )
        require(
            "type: custom:hoymiles-aurora-energy-card" in dashboard_text,
            f"{language} dashboard does not use the Aurora live-energy card",
        )
        require(
            "sensor.hoymiles_hit_battery_current_inverter" in dashboard_text,
            f"{language} dashboard does not show inverter-side battery current",
        )
        require(
            "sensor.hoymiles_hit_battery_1_voltage" in dashboard_text,
            f"{language} dashboard does not show inverter-side battery voltage",
        )
        require(
            "button.hoymiles_hit_clear_fault" in dashboard_text,
            f"{language} dashboard lacks the Clear Fault button",
        )
        require(
            '<ha-alert alert-type="error">' not in dashboard_text,
            f"{language} dashboard still contains the removed red alert rows",
        )
        state_title = (
            "Stan systemu i łączność"
            if language == "Polish"
            else "System and connectivity"
        )
        alarm_title = (
            "Alarmy — szybki podgląd"
            if language == "Polish"
            else "Alarms — quick view"
        )
        start_section = dashboard_text.split("  - title: Start", 1)[1].split(
            "\n  - title:", 1
        )[0]
        require(
            state_title not in start_section and alarm_title not in start_section,
            f"{language} Start view still duplicates diagnostics/status cards",
        )
        require(
            f"path: {'sterowanie' if language == 'Polish' else 'control'}\n    icon: mdi:tune-variant\n    type: sidebar"
            in dashboard_text,
            f"{language} control view is not using the sidebar layout",
        )
        for entity_id in (
            "input_boolean.hoymiles_ems_push_notifications_enabled",
            "input_text.hoymiles_ems_push_notify_target",
            "sensor.hoymiles_ems_push_notification_status",
            "input_boolean.hoymiles_rce_dynamic_soc_enabled",
            "input_number.hoymiles_rce_soc_safety_margin",
            "input_text.hoymiles_solcast_forecast_today_entity",
            "input_text.hoymiles_solcast_forecast_tomorrow_entity",
            "input_select.hoymiles_rce_inverter_rated_power",
            "input_number.hoymiles_rce_fallback_daily_load",
            "sensor.hoymiles_hit_rce_optimized_plan",
            "sensor.hoymiles_solcast_forecast_today",
            "sensor.hoymiles_solcast_forecast_remaining_today",
            "sensor.hoymiles_solcast_forecast_tomorrow",
            "sensor.hoymiles_load_average_4_days",
            "sensor.hoymiles_night_load_average_4_days",
            "sensor.hoymiles_rce_protected_home_energy",
            "sensor.hoymiles_rce_dynamic_minimum_soc",
            "sensor.hoymiles_rce_self_use_baseline_export",
            "sensor.hoymiles_hit_grid_energy_sell_today",
            "sensor.hoymiles_rce_realized_controlled_export_today",
            "sensor.hoymiles_rce_realized_natural_export_today",
            "sensor.hoymiles_rce_unclassified_export_today",
            "sensor.hoymiles_rce_realized_controlled_revenue_today",
            "sensor.hoymiles_rce_realized_natural_revenue_today",
            "sensor.hoymiles_rce_realized_revenue_today",
            "sensor.hoymiles_rce_day",
            "sensor.hoymiles_rce_day_tomorrow",
            "input_boolean.hoymiles_tariff_charge_enabled",
            "input_select.hoymiles_tariff_type",
            "input_number.hoymiles_tariff_g11_price",
            "input_number.hoymiles_tariff_low_price",
            "sensor.hoymiles_hit_tariff_charge_plan",
            "binary_sensor.hoymiles_tariff_planned_charge_slot",
            "sensor.hoymiles_tariff_target_soc",
            "sensor.hoymiles_tariff_planned_grid_import",
            "sensor.hoymiles_tariff_estimated_savings",
            "sensor.hoymiles_tariff_grid_charge_energy_daily",
            "sensor.hoymiles_tariff_savings_daily",
            "input_boolean.hoymiles_battery_balancing_enabled",
            "input_number.hoymiles_battery_balancing_interval_days",
            "input_number.hoymiles_battery_balancing_hold_hours",
            "sensor.hoymiles_battery_balancing_status",
            "sensor.hoymiles_battery_balancing_next_run",
            "timer.hoymiles_battery_balancing_hold",
            "sensor.hoymiles_battery_balancing_slow_charge_power",
        ):
            require(
                entity_id in dashboard_text,
                f"{language} dashboard lacks dynamic RCE entity {entity_id}",
            )
        require(
            "path: ladowanie-taryfowe" in dashboard_text,
            f"{language} dashboard lacks the tariff charging view",
        )
        require(
            "https://github.com/BJReplay/ha-solcast-solar" in dashboard_text,
            f"{language} dashboard does not document the Solcast dependency",
        )
    require(
        "Wyczyść alarmy falownika" in polish_dashboard,
        "Polish dashboard lacks the localized Clear Fault name",
    )
    require(
        "Clear Fault" in english_dashboard,
        "English dashboard lacks the localized Clear Fault name",
    )
    require(
        'name: "Docelowy SOC ładowania z sieci"' in polish_dashboard,
        "Polish dashboard lacks the localized Force Charge SOC name",
    )
    require(
        'name: "Maksymalna moc rozładowania do sieci"' in polish_dashboard,
        "Polish dashboard lacks the localized Maximum Discharge Power name",
    )
    for dashboard_text, language in (
        (polish_dashboard, "Polish"),
        (english_dashboard, "English"),
    ):
        for entity_id in (
            "sensor.hoymiles_hit_grid_voltage_l1",
            "sensor.hoymiles_hit_grid_voltage_l2",
            "sensor.hoymiles_hit_grid_voltage_l3",
            "sensor.hoymiles_hit_inverter_grid_frequency",
        ):
            require(
                entity_id in dashboard_text,
                f"{language} dashboard lacks grid diagnostic entity {entity_id}",
            )
    require(
        "potrzebna domowi" not in english_dashboard,
        "English dashboard contains an untranslated RCE reserve explanation",
    )
    for dashboard_text, language in (
        (polish_dashboard, "Polish"),
        (english_dashboard, "English"),
    ):
        for entity_id in (
            "sensor.hoymiles_hit_load_power_l1n",
            "sensor.hoymiles_hit_load_power_l2n",
            "sensor.hoymiles_hit_load_power_l3n",
            "sensor.hoymiles_hit_load_power_total",
        ):
            require(
                entity_id in dashboard_text,
                f"{language} dashboard lacks live load-power entity {entity_id}",
            )
        for entity_id in (
            "sensor.hoymiles_rce_revenue_daily",
            "sensor.hoymiles_rce_revenue_weekly",
            "sensor.hoymiles_rce_revenue_monthly",
            "sensor.hoymiles_rce_revenue_yearly",
            "sensor.hoymiles_rce_grid_export_energy_daily",
            "sensor.hoymiles_rce_grid_export_energy_weekly",
            "sensor.hoymiles_rce_grid_export_energy_monthly",
            "sensor.hoymiles_rce_grid_export_energy_yearly",
        ):
            require(
                entity_id in dashboard_text,
                f"{language} dashboard lacks profit-period entity {entity_id}",
            )
        for entity_id in (
            "select.hoymiles_hit_generation_control_function",
            "number.hoymiles_hit_maximum_export_power_limit",
            "select.hoymiles_hit_gen_port_mode",
            "sensor.hoymiles_hit_overview_generator_active_power",
        ):
            require(
                entity_id in dashboard_text,
                f"{language} dashboard lacks GCF/GEN entity {entity_id}",
            )
        require(
            "grid_input_power_limitation_valley" not in dashboard_text,
            f"{language} dashboard still contains the removed Valley setting",
        )
    require(
        'id: replenish_power_310' in settings_source
        and 'max_value: 1000' in settings_source
        and 'return x > 1000.0f ? 1000.0f' in settings_source,
        "Low-SOC grid-charge register 310 is not protected by the 1000 W limit",
    )
    for register_id in (
        "battery_max_charge_power_306",
        "battery_max_discharge_power_307",
    ):
        register_offset = settings_source.index(f"id: {register_id}")
        register_block = settings_source[register_offset : register_offset + 1300]
        require(
            "lambda: return x * 0.1f;" in register_block
            and "return safe * 10.0f;" in register_block,
            f"Register {register_id} does not use the required 0.1% scale",
        )
    for dashboard_text, language, topology_name, status_name in (
        (
            polish_dashboard,
            "Polish",
            'name: "Topologia sieci"',
            'name: "Gotowość sterowania EMS"',
        ),
        (
            english_dashboard,
            "English",
            'name: "Network topology"',
            'name: "EMS control readiness"',
        ),
    ):
        require(
            "sensor.hoymiles_hit_parallel_topology" in dashboard_text
            and "sensor.hoymiles_hit_parallel_ems_control_status" in dashboard_text
            and topology_name in dashboard_text
            and status_name in dashboard_text,
            f"{language} dashboard lacks localized parallel EMS diagnostics",
        )

    english_assets = [required_assets[0], required_assets[2]]
    polish_characters = re.compile(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")
    for asset in english_assets:
        visible_lines = [
            line
            for line in asset.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        ]
        visible_text = "\n".join(visible_lines).replace(
            "sensor.solcast_pv_forecast_prognoza_na_jutro",
            "sensor.solcast_pv_forecast_localized_tomorrow",
        )
        require(
            not polish_characters.search(visible_text),
            f"Visible Polish text remains in English asset {asset.name}",
        )

    for python_file in COMPONENT.glob("*.py"):
        compile(
            python_file.read_text(encoding="utf-8"),
            str(python_file),
            "exec",
        )

    localization = load_localization_module()
    require(
        localization.localized_text_state("Praca z siecią", "en") == "On-grid operation",
        "English text state localization failed",
    )
    require(
        localization.localized_text_state("Praca z siecią", "pl") == "Praca z siecią",
        "Polish text state localization failed",
    )
    require(
        localization.localized_text_state("Brak błędu", "en") == "No error",
        "English fault state localization failed",
    )
    require(
        localization.localized_text_state(
            "Gotowe - sterowanie bezpośrednie", "en"
        )
        == "Ready - direct control",
        "English parallel EMS state localization failed",
    )
    require(
        localization.localized_text_state(
            "Zablokowane - ESP32 podłączone do Slave", "pl"
        )
        == "Zablokowane - ESP32 podłączone do Slave",
        "Polish parallel EMS state localization failed",
    )
    validate_fresh_asset_install()
    validate_frontend_asset_failure_isolation(init_source)

    for image_name in (
        "icon.png",
        "dark_icon.png",
        "icon@2x.png",
        "dark_icon@2x.png",
        "logo.png",
        "dark_logo.png",
    ):
        image_path = COMPONENT / "brand" / image_name
        require(image_path.is_file(), f"Missing brand asset {image_name}")
        width, height = png_dimensions(image_path)
        require(width >= 128 and height >= 128, f"{image_name} is too small")
    for image_name, expected_size in (
        ("icon.png", 256),
        ("dark_icon.png", 256),
        ("icon@2x.png", 512),
        ("dark_icon@2x.png", 512),
    ):
        width, height = png_dimensions(COMPONENT / "brand" / image_name)
        require(
            (width, height) == (expected_size, expected_size),
            f"{image_name} must be {expected_size}x{expected_size}",
        )

    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    license_policy = (ROOT / "LICENSE_POLICY.md").read_text(encoding="utf-8")
    notice_text = (ROOT / "NOTICE").read_text(encoding="utf-8")
    contribution_text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    pr_template_text = (
        ROOT / ".github" / "pull_request_template.md"
    ).read_text(encoding="utf-8")
    codeowners_text = (ROOT / ".github" / "CODEOWNERS").read_text(
        encoding="utf-8"
    )
    workflow_text = (ROOT / ".github" / "workflows" / "validate.yml").read_text(
        encoding="utf-8"
    )
    require(
        license_text.startswith("MIT License\n\nCopyright (c) 2026 Kaluzaburza")
        and "Permission is hereby granted, free of charge" in license_text
        and 'THE SOFTWARE IS PROVIDED "AS IS"' in license_text,
        "MIT license text is missing or incomplete",
    )
    require(
        hashlib.sha256(
            (ROOT / "LICENSE").read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        == "fa0bb01cef85cd8e77d7930efae10d9c93812cfa6e4878f3d7d57ad23c224290",
        "LICENSE is not the reviewed MIT text",
    )
    require(
        "[MIT License](LICENSE)" in license_policy
        and "Private and commercial use" in license_policy
        and "Oprogramowanie jest udostępniane bez gwarancji" in license_policy
        and "PolyForm" not in license_policy,
        "License policy does not describe the current MIT terms cleanly",
    )
    require(
        notice_text.startswith("Copyright (c) 2026 Kaluzaburza")
        and "Licensed under the MIT License" in notice_text,
        "Required copyright notice is missing",
    )
    require(
        "[MIT License](LICENSE)" in contribution_text
        and "commercial purposes" in contribution_text
        and "Contribution Certificate 1.0" in contribution_text
        and "Signed-off-by:" in contribution_text,
        "Contribution rights and sign-off terms are incomplete",
    )
    require(
        "I accept [CONTRIBUTING.md]" in pr_template_text
        and "Signed-off-by:" in pr_template_text
        and "* @Kaluzaburza" in codeowners_text,
        "Pull-request rights confirmation or CODEOWNERS is missing",
    )
    for test_command in (
        "python tools/test_tariff_profiles.py",
        "python tools/test_tariff_optimizer.py",
        "python tools/test_rcm_history.py",
        "python tools/test_rcm_optimizer.py",
        "python tools/test_diagnostic_analyzer.py",
        "python tools/test_optimizer_startup_contract.py",
        "python tools/test_automation_matrix.py",
        "python tools/test_automation_matrix.py --exhaustive",
    ):
        require(test_command in workflow_text, f"CI does not run {test_command}")
    require(
        "validate-hacs:" in workflow_text
        and "HACS validation" in workflow_text
        and "continue-on-error" not in workflow_text
        and "ignore:" not in workflow_text,
        "Official HACS validation must be mandatory and unignored",
    )

    node_executable = os.environ.get("HOYMILES_NODE_EXECUTABLE") or shutil.which(
        "node"
    )
    require(
        node_executable is not None,
        "Node.js is required for managed frontend validation",
    )
    with tempfile.TemporaryDirectory(
        prefix="hoymiles_rev28_frontend_fixture_"
    ) as temporary:
        historical_root = Path(temporary)
        build_historical_rev28_frontend_fixture(historical_root)
        historical_environment = os.environ.copy()
        historical_environment["HOYMILES_UI_TEST_ROOT"] = str(historical_root)
        for frontend_test_name in (
            "validate_rce_card.js",
            "test_supervisor_aurora_ui_contract.js",
        ):
            frontend_test = historical_root / "tools" / frontend_test_name
            completed = subprocess.run(
                [node_executable, str(frontend_test)],
                cwd=historical_root,
                env=historical_environment,
                check=False,
                capture_output=True,
                text=True,
            )
            require(
                completed.returncode == 0,
                f"Historical frontend validation failed: {frontend_test.name}\n"
                f"{completed.stdout}{completed.stderr}",
            )
        validate_ui_count_self_tests(node_executable, historical_root)
        visual_mutations_detected = validate_rev28_visual_mutations(
            node_executable,
            historical_root,
        )

    print(f"HACS layout: OK ({len(integration_dirs)} integration)")
    print("Historical REV28 fixture gate: OK (32-path frozen branch)")
    print("Committed AP-1 baseline gate: OK (16-path task, 40-path branch)")
    print(
        "Current AP-1E gate: OK "
        f"({current_ap1e_gate}; 4-path correction, 18-path task, 41-path branch)"
    )
    print(f"Manifest: OK (version {manifest['version']})")
    print(f"Localized entities: {len(catalog)} (English and Polish)")
    print("Bundled dashboards/EMS assets: OK")
    print("HACS-visible user update instructions: OK")
    print("README screenshots: OK")
    print("Public ESPHome remote packages: OK")
    print("Python syntax: OK")
    print("Text-state localization: OK")
    print("Fresh PL/EN asset installation: OK")
    print("Brand assets: OK")
    print("MIT/OSI license and current license documentation: OK")
    print("Contribution rights, sign-off and CODEOWNERS: OK")
    print("RCE/tariff/RCEm CI regression matrix: OK")
    print("Managed frontend validators: OK (historical REV28 fixture)")
    print(
        "Revision 28 visual mutations: "
        f"{visual_mutations_detected}/12 detected, 0 survivors"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
