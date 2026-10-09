"""Home Assistant persistence and entity view for Supervisor accounting v2."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import json
import logging
import math
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN, UnitOfEnergy
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, NAME
from .models import RuntimeData
from .supervisor_accounting_runtime import (
    build_active_accounting_entry,
    entry_is_accounting_relevant,
)
from .supervisor_accounting_v2 import (
    ENTITY_UNIQUE_ID,
    STORAGE_EPOCH,
    STORAGE_KEY,
    STORAGE_SCHEMA_VERSION,
    AccountingV2State,
    accounting_state_from_dict,
    accounting_state_to_dict,
    accumulate_execution_entry,
    legacy_v1_diagnostic,
    new_accounting_state,
)
from .supervisor_active_controller import ActiveFrame
from .supervisor_executor import ExecutorRecord
from .supervisor_ledger import (
    ExecutionLedgerEntry,
    execution_ledger_entry_to_dict,
)


_LOGGER = logging.getLogger(__name__)
_STORE_WRAPPER_KEYS = frozenset(
    {
        "schema_version",
        "storage_epoch",
        "epoch_started_at",
        "legacy_v1_value_kwh",
        "legacy_v1_observed_at",
        "last_processed_evidence_fingerprint",
        "last_processed_reason",
        "state",
    }
)
_TRANSITION_PROOF_KEY = "last_transition_proof"
_TRANSITION_PROOF_KEYS = frozenset({
    "schema_version", "observed_at", "evidence_fingerprint",
    "previous_anchor_fingerprint", "reason", "accepted",
    "interval_energy_kwh", "accepted_interval_count",
    "rejected_interval_count", "provenance",
})
_LEGACY_ENTITY_ID = "sensor.hoymiles_tariff_grid_charge_energy_total"


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError("epoch_started_at must be a canonical UTC timestamp")
    parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("epoch_started_at must be timezone-aware")
    normalized = parsed.astimezone(timezone.utc)
    if _timestamp(normalized) != value:
        raise ValueError("epoch_started_at is not canonical")
    return normalized


def _validate_transition_proof(value: Any) -> dict[str, Any] | None:
    """Accept only a small, exact Recorder/Store proof; never repair bad data."""
    if value is None:
        return None
    if type(value) is not dict or frozenset(value) != _TRANSITION_PROOF_KEYS:
        raise ValueError("accounting transition proof keys are invalid")
    if value["schema_version"] != 1:
        raise ValueError("accounting transition proof schema is unsupported")
    _parse_timestamp(value["observed_at"])
    for key in ("evidence_fingerprint", "previous_anchor_fingerprint"):
        item = value[key]
        if item is not None and (type(item) is not str or len(item) != 64
                                 or any(char not in "0123456789abcdef" for char in item)):
            raise ValueError("accounting transition fingerprint is invalid")
    if value["evidence_fingerprint"] is None:
        raise ValueError("accounting transition fingerprint is missing")
    if type(value["reason"]) is not str or len(value["reason"]) > 96:
        raise ValueError("accounting transition reason is invalid")
    if type(value["accepted"]) is not bool:
        raise ValueError("accounting transition acceptance is invalid")
    energy = value["interval_energy_kwh"]
    if type(energy) not in (int, float) or not math.isfinite(energy) or energy < 0:
        raise ValueError("accounting transition energy is invalid")
    for key in ("accepted_interval_count", "rejected_interval_count"):
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError("accounting transition count is invalid")
    sources = value["provenance"]
    if type(sources) is not list or len(sources) != 4:
        raise ValueError("accounting transition provenance is invalid")
    for source in sources:
        if type(source) is not dict or frozenset(source) != {
            "field", "source", "reported_at", "status"
        }:
            raise ValueError("accounting transition source is invalid")
        for key in ("field", "source", "status"):
            if type(source[key]) is not str or len(source[key]) > 256:
                raise ValueError("accounting transition source value is invalid")
        if source["reported_at"] is not None:
            _parse_timestamp(source["reported_at"])
    if len(json.dumps(value, sort_keys=True, allow_nan=False).encode()) > 4096:
        raise ValueError("accounting transition proof exceeds size bound")
    return value


class HoymilesSupervisorAccountingV2Sensor(SensorEntity):
    """Publish an isolated total-increasing physical grid-to-battery epoch."""

    _attr_should_poll = False
    # These two fields remain fresh in live state and in the durable accounting
    # ledger, but their per-frame copies carry no new cumulative-energy value.
    _unrecorded_attributes = frozenset({
        "current_evidence_fingerprint",
        "current_provenance",
    })
    _attr_has_entity_name = True
    _attr_translation_key = "ems_supervisor_grid_to_battery_energy_v2"
    _attr_icon = "mdi:battery-arrow-up-outline"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(
        self,
        hass,
        entry: ConfigEntry,
        runtime: RuntimeData,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_{ENTITY_UNIQUE_ID}"
        self._store: Store[dict[str, Any]] = Store(
            hass,
            STORAGE_SCHEMA_VERSION,
            f"{STORAGE_KEY}.{entry.entry_id}",
        )
        self._state: AccountingV2State | None = None
        self._epoch_started_at: datetime | None = None
        self._last_processed_evidence_fingerprint: str | None = None
        self._last_processed_reason: str | None = None
        self._legacy_v1_value_kwh: float | None = None
        self._legacy_v1_observed_at: datetime | None = None
        self._latest_entry: ExecutionLedgerEntry | None = None
        self._last_transition_proof: dict[str, Any] | None = None
        self._storage_error: str | None = None
        self._loaded = False
        self._added = False
        self._lock = asyncio.Lock()
        self._feedback_sink: Callable[..., None] | None = None

    def attach_feedback_sink(self, sink: Callable[..., None]) -> None:
        """Attach the tariff planner's accounting-v2-only feedback consumer."""

        if self._feedback_sink is not None:
            raise RuntimeError("Supervisor accounting feedback sink is already attached")
        self._feedback_sink = sink

    @property
    def suggested_object_id(self) -> str:
        return "hoymiles_hit_ems_supervisor_grid_to_battery_energy_v2"

    @property
    def device_info(self) -> DeviceInfo:
        source = self._runtime.source_device
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=source.name_by_user or source.name or NAME,
            manufacturer=source.manufacturer or "Hoymiles",
            model=source.model or "HIT xxL G3",
            sw_version=source.sw_version,
        )

    @property
    def available(self) -> bool:
        return self._loaded and self._storage_error is None and self._state is not None

    @property
    def native_value(self) -> float | None:
        if not self.available:
            return None
        assert self._state is not None
        return self._state.accumulated_energy_kwh

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self._state
        legacy = legacy_v1_diagnostic()
        attributes: dict[str, Any] = {
            "storage_schema_version": STORAGE_SCHEMA_VERSION,
            "storage_epoch": STORAGE_EPOCH,
            "storage_key": STORAGE_KEY,
            "epoch_started_at": (
                _timestamp(self._epoch_started_at)
                if self._epoch_started_at is not None
                else None
            ),
            "accounting_status": (
                "verified"
                if self._latest_entry is not None and self._latest_entry.qualified
                else "unverified"
            ),
            "accounting_source": "physical_grid_to_battery",
            "accounting_fail_closed": True,
            "legacy_value_kwh": self._legacy_v1_value_kwh,
            "legacy_observed_at": (
                _timestamp(self._legacy_v1_observed_at)
                if self._legacy_v1_observed_at is not None
                else None
            ),
            "legacy_version": legacy["storage_epoch"],
            "legacy_included_in_v2": legacy["included_in_v2"],
            "legacy_invalid_reason": legacy["invalid_reason"],
            "storage_error": self._storage_error,
        }
        if state is not None:
            attributes.update(
                {
                    "accounting_contract_id": state.accounting_contract_id,
                    "accepted_interval_count": state.accepted_interval_count,
                    "rejected_interval_count": state.rejected_interval_count,
                    "delivered_power_feedback_w": (
                        state.delivered_power_feedback_w
                    ),
                    "last_update_reason": state.last_update_reason.value,
                }
            )
        if self._last_transition_proof is not None:
            attributes["last_transition_proof"] = self._last_transition_proof
        if self._latest_entry is not None:
            entry = execution_ledger_entry_to_dict(self._latest_entry)
            attributes.update(
                {
                    "current_entry_qualified": self._latest_entry.qualified,
                    "current_reason_code": self._latest_entry.reason_code.value,
                    "current_classification": (
                        self._latest_entry.classification.value
                    ),
                    "current_attributed_power_w": (
                        self._latest_entry.attributed_power_w
                    ),
                    "current_evidence_fingerprint": (
                        self._latest_entry.evidence_fingerprint
                    ),
                    "current_provenance": entry["provenance"],
                }
            )
        return attributes

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        async with self._lock:
            await self._async_ensure_loaded()
            self._added = True
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        self._added = False
        await super().async_will_remove_from_hass()

    async def async_process_active_frame(
        self,
        record: ExecutorRecord,
        frame: ActiveFrame,
        source_entity_ids: Mapping[str, str | None],
    ) -> None:
        """Persist at most one transition for a new physical evidence cohort."""

        entry = build_active_accounting_entry(record, frame, source_entity_ids)
        async with self._lock:
            await self._async_ensure_loaded()
            self._latest_entry = entry
            state = self._state
            if self._storage_error is not None or state is None:
                self._write_if_added()
                return
            relevant = entry_is_accounting_relevant(entry)
            if not relevant and state.anchor_observed_at is None:
                self._write_if_added()
                return
            reason = entry.reason_code.value
            if (
                entry.evidence_fingerprint
                == self._last_processed_evidence_fingerprint
                and reason == self._last_processed_reason
            ):
                self._write_if_added()
                return
            update = accumulate_execution_entry(state, entry)
            transition_proof = self._last_transition_proof
            if (
                update.state.accepted_interval_count != state.accepted_interval_count
                or update.state.rejected_interval_count != state.rejected_interval_count
                or update.state.anchor_evidence_fingerprint
                != state.anchor_evidence_fingerprint
            ):
                entry_payload = execution_ledger_entry_to_dict(entry)
                transition_proof = _validate_transition_proof({
                    "schema_version": 1,
                    "observed_at": entry_payload["observed_at"],
                    "evidence_fingerprint": entry.evidence_fingerprint,
                    "previous_anchor_fingerprint": state.anchor_evidence_fingerprint,
                    "reason": update.reason.value,
                    "accepted": update.accepted,
                    "interval_energy_kwh": update.interval_energy_kwh,
                    "accepted_interval_count": update.state.accepted_interval_count,
                    "rejected_interval_count": update.state.rejected_interval_count,
                    "provenance": entry_payload["provenance"],
                })
            self._state = update.state
            self._last_transition_proof = transition_proof
            self._last_processed_evidence_fingerprint = entry.evidence_fingerprint
            self._last_processed_reason = reason
            await self._async_save()
            transaction = record.transaction
            sink = self._feedback_sink
            if (
                sink is not None
                and entry.qualified
                and update.state.delivered_power_feedback_w > 0.0
                and transaction is not None
            ):
                try:
                    sink(
                        delivered_power_feedback_w=(
                            update.state.delivered_power_feedback_w
                        ),
                        evidence_fingerprint=entry.evidence_fingerprint,
                        observed_at=entry.observed_at,
                        transaction_started_at=transaction.started_at,
                    )
                except Exception:  # noqa: BLE001 - planner feedback is optional
                    _LOGGER.exception(
                        "Tariff planner rejected Supervisor accounting v2 feedback"
                    )
            self._write_if_added()

    async def _async_ensure_loaded(self) -> None:
        if self._loaded:
            return
        raw = await self._store.async_load()
        if raw is None:
            self._state = new_accounting_state()
            self._epoch_started_at = dt_util.utcnow().astimezone(timezone.utc)
            self._legacy_v1_value_kwh = self._legacy_value()
            if self._legacy_v1_value_kwh is not None:
                self._legacy_v1_observed_at = self._epoch_started_at
            self._loaded = True
            await self._async_save()
            return
        try:
            if type(raw) is not dict or frozenset(raw) not in {
                _STORE_WRAPPER_KEYS,
                _STORE_WRAPPER_KEYS | {_TRANSITION_PROOF_KEY},
            }:
                raise ValueError("accounting v2 store wrapper is invalid")
            if raw["schema_version"] != STORAGE_SCHEMA_VERSION:
                raise ValueError("accounting v2 store schema is unsupported")
            if raw["storage_epoch"] != STORAGE_EPOCH:
                raise ValueError("accounting v2 store epoch is unsupported")
            fingerprint = raw["last_processed_evidence_fingerprint"]
            reason = raw["last_processed_reason"]
            if fingerprint is not None and (
                type(fingerprint) is not str or len(fingerprint) != 64
            ):
                raise ValueError("accounting evidence fingerprint is invalid")
            if reason is not None and (type(reason) is not str or len(reason) > 96):
                raise ValueError("accounting entry reason is invalid")
            self._epoch_started_at = _parse_timestamp(raw["epoch_started_at"])
            legacy_value = raw["legacy_v1_value_kwh"]
            if legacy_value is not None and (
                type(legacy_value) not in {int, float}
                or not math.isfinite(float(legacy_value))
                or legacy_value < 0.0
            ):
                raise ValueError("legacy v1 value is invalid")
            legacy_observed_raw = raw["legacy_v1_observed_at"]
            self._legacy_v1_value_kwh = (
                float(legacy_value) if legacy_value is not None else None
            )
            self._legacy_v1_observed_at = (
                _parse_timestamp(legacy_observed_raw)
                if legacy_observed_raw is not None
                else None
            )
            if (self._legacy_v1_value_kwh is None) != (
                self._legacy_v1_observed_at is None
            ):
                raise ValueError("legacy v1 cutover evidence is incomplete")
            restored_state = accounting_state_from_dict(raw["state"])
            transition_proof = _validate_transition_proof(
                raw.get(_TRANSITION_PROOF_KEY)
            )
            if transition_proof is not None and (
                transition_proof["accepted_interval_count"]
                != restored_state.accepted_interval_count
                or transition_proof["rejected_interval_count"]
                != restored_state.rejected_interval_count
            ):
                raise ValueError("accounting transition proof counters diverged")
            self._state = restored_state
            self._last_processed_evidence_fingerprint = fingerprint
            self._last_processed_reason = reason
            self._last_transition_proof = transition_proof
        except (KeyError, TypeError, ValueError, OverflowError):
            self._storage_error = "invalid_accounting_v2_store"
            _LOGGER.exception(
                "Stored EMS Supervisor accounting v2 state is invalid; "
                "the isolated counter remains unavailable"
            )
        self._loaded = True

    async def _async_save(self) -> None:
        state = self._state
        epoch_started_at = self._epoch_started_at
        if state is None or epoch_started_at is None:
            raise RuntimeError("accounting v2 state is not initialized")
        await self._store.async_save(
            {
                "schema_version": STORAGE_SCHEMA_VERSION,
                "storage_epoch": STORAGE_EPOCH,
                "epoch_started_at": _timestamp(epoch_started_at),
                "legacy_v1_value_kwh": self._legacy_v1_value_kwh,
                "legacy_v1_observed_at": (
                    _timestamp(self._legacy_v1_observed_at)
                    if self._legacy_v1_observed_at is not None
                    else None
                ),
                "last_processed_evidence_fingerprint": (
                    self._last_processed_evidence_fingerprint
                ),
                "last_processed_reason": self._last_processed_reason,
                _TRANSITION_PROOF_KEY: self._last_transition_proof,
                "state": accounting_state_to_dict(state),
            }
        )

    def _legacy_value(self) -> float | None:
        legacy_state = self.hass.states.get(_LEGACY_ENTITY_ID)
        if legacy_state is None or legacy_state.state in {
            STATE_UNKNOWN,
            STATE_UNAVAILABLE,
        }:
            return None
        try:
            value = float(legacy_state.state)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value >= 0.0 else None

    def _write_if_added(self) -> None:
        if self._added:
            self.async_write_ha_state()


__all__ = ("HoymilesSupervisorAccountingV2Sensor",)
