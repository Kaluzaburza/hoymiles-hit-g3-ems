"""Install optional dashboard and EMS assets into Home Assistant config."""

from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from homeassistant.components.lovelace.const import (
    CONF_RESOURCE_TYPE_WS,
    LOVELACE_DATA,
    MODE_STORAGE,
)
from homeassistant.const import ATTR_EDITABLE, CONF_ID, CONF_TYPE, CONF_URL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    EMS_PACKAGE_VERSION,
    EMS_PACKAGE_VERSION_ENTITY,
    VERSION,
)


_LOGGER = logging.getLogger(__name__)
RESOURCE_ROOT = Path(__file__).with_name("resources")
CATALOG_PATH = Path(__file__).with_name("entity_catalog.json")
LEGACY_ENTITY_BACKUP_SUFFIX = ".pre-stable-entity-ids.bak"
EMS_PACKAGE_RELATIVE_PATH = "packages/hoymiles_ems_scheduler.yaml"
EMS_PACKAGE_BACKUP_SUFFIX = ".pre-ems-supervisor-1b3.bak"
SUPERVISOR_HELPER_COLLISION_CATEGORY = "supervisor_helper_collision"
_INSTALL_LOCK_DATA_KEY = f"_{DOMAIN}_asset_install_lock"
_SUPERVISOR_HELPERS = (
    (
        "input_select.hoymiles_ems_supervisor_mode",
        "input_select",
        "hoymiles_ems_supervisor_mode",
    ),
    (
        "input_select.hoymiles_ems_supervisor_profile",
        "input_select",
        "hoymiles_ems_supervisor_profile",
    ),
    (
        "input_boolean.hoymiles_ems_supervisor_allow_rce",
        "input_boolean",
        "hoymiles_ems_supervisor_allow_rce",
    ),
    (
        "input_boolean.hoymiles_ems_supervisor_allow_tariff",
        "input_boolean",
        "hoymiles_ems_supervisor_allow_tariff",
    ),
    (
        "input_boolean.hoymiles_ems_supervisor_allow_rcm",
        "input_boolean",
        "hoymiles_ems_supervisor_allow_rcm",
    ),
)
_SUPERVISOR_OBJECT_IDS = frozenset(item[2] for item in _SUPERVISOR_HELPERS)
_HELPER_STORE_CONTRACTS = {
    "input_select": (1, frozenset({1, 2})),
    "input_boolean": (1, frozenset({1})),
}


class HelperClassification(StrEnum):
    """Closed live classification for one canonical Supervisor helper."""

    ABSENT = "ABSENT"
    EXPECTED_YAML = "EXPECTED_YAML"
    UI_STORAGE_COLLISION = "UI_STORAGE_COLLISION"
    UNVERIFIABLE = "UNVERIFIABLE"


class SchedulerFilesystemAction(StrEnum):
    """Closed outcome of the scheduler filesystem transaction."""

    BLOCKED_COLLISION = "BLOCKED_COLLISION"
    CURRENT = "CURRENT"
    INSTALLED_FRESH = "INSTALLED_FRESH"
    REPLACED_EXISTING = "REPLACED_EXISTING"
    PRESERVED_MODIFIED = "PRESERVED_MODIFIED"
    ABORTED_DESTINATION_CHANGED = "ABORTED_DESTINATION_CHANGED"
    FAILED_AFTER_REPLACE = "FAILED_AFTER_REPLACE"


class RollbackAction(StrEnum):
    """Closed outcome of a postflight scheduler rollback."""

    NOT_NEEDED = "NOT_NEEDED"
    RESTORED_OLD = "RESTORED_OLD"
    REMOVED_FRESH = "REMOVED_FRESH"
    FAILED_CONCURRENT_CHANGE = "FAILED_CONCURRENT_CHANGE"


class DestinationAttestationAction(StrEnum):
    """Closed outcome of the final scheduler destination attestation."""

    NOT_REQUIRED = "NOT_REQUIRED"
    VERIFIED = "VERIFIED"
    ABORTED_DESTINATION_CHANGED = "ABORTED_DESTINATION_CHANGED"


@dataclass(frozen=True, slots=True)
class FileIdentity:
    """Stable bounded identity captured without following a path symlink."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    mode: int
    link_count: int


@dataclass(frozen=True, slots=True)
class LiveHelperFact:
    """Immutable public-API facts for one canonical helper."""

    entity_id: str
    domain: str
    object_id: str
    classification: HelperClassification
    registry_entity_id: str | None
    reason_code: str


@dataclass(frozen=True, slots=True)
class SchedulerInstallAuthorization:
    """Immutable collision decision passed to the filesystem executor."""

    helper_facts: tuple[LiveHelperFact, ...]
    disk_collision_ids: tuple[str, ...]
    disk_verifiable: bool
    package_marker_current: bool
    allowed: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """Bounded file state used at transaction boundaries."""

    path: Path
    existed: bool
    sha256: str | None
    identity: FileIdentity | None


@dataclass(frozen=True, slots=True)
class SchedulerFilesystemResult:
    """Immutable result of the scheduler filesystem transaction."""

    action: SchedulerFilesystemAction
    source_hash: str
    before: FileSnapshot
    after: FileSnapshot
    persistent_backup: Path | None
    transaction_rollback: Path | None
    transaction_rollback_snapshot: FileSnapshot | None
    scheduler_metadata_candidate: str | None
    physically_written: bool
    error_code: str | None


@dataclass(frozen=True, slots=True)
class AssetFilesystemResult:
    """Immutable executor result for all managed filesystem assets."""

    written_paths: tuple[Path, ...]
    non_scheduler_hashes: tuple[tuple[str, str], ...]
    scheduler: SchedulerFilesystemResult


@dataclass(frozen=True, slots=True)
class RollbackResult:
    """Immutable postflight rollback result."""

    action: RollbackAction
    resulting_snapshot: FileSnapshot
    error_code: str | None


@dataclass(frozen=True, slots=True)
class DestinationAttestationResult:
    """Immutable result of the final pre-metadata filesystem check."""

    action: DestinationAttestationAction
    resulting_snapshot: FileSnapshot
    error_code: str | None


@dataclass(frozen=True, slots=True)
class _LiveHelperSnapshot:
    helper_facts: tuple[LiveHelperFact, ...]
    package_marker_current: bool
    verifiable: bool


@dataclass(frozen=True, slots=True)
class _DiskHelperEvidence:
    collision_ids: tuple[str, ...]
    verifiable: bool
    reason_code: str


class _FilesystemContractError(OSError):
    """Bounded internal filesystem-contract failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


MODIFIED_EMS_PACKAGE_INSTRUCTIONS = {
    "pl": (
        "Wykryto lokalnie zmodyfikowany "
        "`/config/packages/hoymiles_ems_scheduler.yaml`; integracja go nie "
        "nadpisała. Wykonaj własną kopię pliku, uruchom "
        "`hoymiles_hit_modbus.install_assets` z `overwrite: true`, wykonaj "
        "`ha core check` i uruchom Home Assistant ponownie. Ta akcja "
        "zastępuje cały zmodyfikowany pakiet; po restarcie ponownie nanieś "
        "tylko świadome zmiany użytkownika."
    ),
    "en": (
        "A locally modified `/config/packages/hoymiles_ems_scheduler.yaml` "
        "was detected and was not overwritten. Back up the file, run "
        "`hoymiles_hit_modbus.install_assets` with `overwrite: true`, run "
        "`ha core check`, and restart Home Assistant. This action replaces "
        "the entire modified package; after restart, reapply only the "
        "intentional user changes."
    ),
}
SUPERVISOR_HELPER_COLLISION_INSTRUCTIONS = {
    "pl": (
        "Wykryto kolizję helpera UI Nadzorcy EMS albo nie można było jej "
        "bezpiecznie wykluczyć; integracja nie zmieniła "
        "`/config/packages/hoymiles_ems_scheduler.yaml`. Usuń albo przemianuj "
        "kolidujący helper UI, ponów instalację zasobów, wykonaj "
        "`ha core check` i uruchom Home Assistant ponownie."
    ),
    "en": (
        "An EMS Supervisor UI helper collision was detected or could not be "
        "safely ruled out; the integration did not change "
        "`/config/packages/hoymiles_ems_scheduler.yaml`. Remove or rename the "
        "colliding UI helper, repeat asset installation, run `ha core check`, "
        "and restart Home Assistant."
    ),
}
UNSAFE_EMS_PACKAGE_BACKUP_INSTRUCTIONS = {
    "pl": (
        "Istniejąca kopia "
        "`/config/packages/hoymiles_ems_scheduler.yaml"
        ".pre-ems-supervisor-1b3.bak` nie odpowiada dokładnym bajtom "
        "bieżącego pakietu, dlatego pakiet nie został zmieniony. Najpierw "
        "zachowaj i sprawdź istniejącą kopię pod własną nową nazwą poza "
        "stałą ścieżką, następnie przenieś stałą kopię z tej ścieżki, ponów "
        "`hoymiles_hit_modbus.install_assets` z `overwrite: true`, wykonaj "
        "`ha core check` i uruchom Home Assistant ponownie. Integracja nigdy "
        "nie nadpisuje istniejącej kopii."
    ),
    "en": (
        "The existing "
        "`/config/packages/hoymiles_ems_scheduler.yaml"
        ".pre-ems-supervisor-1b3.bak` does not match the exact current "
        "package bytes, so the package was not changed. First preserve and "
        "inspect the existing backup under your own new name outside the "
        "fixed path, then move it away from the fixed path, repeat "
        "`hoymiles_hit_modbus.install_assets` with `overwrite: true`, run "
        "`ha core check`, and restart Home Assistant. The integration never "
        "overwrites an existing backup."
    ),
}
DESTINATION_CHANGED_INSTRUCTIONS = {
    "pl": (
        "Pakiet EMS zmienił się podczas instalacji, dlatego integracja nie "
        "nadpisała nowszych bajtów ani nie zaktualizowała metadanych. Sprawdź "
        "plik, ponów instalację, wykonaj `ha core check` i uruchom Home "
        "Assistant ponownie."
    ),
    "en": (
        "The EMS package changed during installation, so the integration did "
        "not overwrite the newer bytes or advance metadata. Inspect the file, "
        "repeat installation, run `ha core check`, and restart Home Assistant."
    ),
}
ENTITY_ID_PATTERN = re.compile(
    r"\b(button|sensor|number|select)\.([a-z0-9_]+)\b"
)
ASSET_STORAGE_VERSION = 1
ASSET_STORAGE_KEY = f"{DOMAIN}.assets"
LOVELACE_RESOURCES_KEY = "lovelace_resources"
LOVELACE_STORAGE_PREFIX = "lovelace."
ZEBRA_CARD_TYPE = "custom:hoymiles-zebra-entities-card"
FRONTEND_ASSET_REVISION = 28
FRONTEND_STATIC_ROUTE = "static-r2"
FRONTEND_RESOURCE_URL = (
    "/local/hoymiles-rce-chart-card.js"
    f"?v={VERSION}.{FRONTEND_ASSET_REVISION}"
)
FRONTEND_BOOTSTRAP_URL = (
    "/local/hoymiles-dashboard-strategy.js"
    f"?v={VERSION}.{FRONTEND_ASSET_REVISION}"
)
LOCAL_FRONTEND_ASSETS = (
    "hoymiles-rce-chart-card.js",
    "hoymiles-dashboard-strategy.js",
    "dashboard_hoymiles_en.json",
    "dashboard_hoymiles_pl.json",
    "hoymiles-inverter.png",
)
MANAGED_FRONTEND_RESOURCE_PATHS = {
    "/local/hoymiles-rce-chart-card.js",
    "/local/hoymiles-dashboard-strategy.js",
    f"/api/{DOMAIN}/static/hoymiles-rce-chart-card.js",
    f"/api/{DOMAIN}/static/hoymiles-dashboard-strategy.js",
    f"/api/{DOMAIN}/{FRONTEND_STATIC_ROUTE}/hoymiles-rce-chart-card.js",
    f"/api/{DOMAIN}/{FRONTEND_STATIC_ROUTE}/hoymiles-dashboard-strategy.js",
}
HOYMILES_DASHBOARD_MARKERS = (
    "hoymiles_hit_overview_pv_total_power",
    "hoymiles_hit_overview_battery_power",
)
LEGACY_INVERTER_IMAGE_PATHS = {
    f"/api/{DOMAIN}/static/hoymiles-inverter.png",
    f"/api/{DOMAIN}/{FRONTEND_STATIC_ROUTE}/hoymiles-inverter.png",
}
INVERTER_IMAGE_PATH = "/local/hoymiles-inverter.png"
RCE_LOAD_ROW_LABELS = {
    "pl": {
        "sensor.hoymiles_actual_load_energy_today": (
            "Rzeczywiste zużycie odbiorników dzisiaj"
        ),
        "sensor.hoymiles_rce_pv_self_consumption_today": (
            "PV → odbiorniki — rejestr diagnostyczny"
        ),
        "sensor.hoymiles_rce_battery_to_load_today": (
            "Energia oddana przez baterię — diagnostycznie"
        ),
        "sensor.hoymiles_rce_grid_to_load_today": (
            "Energia pobrana z sieci — diagnostycznie"
        ),
    },
    "en": {
        "sensor.hoymiles_actual_load_energy_today": (
            "Actual load consumption today"
        ),
        "sensor.hoymiles_rce_pv_self_consumption_today": (
            "PV to load — diagnostic register"
        ),
        "sensor.hoymiles_rce_battery_to_load_today": (
            "Battery energy output — diagnostic"
        ),
        "sensor.hoymiles_rce_grid_to_load_today": (
            "Grid energy input — diagnostic"
        ),
    },
}

# v1.2.0 was the last release without managed-asset metadata. These checksums
# let the first newer release upgrade untouched files while preserving user
# modifications.
LEGACY_MANAGED_HASHES: dict[str, set[str]] = {
    "dashboard_hoymiles.yaml": {
        "86d43b9126b16e1fcb710e298ac80f8793e9a08e377105c6fa1c26c96e8a5d7f",
        "1cdf745154d565ce2dddb8c8a64c96075a1fac813171872445e716521199d21a",
    },
    "packages/hoymiles_ems_scheduler.yaml": {
        "6df876b47f18223ce905e0cc052921393325703f1ecef31d013dbe1aec237750",
        "82d3250cf87b316d7eb95e3ed1939e6bdf8f2845c17e9549f1b7d270490cf09a",
        "9846bfe0d0e9f8f707db7b3eb5b30fd349b663f8fb1c3777b460ef62696026a2",
        "b76ba6a2a9f94d307d1582822101b2fc0951868ba7319394ce0886ee9fe9e07d",
    },
    "www/hoymiles-rce-chart-card.js": {
        "bdc80c03d40d811835697f4d5c126ec90a8a6f2c59be9bdd29de1c05b96f09a6",
    },
    "www/hoymiles-inverter.png": {
        "4531e85081e78cf94dee82dbde75b2f931860886457ce9800f546afb4a3b3d15",
    },
}


def _stable_entity_id_map() -> dict[tuple[str, str], str]:
    """Return source object ids mapped to stable integration entity ids."""
    with CATALOG_PATH.open(encoding="utf-8") as catalog_file:
        catalog = json.load(catalog_file)
    return {
        (record["domain"], record["source_object_id"]): (
            f"{record['domain']}.hoymiles_hit_{record['translation_key']}"
        )
        for record in catalog
    }


def _migrate_legacy_entity_ids(path: Path) -> bool:
    """Replace device-name-dependent ids while preserving the user asset."""
    if not path.is_file():
        return False

    original = path.read_text(encoding="utf-8")
    stable_ids = _stable_entity_id_map()

    def replace(match: re.Match[str]) -> str:
        domain, object_id = match.groups()
        if "hoymiles_inverter" not in object_id:
            return match.group(0)
        for (candidate_domain, source_object_id), stable_id in stable_ids.items():
            if candidate_domain != domain:
                continue
            if object_id == source_object_id or object_id.endswith(
                f"_{source_object_id}"
            ):
                return stable_id
        return match.group(0)

    migrated = ENTITY_ID_PATTERN.sub(replace, original)
    if migrated == original:
        return False

    backup = path.with_name(f"{path.name}{LEGACY_ENTITY_BACKUP_SUFFIX}")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_text(migrated, encoding="utf-8")
    return True


def _sha256(path: Path) -> str:
    """Return a file SHA-256 digest."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identity_from_stat(info: os.stat_result) -> FileIdentity:
    """Create the exact bounded identity used by transaction comparisons."""
    return FileIdentity(
        device=int(info.st_dev),
        inode=int(info.st_ino),
        size=int(info.st_size),
        mtime_ns=int(info.st_mtime_ns),
        mode=int(info.st_mode),
        link_count=int(info.st_nlink),
    )


def _same_inode(first: FileIdentity, second: FileIdentity) -> bool:
    """Return whether two identities address the same filesystem object."""
    return first.device == second.device and first.inode == second.inode


def _read_fd_bytes(descriptor: int) -> bytes:
    """Read all bytes from a descriptor without reopening its path."""
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(descriptor, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _open_read_flags() -> int:
    """Return portable read flags with no-follow protection when available."""
    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _capture_regular_file(
    path: Path,
    *,
    require_single_link: bool,
    error_code: str,
) -> tuple[FileSnapshot, bytes]:
    """Capture stable bytes and identity using lstat/open/fstat/no-follow."""
    try:
        before_stat = os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError as err:
        raise _FilesystemContractError(error_code) from err
    if not stat.S_ISREG(before_stat.st_mode):
        raise _FilesystemContractError(error_code)
    before_identity = _identity_from_stat(before_stat)
    if require_single_link and before_identity.link_count != 1:
        raise _FilesystemContractError(error_code)

    descriptor = -1
    try:
        descriptor = os.open(path, _open_read_flags())
        first_identity = _identity_from_stat(os.fstat(descriptor))
        if first_identity != before_identity:
            raise _FilesystemContractError(error_code)
        data = _read_fd_bytes(descriptor)
        second_identity = _identity_from_stat(os.fstat(descriptor))
        if second_identity != first_identity or len(data) != second_identity.size:
            raise _FilesystemContractError(error_code)
    except _FilesystemContractError:
        raise
    except OSError as err:
        raise _FilesystemContractError(error_code) from err
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    digest = hashlib.sha256(data).hexdigest()
    return FileSnapshot(path, True, digest, second_identity), data


def _bounded_path_snapshot(path: Path) -> FileSnapshot:
    """Return the strongest bounded no-follow snapshot available for a result."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return FileSnapshot(path, False, None, None)
    except OSError:
        return FileSnapshot(path, True, None, None)
    identity = _identity_from_stat(info)
    if not stat.S_ISREG(info.st_mode):
        return FileSnapshot(path, True, None, identity)
    try:
        snapshot, _ = _capture_regular_file(
            path,
            require_single_link=False,
            error_code="snapshot_unverifiable",
        )
    except (FileNotFoundError, _FilesystemContractError):
        return FileSnapshot(path, True, None, identity)
    return snapshot


def _unlink_owned_path(path: Path, identity: FileIdentity) -> bool:
    """Unlink only the exact inode created by this transaction."""
    try:
        current = _identity_from_stat(os.lstat(path))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if not stat.S_ISREG(current.mode) or not _same_inode(current, identity):
        return False
    try:
        os.unlink(path)
    except OSError:
        return False
    return True


def _write_all_fd(descriptor: int, data: bytes) -> None:
    """Write every byte through the exclusive descriptor."""
    view = memoryview(data)
    offset = 0
    while offset < len(view):
        written = os.write(descriptor, view[offset:])
        if written <= 0:
            raise _FilesystemContractError("temporary_partial_write")
        offset += written


def _create_verified_temp(
    parent: Path,
    *,
    prefix: str,
    suffix: str,
    data: bytes,
    expected_hash: str,
) -> tuple[Path, FileSnapshot]:
    """Create, fsync and twice verify a random exclusive regular file."""
    descriptor = -1
    temporary: Path | None = None
    owned_identity: FileIdentity | None = None
    completed = False
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=parent,
            prefix=prefix,
            suffix=suffix,
        )
        temporary = Path(temporary_name)
        owned_identity = _identity_from_stat(os.fstat(descriptor))
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        _write_all_fd(descriptor, data)
        os.fsync(descriptor)
        descriptor_bytes = _read_fd_bytes(descriptor)
        descriptor_identity = _identity_from_stat(os.fstat(descriptor))
        owned_identity = descriptor_identity
        if (
            descriptor_bytes != data
            or hashlib.sha256(descriptor_bytes).hexdigest() != expected_hash
            or descriptor_identity.size != len(data)
            or descriptor_identity.link_count != 1
            or not stat.S_ISREG(descriptor_identity.mode)
        ):
            raise _FilesystemContractError("temporary_verification_failed")
        os.close(descriptor)
        descriptor = -1
        reopened, reopened_bytes = _capture_regular_file(
            temporary,
            require_single_link=True,
            error_code="temporary_verification_failed",
        )
        if (
            reopened.identity != descriptor_identity
            or reopened.sha256 != expected_hash
            or reopened_bytes != data
        ):
            raise _FilesystemContractError("temporary_verification_failed")
        completed = True
        return temporary, reopened
    except _FilesystemContractError:
        raise
    except OSError as err:
        raise _FilesystemContractError("temporary_write_failed") from err
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None and not completed:
            if owned_identity is not None:
                _unlink_owned_path(temporary, owned_identity)


def _verify_temp_for_publish(
    path: Path,
    expected: FileSnapshot,
    expected_hash: str,
) -> None:
    """Revalidate an exclusive temp immediately before destination checks."""
    current, _ = _capture_regular_file(
        path,
        require_single_link=True,
        error_code="temporary_changed",
    )
    if current.identity != expected.identity or current.sha256 != expected_hash:
        raise _FilesystemContractError("temporary_changed")


def _fsync_directory(path: Path) -> None:
    """Persist a directory mutation on POSIX; Windows has no portable API."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_copy(source: Path, destination: Path) -> None:
    """Copy a regular managed asset through a random exclusive temp."""
    source_snapshot, source_bytes = _capture_regular_file(
        source,
        require_single_link=False,
        error_code="source_unverifiable",
    )
    assert source_snapshot.sha256 is not None
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary, temporary_snapshot = _create_verified_temp(
        destination.parent,
        prefix=f".{destination.name}.{DOMAIN}.txn.",
        suffix=".tmp",
        data=source_bytes,
        expected_hash=source_snapshot.sha256,
    )
    replaced = False
    try:
        _verify_temp_for_publish(
            temporary,
            temporary_snapshot,
            source_snapshot.sha256,
        )
        os.replace(temporary, destination)
        replaced = True
        _fsync_directory(destination.parent)
    finally:
        if not replaced and temporary_snapshot.identity is not None:
            _unlink_owned_path(temporary, temporary_snapshot.identity)


def _verify_fixed_backup(
    backup: Path,
    before: FileSnapshot,
    before_bytes: bytes,
) -> None:
    """Accept only an exact single-link backup of current pre-update bytes."""
    backup_snapshot, backup_bytes = _capture_regular_file(
        backup,
        require_single_link=True,
        error_code="unsafe_backup",
    )
    if (
        before.sha256 is None
        or backup_snapshot.sha256 != before.sha256
        or backup_bytes != before_bytes
    ):
        raise _FilesystemContractError("unsafe_backup")


def _ensure_fixed_backup(
    destination: Path,
    before: FileSnapshot,
    before_bytes: bytes,
) -> Path:
    """Verify or atomically publish the immutable fixed scheduler backup."""
    backup = destination.with_name(f"{destination.name}{EMS_PACKAGE_BACKUP_SUFFIX}")
    try:
        os.lstat(backup)
    except FileNotFoundError:
        pass
    except OSError as err:
        raise _FilesystemContractError("unsafe_backup") from err
    else:
        _verify_fixed_backup(backup, before, before_bytes)
        return backup

    if before.sha256 is None:
        raise _FilesystemContractError("unsafe_backup")
    temporary, temporary_snapshot = _create_verified_temp(
        destination.parent,
        prefix=f".{backup.name}.{DOMAIN}.backup.",
        suffix=".tmp",
        data=before_bytes,
        expected_hash=before.sha256,
    )
    published = False
    try:
        _verify_temp_for_publish(temporary, temporary_snapshot, before.sha256)
        try:
            os.link(temporary, backup)
        except FileExistsError:
            pass
        except OSError as err:
            raise _FilesystemContractError("backup_publish_failed") from err
        else:
            published = True
    finally:
        assert temporary_snapshot.identity is not None
        if not _unlink_owned_path(temporary, temporary_snapshot.identity):
            raise _FilesystemContractError("backup_temp_cleanup_failed")

    _verify_fixed_backup(backup, before, before_bytes)
    if published:
        try:
            _fsync_directory(destination.parent)
        except OSError as err:
            raise _FilesystemContractError("backup_directory_fsync_failed") from err
    return backup


def _storage_helper_ids(path: Path, storage_key: str) -> set[str]:
    """Read one exact HA 2026.8 helper Store contract without following links."""
    try:
        _, raw = _capture_regular_file(
            path,
            require_single_link=False,
            error_code="helper_store_unverifiable",
        )
    except FileNotFoundError:
        return set()
    payload = json.loads(raw.decode("utf-8"))
    expected_major, supported_minors = _HELPER_STORE_CONTRACTS[storage_key]
    if not isinstance(payload, dict) or payload.get("key") != storage_key:
        raise ValueError("invalid helper storage envelope")
    major = payload.get("version")
    if (
        not isinstance(major, int)
        or isinstance(major, bool)
        or major != expected_major
    ):
        raise ValueError("unsupported helper storage major")
    minor = payload.get("minor_version", 1)
    if (
        not isinstance(minor, int)
        or isinstance(minor, bool)
        or minor not in supported_minors
    ):
        raise ValueError("unsupported helper storage minor")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("invalid helper storage collection")
    object_ids: set[str] = set()
    for item in data["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError("invalid helper storage item")
        object_id = item["id"]
        if object_id in object_ids:
            raise ValueError("duplicate helper storage id")
        object_ids.add(object_id)
    return object_ids


def _read_disk_helper_evidence(config_path: Path) -> _DiskHelperEvidence:
    """Return secondary, read-only, fail-closed Store evidence."""
    storage_path = config_path / ".storage"
    try:
        storage_stat = os.lstat(storage_path)
    except FileNotFoundError:
        return _DiskHelperEvidence((), True, "disk_clear")
    except OSError:
        return _DiskHelperEvidence((), False, "disk_unreadable")
    if not stat.S_ISDIR(storage_stat.st_mode):
        return _DiskHelperEvidence((), False, "disk_storage_dir_unsafe")

    collisions: list[str] = []
    try:
        for storage_key in ("input_select", "input_boolean"):
            stored_ids = _storage_helper_ids(
                storage_path / storage_key,
                storage_key,
            )
            collisions.extend(
                f"{storage_key}.{object_id}"
                for object_id in stored_ids
                if object_id in _SUPERVISOR_OBJECT_IDS
            )
    except (
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        _FilesystemContractError,
    ):
        return _DiskHelperEvidence((), False, "disk_store_unverifiable")
    ordered = tuple(sorted(collisions))
    return _DiskHelperEvidence(
        ordered,
        True,
        "disk_collision" if ordered else "disk_clear",
    )


def _unverifiable_live_snapshot(reason_code: str) -> _LiveHelperSnapshot:
    """Return five bounded fail-closed facts for a malformed public surface."""
    return _LiveHelperSnapshot(
        tuple(
            LiveHelperFact(
                entity_id,
                domain,
                object_id,
                HelperClassification.UNVERIFIABLE,
                None,
                reason_code,
            )
            for entity_id, domain, object_id in _SUPERVISOR_HELPERS
        ),
        False,
        False,
    )


def _capture_live_helper_snapshot(hass: HomeAssistant) -> _LiveHelperSnapshot:
    """Classify canonical helpers using only public StateMachine/registry APIs."""
    try:
        marker_state = hass.states.get(EMS_PACKAGE_VERSION_ENTITY)
        if marker_state is None:
            package_marker_current = False
        else:
            marker_entity_id = getattr(marker_state, "entity_id", None)
            marker_value = getattr(marker_state, "state", None)
            if (
                marker_entity_id != EMS_PACKAGE_VERSION_ENTITY
                or not isinstance(marker_value, str)
            ):
                return _unverifiable_live_snapshot("marker_malformed")
            package_marker_current = marker_value == EMS_PACKAGE_VERSION
        registry = er.async_get(hass)
        registry_get = registry.async_get
        registry_get_entity_id = registry.async_get_entity_id
    except Exception:  # noqa: BLE001 - malformed public surfaces fail closed
        return _unverifiable_live_snapshot("live_surface_malformed")

    facts: list[LiveHelperFact] = []
    for entity_id, domain, object_id in _SUPERVISOR_HELPERS:
        classification = HelperClassification.UNVERIFIABLE
        reason_code = "live_surface_malformed"
        registry_entity_id: str | None = None
        try:
            state = hass.states.get(entity_id)
            exact_entry = registry_get(entity_id)
            mapped_entity_id = registry_get_entity_id(domain, domain, object_id)
            if mapped_entity_id is not None and not isinstance(
                mapped_entity_id, str
            ):
                raise TypeError
            if exact_entry is not None:
                candidate_registry_id = getattr(exact_entry, "entity_id", None)
                if not isinstance(candidate_registry_id, str):
                    raise TypeError
                registry_entity_id = candidate_registry_id

            if mapped_entity_id is not None and mapped_entity_id != entity_id:
                classification = HelperClassification.UI_STORAGE_COLLISION
                registry_entity_id = mapped_entity_id
                reason_code = "registry_identity_changed"
            elif state is None:
                if exact_entry is not None or mapped_entity_id is not None:
                    reason_code = "registry_without_state"
                elif package_marker_current:
                    reason_code = "expected_helper_absent"
                else:
                    classification = HelperClassification.ABSENT
                    reason_code = "absent"
            else:
                if getattr(state, "entity_id", None) != entity_id:
                    raise TypeError
                attributes = getattr(state, "attributes", None)
                if not isinstance(attributes, Mapping):
                    raise TypeError
                editable = attributes.get(ATTR_EDITABLE, object())
                if editable is True:
                    classification = HelperClassification.UI_STORAGE_COLLISION
                    reason_code = "editable_true"
                elif editable is not False:
                    reason_code = "editable_malformed"
                elif exact_entry is None or mapped_entity_id != entity_id:
                    reason_code = "state_registry_incoherent"
                elif (
                    getattr(exact_entry, "entity_id", None) != entity_id
                    or getattr(exact_entry, "platform", None) != domain
                    or getattr(exact_entry, "unique_id", None) != object_id
                ):
                    reason_code = "registry_identity_incoherent"
                elif not package_marker_current:
                    reason_code = "expected_marker_missing"
                else:
                    classification = HelperClassification.EXPECTED_YAML
                    reason_code = "expected_yaml"
        except Exception:  # noqa: BLE001 - malformed public facts fail closed
            classification = HelperClassification.UNVERIFIABLE
            reason_code = "live_surface_malformed"
        facts.append(
            LiveHelperFact(
                entity_id,
                domain,
                object_id,
                classification,
                registry_entity_id,
                reason_code,
            )
        )
    frozen_facts = tuple(facts)
    return _LiveHelperSnapshot(
        frozen_facts,
        package_marker_current,
        all(
            fact.classification is not HelperClassification.UNVERIFIABLE
            for fact in frozen_facts
        ),
    )


def _build_scheduler_authorization(
    first_live: _LiveHelperSnapshot,
    disk: _DiskHelperEvidence,
    second_live: _LiveHelperSnapshot,
) -> SchedulerInstallAuthorization:
    """Combine live/disk/live facts at the pre/postflight linearization point."""
    classifications = tuple(
        fact.classification for fact in second_live.helper_facts
    )
    if first_live != second_live:
        reason_code = "live_changed"
    elif not disk.verifiable:
        reason_code = disk.reason_code
    elif disk.collision_ids:
        reason_code = "disk_collision"
    elif HelperClassification.UI_STORAGE_COLLISION in classifications:
        reason_code = "live_collision"
    elif HelperClassification.UNVERIFIABLE in classifications:
        reason_code = "live_unverifiable"
    else:
        reason_code = "clear"
    return SchedulerInstallAuthorization(
        second_live.helper_facts,
        disk.collision_ids,
        disk.verifiable,
        second_live.package_marker_current,
        reason_code == "clear",
        reason_code,
    )


async def _async_scheduler_authorization(
    hass: HomeAssistant,
    config_path: Path,
) -> SchedulerInstallAuthorization:
    """Run one exact live/disk/live collision authorization sequence."""
    first_live = _capture_live_helper_snapshot(hass)
    disk = await hass.async_add_executor_job(
        _read_disk_helper_evidence,
        config_path,
    )
    second_live = _capture_live_helper_snapshot(hass)
    return _build_scheduler_authorization(first_live, disk, second_live)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write a Home Assistant storage document atomically."""
    temporary = path.with_name(f".{path.name}.{DOMAIN}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if path.exists():
        shutil.copystat(path, temporary)
    os.replace(temporary, path)


def _backup_storage_once(path: Path) -> None:
    """Keep one exact rollback copy for this integration release."""
    backup = path.with_name(f"{path.name}.pre-{VERSION}.bak")
    if path.is_file() and not backup.exists():
        shutil.copy2(path, backup)


def _replace_entities_cards(value: Any) -> int:
    """Convert native entities cards to the drop-in zebra card in place."""
    changed = 0
    if isinstance(value, dict):
        if value.get("type") == "entities":
            value["type"] = ZEBRA_CARD_TYPE
            changed += 1
        for child in value.values():
            changed += _replace_entities_cards(child)
    elif isinstance(value, list):
        for child in value:
            changed += _replace_entities_cards(child)
    return changed


def _migrate_rce_load_rows(value: Any, language: str) -> int:
    """Expose the physical LOAD counter and clarify legacy flow estimates."""
    changed = 0
    localized = "pl" if language.startswith("pl") else "en"
    labels = RCE_LOAD_ROW_LABELS[localized]
    pv_entity = "sensor.hoymiles_rce_pv_self_consumption_today"
    actual_entity = "sensor.hoymiles_actual_load_energy_today"

    if isinstance(value, dict):
        entity = value.get("entity")
        if entity in labels and value.get("name") != labels[entity]:
            value["name"] = labels[entity]
            changed += 1

        entities = value.get("entities")
        if isinstance(entities, list):
            entity_ids = {
                row.get("entity")
                for row in entities
                if isinstance(row, dict)
            }
            if pv_entity in entity_ids and actual_entity not in entity_ids:
                for index, row in enumerate(entities):
                    if isinstance(row, dict) and row.get("entity") == pv_entity:
                        entities.insert(
                            index,
                            {
                                "entity": actual_entity,
                                "name": labels[actual_entity],
                            },
                        )
                        changed += 1
                        break

        for child in value.values():
            changed += _migrate_rce_load_rows(child, language)
    elif isinstance(value, list):
        for child in value:
            changed += _migrate_rce_load_rows(child, language)
    return changed


def _is_hoymiles_dashboard(config: Any) -> bool:
    """Return whether a Lovelace config is the managed Hoymiles dashboard."""
    if not isinstance(config, dict):
        return False
    serialized = json.dumps(config, ensure_ascii=False, separators=(",", ":"))
    return all(marker in serialized for marker in HOYMILES_DASHBOARD_MARKERS)


async def _async_sync_lovelace_resource(hass: HomeAssistant) -> bool:
    """Publish exactly one full module through Lovelace's live collection."""
    lovelace_data = hass.data.get(LOVELACE_DATA)
    if lovelace_data is None or lovelace_data.resource_mode != MODE_STORAGE:
        return False

    resources = lovelace_data.resources
    # async_get_info is the public read path and guarantees the lazy storage
    # collection is loaded before async_items is inspected on supported HA
    # versions. Mutating via the same collection used by the websocket API
    # keeps memory and delayed storage writes consistent.
    await resources.async_get_info()
    managed_items: list[dict[str, Any]] = []
    canonical_item: dict[str, Any] | None = None
    changed = False
    for item in list(resources.async_items()):
        if not isinstance(item, dict):
            continue
        url = item.get(CONF_URL)
        if not isinstance(url, str):
            continue
        if url.partition("?")[0] not in MANAGED_FRONTEND_RESOURCE_PATHS:
            continue
        resource_id = item.get(CONF_ID)
        if not isinstance(resource_id, str):
            continue
        managed_items.append(item)
        if (
            canonical_item is None
            and url == FRONTEND_RESOURCE_URL
            and item.get(CONF_TYPE) == "module"
        ):
            canonical_item = item

    if canonical_item is None and managed_items:
        canonical_item = managed_items[0]

    if canonical_item is not None:
        canonical_id = canonical_item[CONF_ID]
        if (
            canonical_item.get(CONF_URL) != FRONTEND_RESOURCE_URL
            or canonical_item.get(CONF_TYPE) != "module"
        ):
            await resources.async_update_item(
                canonical_id,
                {
                    CONF_URL: FRONTEND_RESOURCE_URL,
                    CONF_RESOURCE_TYPE_WS: "module",
                },
            )
            changed = True
        # Older releases could publish the classic strategy bootstrap. If it
        # loads first, its undecorated strategy owns the immutable custom
        # element name for that page. Remove that resource, plus duplicate card
        # entries, through the same live collection used by Lovelace.
        for item in managed_items:
            resource_id = item[CONF_ID]
            if resource_id == canonical_id:
                continue
            await resources.async_delete_item(resource_id)
            changed = True
    else:
        await resources.async_create_item(
            {
                CONF_URL: FRONTEND_RESOURCE_URL,
                CONF_RESOURCE_TYPE_WS: "module",
            }
        )
        changed = True

    return changed


def _sync_lovelace_storage(
    config_path: Path,
    language: str = "pl",
) -> list[Path]:
    """Migrate active storage dashboards without replacing user layouts."""
    storage_path = config_path / ".storage"
    if not storage_path.is_dir():
        return []

    written: list[Path] = []

    for path in sorted(storage_path.glob(f"{LOVELACE_STORAGE_PREFIX}*")):
        if not path.is_file() or path.name == LOVELACE_RESOURCES_KEY:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        # Backups share the same embedded key as the active file. Requiring
        # an exact key/path match prevents recursively migrating them.
        if payload.get("key") != path.name:
            continue
        config = payload.get("data", {}).get("config")
        if not _is_hoymiles_dashboard(config):
            continue
        changes = _replace_entities_cards(config)
        changes += _migrate_rce_load_rows(config, language)
        changes += _migrate_inverter_image_paths(config)
        if changes == 0:
            continue
        _backup_storage_once(path)
        _atomic_write_json(path, payload)
        written.append(path)
    return written


def _migrate_inverter_image_paths(value: Any) -> int:
    """Move dashboards away from removed integration-static image routes."""
    changes = 0
    if isinstance(value, dict):
        image = value.get("inverter_image")
        if image in LEGACY_INVERTER_IMAGE_PATHS:
            value["inverter_image"] = INVERTER_IMAGE_PATH
            changes += 1
        for child in value.values():
            changes += _migrate_inverter_image_paths(child)
    elif isinstance(value, list):
        for child in value:
            changes += _migrate_inverter_image_paths(child)
    return changes


def _revalidate_destination(
    destination: Path,
    before: FileSnapshot,
) -> None:
    """Require the destination to match its exact pre-transaction snapshot."""
    if not before.existed:
        try:
            os.lstat(destination)
        except FileNotFoundError:
            return
        except OSError as err:
            raise _FilesystemContractError("destination_changed") from err
        raise _FilesystemContractError("destination_changed")
    if before.identity is None or before.sha256 is None:
        raise _FilesystemContractError("destination_changed")
    try:
        current, _ = _capture_regular_file(
            destination,
            require_single_link=False,
            error_code="destination_changed",
        )
    except FileNotFoundError as err:
        raise _FilesystemContractError("destination_changed") from err
    if current.identity != before.identity or current.sha256 != before.sha256:
        raise _FilesystemContractError("destination_changed")


def _remove_transaction_artifact(
    path: Path,
    snapshot: FileSnapshot,
) -> bool:
    """Remove only the exact random artifact created by this transaction."""
    if snapshot.identity is None or snapshot.sha256 is None:
        return False
    try:
        current, _ = _capture_regular_file(
            path,
            require_single_link=True,
            error_code="rollback_artifact_changed",
        )
    except (FileNotFoundError, _FilesystemContractError):
        return False
    if current.identity != snapshot.identity or current.sha256 != snapshot.sha256:
        return False
    return _unlink_owned_path(path, snapshot.identity)


def _aborted_scheduler_result(
    *,
    source_hash: str,
    before: FileSnapshot,
    persistent_backup: Path | None,
    error_code: str,
    physically_written: bool = False,
    rollback_path: Path | None = None,
    rollback_snapshot: FileSnapshot | None = None,
) -> SchedulerFilesystemResult:
    """Build one bounded failed scheduler result."""
    return SchedulerFilesystemResult(
        SchedulerFilesystemAction.FAILED_AFTER_REPLACE
        if physically_written
        else SchedulerFilesystemAction.ABORTED_DESTINATION_CHANGED,
        source_hash,
        before,
        _bounded_path_snapshot(before.path),
        persistent_backup,
        rollback_path,
        rollback_snapshot,
        None,
        physically_written,
        error_code,
    )


def _sync_ems_scheduler(
    source: Path,
    destination: Path,
    *,
    overwrite: bool,
    previous_hash: str | None,
    authorization: SchedulerInstallAuthorization,
) -> SchedulerFilesystemResult:
    """Execute the authorized scheduler filesystem transaction only."""
    source_snapshot, source_bytes = _capture_regular_file(
        source,
        require_single_link=False,
        error_code="source_unverifiable",
    )
    assert source_snapshot.sha256 is not None
    source_hash = source_snapshot.sha256

    if not authorization.allowed:
        before = _bounded_path_snapshot(destination)
        return SchedulerFilesystemResult(
            SchedulerFilesystemAction.BLOCKED_COLLISION,
            source_hash,
            before,
            before,
            None,
            None,
            None,
            None,
            False,
            authorization.reason_code,
        )

    try:
        before, before_bytes = _capture_regular_file(
            destination,
            require_single_link=False,
            error_code="destination_unsafe",
        )
    except FileNotFoundError:
        before = FileSnapshot(destination, False, None, None)
        before_bytes = b""
    except _FilesystemContractError as err:
        before = _bounded_path_snapshot(destination)
        return _aborted_scheduler_result(
            source_hash=source_hash,
            before=before,
            persistent_backup=None,
            error_code=err.code,
        )

    if before.sha256 == source_hash:
        return SchedulerFilesystemResult(
            SchedulerFilesystemAction.CURRENT,
            source_hash,
            before,
            before,
            None,
            None,
            None,
            source_hash,
            False,
            None,
        )

    if before.existed:
        managed = (
            previous_hash == before.sha256
            or before.sha256
            in LEGACY_MANAGED_HASHES.get(EMS_PACKAGE_RELATIVE_PATH, set())
        )
        if not overwrite and not managed:
            return SchedulerFilesystemResult(
                SchedulerFilesystemAction.PRESERVED_MODIFIED,
                source_hash,
                before,
                before,
                None,
                None,
                None,
                None,
                False,
                "modified_package",
            )

    destination.parent.mkdir(parents=True, exist_ok=True)
    persistent_backup: Path | None = None
    rollback_path: Path | None = None
    rollback_snapshot: FileSnapshot | None = None
    destination_temp: Path | None = None
    destination_temp_snapshot: FileSnapshot | None = None
    replaced = False
    try:
        if before.existed:
            persistent_backup = _ensure_fixed_backup(
                destination,
                before,
                before_bytes,
            )
            assert before.sha256 is not None
            rollback_path, rollback_snapshot = _create_verified_temp(
                destination.parent,
                prefix=f".{destination.name}.{DOMAIN}.rollback.",
                suffix=".tmp",
                data=before_bytes,
                expected_hash=before.sha256,
            )

        destination_temp, destination_temp_snapshot = _create_verified_temp(
            destination.parent,
            prefix=f".{destination.name}.{DOMAIN}.destination.",
            suffix=".tmp",
            data=source_bytes,
            expected_hash=source_hash,
        )
        _verify_temp_for_publish(
            destination_temp,
            destination_temp_snapshot,
            source_hash,
        )
        _revalidate_destination(destination, before)
        try:
            os.replace(destination_temp, destination)
        except OSError as err:
            raise _FilesystemContractError("replace_failed") from err
        replaced = True
        try:
            _fsync_directory(destination.parent)
        except OSError as err:
            raise _FilesystemContractError(
                "destination_directory_fsync_failed"
            ) from err
        try:
            after, after_bytes = _capture_regular_file(
                destination,
                require_single_link=True,
                error_code="destination_verify_failed",
            )
        except FileNotFoundError as err:
            raise _FilesystemContractError("destination_verify_failed") from err
        if after.sha256 != source_hash or after_bytes != source_bytes:
            raise _FilesystemContractError("destination_verify_failed")
    except _FilesystemContractError as err:
        if (
            destination_temp is not None
            and destination_temp_snapshot is not None
            and not replaced
            and destination_temp_snapshot.identity is not None
        ):
            _unlink_owned_path(
                destination_temp,
                destination_temp_snapshot.identity,
            )
        if not replaced and rollback_path is not None and rollback_snapshot is not None:
            if _remove_transaction_artifact(rollback_path, rollback_snapshot):
                rollback_path = None
                rollback_snapshot = None
            else:
                err = _FilesystemContractError("rollback_artifact_cleanup_failed")
        return _aborted_scheduler_result(
            source_hash=source_hash,
            before=before,
            persistent_backup=persistent_backup,
            error_code=err.code,
            physically_written=replaced,
            rollback_path=rollback_path,
            rollback_snapshot=rollback_snapshot,
        )

    return SchedulerFilesystemResult(
        SchedulerFilesystemAction.REPLACED_EXISTING
        if before.existed
        else SchedulerFilesystemAction.INSTALLED_FRESH,
        source_hash,
        before,
        after,
        persistent_backup,
        rollback_path,
        rollback_snapshot,
        source_hash,
        True,
        None,
    )


def _rollback_scheduler(
    transaction: SchedulerFilesystemResult,
) -> RollbackResult:
    """Rollback only an exact scheduler installed by this transaction."""
    destination = transaction.after.path
    if not transaction.physically_written:
        return RollbackResult(
            RollbackAction.NOT_NEEDED,
            transaction.after,
            None,
        )
    if transaction.after.identity is None or transaction.after.sha256 is None:
        return RollbackResult(
            RollbackAction.FAILED_CONCURRENT_CHANGE,
            _bounded_path_snapshot(destination),
            "installed_snapshot_unverifiable",
        )
    try:
        installed, _ = _capture_regular_file(
            destination,
            require_single_link=True,
            error_code="installed_destination_changed",
        )
    except (FileNotFoundError, _FilesystemContractError):
        return RollbackResult(
            RollbackAction.FAILED_CONCURRENT_CHANGE,
            _bounded_path_snapshot(destination),
            "installed_destination_changed",
        )
    if (
        installed.identity != transaction.after.identity
        or installed.sha256 != transaction.source_hash
    ):
        return RollbackResult(
            RollbackAction.FAILED_CONCURRENT_CHANGE,
            installed,
            "installed_destination_changed",
        )

    if not transaction.before.existed:
        try:
            os.unlink(destination)
            _fsync_directory(destination.parent)
        except OSError:
            return RollbackResult(
                RollbackAction.FAILED_CONCURRENT_CHANGE,
                _bounded_path_snapshot(destination),
                "fresh_remove_failed",
            )
        resulting = _bounded_path_snapshot(destination)
        if resulting.existed:
            return RollbackResult(
                RollbackAction.FAILED_CONCURRENT_CHANGE,
                resulting,
                "fresh_remove_unverified",
            )
        return RollbackResult(RollbackAction.REMOVED_FRESH, resulting, None)

    rollback_path = transaction.transaction_rollback
    rollback_expected = transaction.transaction_rollback_snapshot
    if (
        rollback_path is None
        or rollback_expected is None
        or rollback_expected.identity is None
        or transaction.before.sha256 is None
    ):
        return RollbackResult(
            RollbackAction.FAILED_CONCURRENT_CHANGE,
            installed,
            "rollback_artifact_missing",
        )
    try:
        rollback_current, rollback_bytes = _capture_regular_file(
            rollback_path,
            require_single_link=True,
            error_code="rollback_artifact_changed",
        )
    except (FileNotFoundError, _FilesystemContractError):
        return RollbackResult(
            RollbackAction.FAILED_CONCURRENT_CHANGE,
            installed,
            "rollback_artifact_changed",
        )
    if (
        rollback_current.identity != rollback_expected.identity
        or rollback_current.sha256 != transaction.before.sha256
    ):
        return RollbackResult(
            RollbackAction.FAILED_CONCURRENT_CHANGE,
            installed,
            "rollback_artifact_changed",
        )

    restore_temp: Path | None = None
    restore_snapshot: FileSnapshot | None = None
    restored = False
    try:
        restore_temp, restore_snapshot = _create_verified_temp(
            destination.parent,
            prefix=f".{destination.name}.{DOMAIN}.restore.",
            suffix=".tmp",
            data=rollback_bytes,
            expected_hash=transaction.before.sha256,
        )
        _verify_temp_for_publish(
            restore_temp,
            restore_snapshot,
            transaction.before.sha256,
        )
        _revalidate_destination(destination, transaction.after)
        os.replace(restore_temp, destination)
        restored = True
        _fsync_directory(destination.parent)
        resulting, resulting_bytes = _capture_regular_file(
            destination,
            require_single_link=True,
            error_code="rollback_verify_failed",
        )
        if (
            resulting.sha256 != transaction.before.sha256
            or resulting_bytes != rollback_bytes
        ):
            raise _FilesystemContractError("rollback_verify_failed")
    except (OSError, _FilesystemContractError):
        if (
            restore_temp is not None
            and restore_snapshot is not None
            and not restored
            and restore_snapshot.identity is not None
        ):
            _unlink_owned_path(restore_temp, restore_snapshot.identity)
        return RollbackResult(
            RollbackAction.FAILED_CONCURRENT_CHANGE,
            _bounded_path_snapshot(destination),
            "rollback_failed",
        )
    return RollbackResult(RollbackAction.RESTORED_OLD, resulting, None)


def _sync_regular_asset(
    source: Path,
    destination: Path,
    *,
    overwrite: bool,
    previous_hash: str | None,
    relative: str,
) -> tuple[bool, str | None]:
    """Retain the existing non-scheduler managed-asset behavior."""
    source_hash = _sha256(source)
    if destination.exists():
        destination_hash = _sha256(destination)
        if destination_hash == source_hash:
            return False, source_hash
        if overwrite:
            _atomic_copy(source, destination)
            return True, source_hash
        managed = (
            previous_hash == destination_hash
            or destination_hash in LEGACY_MANAGED_HASHES.get(relative, set())
        )
        written = False
        if managed:
            _atomic_copy(source, destination)
            destination_hash = source_hash
            written = True
        if (
            destination.name == "dashboard_hoymiles.yaml"
            and _migrate_legacy_entity_ids(destination)
        ):
            destination_hash = _sha256(destination)
            written = True
        return written, source_hash if destination_hash == source_hash else None
    destination.parent.mkdir(parents=True, exist_ok=True)
    _atomic_copy(source, destination)
    return True, source_hash


def _sync_assets(
    config_path: Path,
    language: str,
    overwrite: bool,
    managed_hash_facts: tuple[tuple[str, str], ...],
    authorization: SchedulerInstallAuthorization,
) -> AssetFilesystemResult:
    """Run the real filesystem phase used by async_install_assets."""
    managed_hashes = dict(managed_hash_facts)
    localized = "pl" if language.startswith("pl") else "en"
    sources = {
        RESOURCE_ROOT / f"dashboard_hoymiles_{localized}.yaml": (
            config_path / "dashboard_hoymiles.yaml"
        ),
    }
    for filename in LOCAL_FRONTEND_ASSETS:
        sources[RESOURCE_ROOT / "www" / filename] = (
            config_path / "www" / filename
        )
    # Keep the scheduler last: after it writes, the executor must return its
    # immutable result without a later unrelated asset failure obscuring it.
    sources[
        RESOURCE_ROOT
        / "home_assistant"
        / localized
        / "hoymiles_ems_scheduler.yaml"
    ] = config_path / "packages" / "hoymiles_ems_scheduler.yaml"

    written: list[Path] = []
    non_scheduler_hashes: dict[str, str] = {}
    scheduler: SchedulerFilesystemResult | None = None
    for source, destination in sources.items():
        relative = destination.relative_to(config_path).as_posix()
        if relative == EMS_PACKAGE_RELATIVE_PATH:
            scheduler = _sync_ems_scheduler(
                source,
                destination,
                overwrite=overwrite,
                previous_hash=managed_hashes.get(relative),
                authorization=authorization,
            )
            if scheduler.action in {
                SchedulerFilesystemAction.INSTALLED_FRESH,
                SchedulerFilesystemAction.REPLACED_EXISTING,
            }:
                written.append(destination)
            continue
        was_written, managed_hash = _sync_regular_asset(
            source,
            destination,
            overwrite=overwrite,
            previous_hash=managed_hashes.get(relative),
            relative=relative,
        )
        if was_written:
            written.append(destination)
        if managed_hash is not None:
            non_scheduler_hashes[relative] = managed_hash
    assert scheduler is not None
    return AssetFilesystemResult(
        tuple(written),
        tuple(sorted(non_scheduler_hashes.items())),
        scheduler,
    )


def _get_install_lock(hass: HomeAssistant) -> asyncio.Lock:
    """Return the one integration-global install lock for this HA instance."""
    lock = hass.data.get(_INSTALL_LOCK_DATA_KEY)
    if lock is None:
        lock = asyncio.Lock()
        hass.data[_INSTALL_LOCK_DATA_KEY] = lock
    if not isinstance(lock, asyncio.Lock):
        raise RuntimeError("invalid integration-global asset install lock")
    return lock


def _attest_scheduler_destination(
    expected_destination: Path,
    transaction: SchedulerFilesystemResult,
) -> DestinationAttestationResult:
    """Verify the exact scheduler state immediately before metadata commit."""
    expected = transaction.after
    candidate_actions = {
        SchedulerFilesystemAction.CURRENT,
        SchedulerFilesystemAction.INSTALLED_FRESH,
        SchedulerFilesystemAction.REPLACED_EXISTING,
    }
    if (
        transaction.action not in candidate_actions
        or transaction.scheduler_metadata_candidate is None
    ):
        return DestinationAttestationResult(
            DestinationAttestationAction.NOT_REQUIRED,
            expected,
            None,
        )

    written_action = transaction.action in {
        SchedulerFilesystemAction.INSTALLED_FRESH,
        SchedulerFilesystemAction.REPLACED_EXISTING,
    }
    if (
        expected.path != expected_destination
        or transaction.before.path != expected_destination
        or not expected.existed
        or expected.identity is None
        or not stat.S_ISREG(expected.identity.mode)
        or expected.sha256 != transaction.source_hash
        or transaction.scheduler_metadata_candidate != transaction.source_hash
        or transaction.physically_written != written_action
    ):
        return DestinationAttestationResult(
            DestinationAttestationAction.ABORTED_DESTINATION_CHANGED,
            _bounded_path_snapshot(expected.path),
            "final_destination_expected_unverifiable",
        )

    try:
        current, _ = _capture_regular_file(
            expected.path,
            require_single_link=False,
            error_code="final_destination_unverifiable",
        )
    except (FileNotFoundError, _FilesystemContractError):
        return DestinationAttestationResult(
            DestinationAttestationAction.ABORTED_DESTINATION_CHANGED,
            _bounded_path_snapshot(expected.path),
            "final_destination_unverifiable",
        )
    if (
        current.identity != expected.identity
        or current.sha256 != transaction.source_hash
    ):
        return DestinationAttestationResult(
            DestinationAttestationAction.ABORTED_DESTINATION_CHANGED,
            current,
            "final_destination_changed",
        )
    return DestinationAttestationResult(
        DestinationAttestationAction.VERIFIED,
        current,
        None,
    )


def _scheduler_metadata_after_transaction(
    previous_hashes: dict[str, str],
    transaction: SchedulerFilesystemResult,
    *,
    preflight: SchedulerInstallAuthorization,
    postflight: SchedulerInstallAuthorization,
    rollback: RollbackResult,
    attestation: DestinationAttestationResult,
) -> str | None:
    """Return only a permitted scheduler metadata value."""
    previous = previous_hashes.get(EMS_PACKAGE_RELATIVE_PATH)
    if (
        preflight.allowed
        and postflight.allowed
        and attestation.action is DestinationAttestationAction.VERIFIED
        and transaction.action
        in {
            SchedulerFilesystemAction.CURRENT,
            SchedulerFilesystemAction.INSTALLED_FRESH,
            SchedulerFilesystemAction.REPLACED_EXISTING,
        }
        and transaction.scheduler_metadata_candidate is not None
    ):
        return transaction.scheduler_metadata_candidate
    if rollback.action is RollbackAction.REMOVED_FRESH:
        return None
    if rollback.action is RollbackAction.RESTORED_OLD:
        if previous == rollback.resulting_snapshot.sha256:
            return previous
        return None
    # No successful scheduler transition occurred. Preserve old metadata
    # verbatim; never manufacture or advance it on a blocked/failed path.
    return previous


def _log_scheduler_outcome(
    localized: str,
    preflight: SchedulerInstallAuthorization,
    postflight: SchedulerInstallAuthorization,
    transaction: SchedulerFilesystemResult,
    rollback: RollbackResult,
    attestation: DestinationAttestationResult,
) -> None:
    """Emit at most one bounded scheduler warning category per invocation."""
    if not preflight.allowed or not postflight.allowed:
        _LOGGER.warning(
            "%s: %s",
            SUPERVISOR_HELPER_COLLISION_CATEGORY,
            SUPERVISOR_HELPER_COLLISION_INSTRUCTIONS[localized],
        )
        return
    if (
        attestation.action
        is DestinationAttestationAction.ABORTED_DESTINATION_CHANGED
    ):
        _LOGGER.warning(
            "ems_package_destination_changed: %s",
            DESTINATION_CHANGED_INSTRUCTIONS[localized],
        )
        return
    if transaction.action is SchedulerFilesystemAction.PRESERVED_MODIFIED:
        _LOGGER.warning("%s", MODIFIED_EMS_PACKAGE_INSTRUCTIONS[localized])
        return
    if transaction.error_code is not None:
        if transaction.error_code.startswith("backup") or transaction.error_code in {
            "unsafe_backup",
            "backup_temp_cleanup_failed",
        }:
            _LOGGER.warning(
                "ems_package_backup_unsafe: %s",
                UNSAFE_EMS_PACKAGE_BACKUP_INSTRUCTIONS[localized],
            )
        else:
            _LOGGER.warning(
                "ems_package_destination_changed: %s",
                DESTINATION_CHANGED_INSTRUCTIONS[localized],
            )
        return
    if rollback.action is RollbackAction.FAILED_CONCURRENT_CHANGE:
        _LOGGER.warning(
            "ems_package_destination_changed: %s",
            DESTINATION_CHANGED_INSTRUCTIONS[localized],
        )


async def async_install_assets(
    hass: HomeAssistant,
    *,
    overwrite: bool,
    publish_frontend: bool = True,
) -> list[Path]:
    """Install the optional assets without blocking Home Assistant."""
    config_path = Path(hass.config.config_dir)
    localized = "pl" if hass.config.language.startswith("pl") else "en"
    lock = _get_install_lock(hass)
    async with lock:
        store: Store[dict] = Store(
            hass,
            ASSET_STORAGE_VERSION,
            ASSET_STORAGE_KEY,
        )
        stored = await store.async_load() or {}
        if not isinstance(stored, dict):
            stored = {}
        raw_managed_hashes = stored.get("assets", {})
        managed_hashes = {
            key: value
            for key, value in (
                raw_managed_hashes.items()
                if isinstance(raw_managed_hashes, dict)
                else ()
            )
            if isinstance(key, str) and isinstance(value, str)
        }

        preflight = await _async_scheduler_authorization(hass, config_path)
        filesystem = await hass.async_add_executor_job(
            _sync_assets,
            config_path,
            hass.config.language,
            overwrite,
            tuple(sorted(managed_hashes.items())),
            preflight,
        )
        postflight = await _async_scheduler_authorization(hass, config_path)

        rollback = RollbackResult(
            RollbackAction.NOT_NEEDED,
            filesystem.scheduler.after,
            None,
        )
        written = list(filesystem.written_paths)
        scheduler_path = config_path / EMS_PACKAGE_RELATIVE_PATH
        if not postflight.allowed and filesystem.scheduler.physically_written:
            rollback = await hass.async_add_executor_job(
                _rollback_scheduler,
                filesystem.scheduler,
            )
            written = [
                path
                for path in written
                if path != scheduler_path
            ]
        elif not postflight.allowed:
            written = [
                path
                for path in written
                if path != scheduler_path
            ]

        attestation = DestinationAttestationResult(
            DestinationAttestationAction.NOT_REQUIRED,
            rollback.resulting_snapshot,
            None,
        )
        if (
            preflight.allowed
            and postflight.allowed
            and rollback.action is RollbackAction.NOT_NEEDED
            and filesystem.scheduler.scheduler_metadata_candidate is not None
        ):
            attestation = await hass.async_add_executor_job(
                _attest_scheduler_destination,
                scheduler_path,
                filesystem.scheduler,
            )
            if (
                attestation.action
                is DestinationAttestationAction.ABORTED_DESTINATION_CHANGED
            ):
                written = [
                    path
                    for path in written
                    if path != scheduler_path
                ]

        next_hashes = dict(filesystem.non_scheduler_hashes)
        scheduler_hash = _scheduler_metadata_after_transaction(
            managed_hashes,
            filesystem.scheduler,
            preflight=preflight,
            postflight=postflight,
            rollback=rollback,
            attestation=attestation,
        )
        if scheduler_hash is not None:
            next_hashes[EMS_PACKAGE_RELATIVE_PATH] = scheduler_hash
        next_metadata = {
            "integration_version": VERSION,
            "assets": next_hashes,
        }
        if next_metadata != stored:
            await store.async_save(next_metadata)

        transaction_resolved = (
            preflight.allowed
            and postflight.allowed
            and filesystem.scheduler.action
            is SchedulerFilesystemAction.REPLACED_EXISTING
        ) or rollback.action is RollbackAction.RESTORED_OLD
        if (
            transaction_resolved
            and filesystem.scheduler.transaction_rollback is not None
            and filesystem.scheduler.transaction_rollback_snapshot is not None
        ):
            removed = await hass.async_add_executor_job(
                _remove_transaction_artifact,
                filesystem.scheduler.transaction_rollback,
                filesystem.scheduler.transaction_rollback_snapshot,
            )
            if not removed:
                if (
                    preflight.allowed
                    and postflight.allowed
                    and attestation.action
                    is not DestinationAttestationAction.ABORTED_DESTINATION_CHANGED
                ):
                    _LOGGER.warning(
                        "ems_package_rollback_cleanup_failed: transaction "
                        "artifact could not be verified and was left untouched"
                    )

        _log_scheduler_outcome(
            localized,
            preflight,
            postflight,
            filesystem.scheduler,
            rollback,
            attestation,
        )

        # Do not rewrite .storage/lovelace.* behind Lovelace's in-memory
        # dashboard objects. The live resource collection remains authoritative.
        if publish_frontend:
            await _async_sync_lovelace_resource(hass)
        return written
