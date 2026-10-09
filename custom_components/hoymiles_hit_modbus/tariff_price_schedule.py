"""Policy-neutral import-price schedule for the configured household tariff.

The schedule exposes the marginal AC import cost already used by the tariff
optimizer.  It deliberately contains no charging-plan state or execution
authority, so a pending or disabled tariff policy cannot hide the price of
energy that the home would otherwise import.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Any
from zoneinfo import ZoneInfo

try:  # Package import in Home Assistant; direct import in deterministic tests.
    from .tariff_profiles import (
        MANUAL_OPERATOR,
        configured_tariff_rate,
        get_tariff_profile,
    )
except ImportError:  # pragma: no cover - exercised by the standalone regression.
    from tariff_profiles import MANUAL_OPERATOR, configured_tariff_rate, get_tariff_profile


PRICE_SCHEDULE_SCHEMA = "tariff_import_price_schedule_v1"
PRICE_UNIT = "PLN/kWh"
ENERGY_SIDE = "ac_import"
UTC_MINUTE = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class TariffPriceConfig:
    """Only the tariff inputs needed to publish marginal import prices."""

    tariff_type: str
    g11_price_pln_kwh: float
    low_price_pln_kwh: float
    medium_price_pln_kwh: float
    peak_price_pln_kwh: float
    cheap_windows: tuple[tuple[int, int], ...]
    medium_windows: tuple[tuple[int, int], ...] = ()
    weekend_low_price: bool = False
    polish_holidays_low_price: bool = False
    operator: str = MANUAL_OPERATOR


@dataclass(frozen=True, slots=True)
class TariffPriceInterval:
    """One contiguous UTC interval with a single marginal import price."""

    start_utc: datetime
    end_utc: datetime
    price_pln_kwh_ac: float
    zone: str

    def as_dict(self) -> dict[str, object]:
        return {
            "start_utc": self.start_utc.isoformat(),
            "end_utc": self.end_utc.isoformat(),
            "price_pln_kwh_ac": self.price_pln_kwh_ac,
            "zone": self.zone,
        }


@dataclass(frozen=True, slots=True)
class TariffPriceScheduleSnapshot:
    """A versioned, bounded publication of future marginal import prices."""

    generated_at_utc: datetime
    requested_start_utc: datetime
    requested_end_utc: datetime
    coverage_start_utc: datetime | None
    coverage_end_utc: datetime | None
    intervals: tuple[TariffPriceInterval, ...]
    source_kind: str
    source_id: str
    source_url: str | None
    source_revision: str
    quality: str
    missing_reasons: tuple[str, ...]
    price_basis: str
    served_from_cache: bool = False

    @property
    def available(self) -> bool:
        return bool(self.intervals) and self.quality != "unavailable"

    @property
    def coverage_complete(self) -> bool:
        return (
            self.coverage_start_utc == self.requested_start_utc
            and self.coverage_end_utc == self.requested_end_utc
            and not self.missing_reasons
        )

    def price_at(self, when: datetime) -> float | None:
        stamp = _utc(when)
        for interval in self.intervals:
            if interval.start_utc <= stamp < interval.end_utc:
                return interval.price_pln_kwh_ac
        return None

    def as_attributes(self) -> dict[str, object]:
        coverage_seconds = (
            (self.coverage_end_utc - self.coverage_start_utc).total_seconds()
            if self.coverage_start_utc is not None
            and self.coverage_end_utc is not None
            else 0.0
        )
        return {
            "schema_version": PRICE_SCHEDULE_SCHEMA,
            "price_unit": PRICE_UNIT,
            "energy_side": ENERGY_SIDE,
            "price_basis": self.price_basis,
            "fixed_fees_excluded": True,
            "conversion_applied": "none_native_pln_per_kwh",
            "generated_at_utc": self.generated_at_utc.isoformat(),
            "requested_start_utc": self.requested_start_utc.isoformat(),
            "requested_end_utc": self.requested_end_utc.isoformat(),
            "coverage_start_utc": (
                self.coverage_start_utc.isoformat()
                if self.coverage_start_utc is not None
                else None
            ),
            "coverage_end_utc": (
                self.coverage_end_utc.isoformat()
                if self.coverage_end_utc is not None
                else None
            ),
            "coverage_hours": round(coverage_seconds / 3600.0, 4),
            "coverage_complete": self.coverage_complete,
            "quality": self.quality,
            "missing_reasons": list(self.missing_reasons),
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "source_url": self.source_url,
            "source_revision": self.source_revision,
            "served_from_cache": self.served_from_cache,
            "interval_count": len(self.intervals),
            "intervals": [item.as_dict() for item in self.intervals],
            "policy_independent": True,
        }


@dataclass(frozen=True, slots=True)
class TariffEnergyCost:
    """Cost and honest bounds for energy lacking an intra-interval profile."""

    energy_kwh_ac: float
    estimated_cost_pln: float
    minimum_cost_pln: float
    maximum_cost_pln: float
    exact: bool
    allocation_basis: str
    interval_count: int


def equivalent_tariff_publication(
    before: object, after: object, *, now: datetime,
) -> bool:
    """Compare price meaning while allowing a fresh rolling publication.

    A new report time is not a new price. Coverage may roll past elapsed time
    and extend, but must not lose any future interval used by the incumbent.
    Unknown source implementations retain their previous equality contract.
    """
    if before == after:
        return True
    if not isinstance(before, TariffPriceScheduleSnapshot) or not isinstance(
        after, TariffPriceScheduleSnapshot
    ):
        return False
    try:
        stamp = _utc(now)
        if (
            not before.coverage_complete or not after.coverage_complete
            or not before.available or not after.available
            or before.coverage_start_utc is None or after.coverage_start_utc is None
            or before.coverage_end_utc is None or after.coverage_end_utc is None
            or not before.coverage_start_utc <= stamp < before.coverage_end_utc
            or not after.coverage_start_utc <= stamp < after.coverage_end_utc
            or after.coverage_end_utc < before.coverage_end_utc
            or any(getattr(before, key) != getattr(after, key) for key in (
                'source_kind', 'source_id', 'source_revision', 'quality',
                'missing_reasons', 'price_basis',
            ))
        ):
            return False
        # Outer edges move every minute. Internal boundaries and prices are
        # the economic contract, shared with the source's change signature.
        def meaning(value: TariffPriceScheduleSnapshot) -> tuple:
            return tuple((
                row.start_utc if index else None,
                row.end_utc if index < len(value.intervals) - 1 else None,
                row.price_pln_kwh_ac, row.zone,
            ) for index, row in enumerate(value.intervals))
        return meaning(before) == meaning(after)
    except (AttributeError, TypeError, ValueError):
        return False


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc)


def normalize_import_price(value: object, unit: str) -> float:
    """Normalize one explicit price exactly once while preserving its sign."""

    if value is None:
        raise ValueError("price_missing")
    try:
        price = float(value)
    except (TypeError, ValueError) as err:
        raise ValueError("price_invalid") from err
    if not math.isfinite(price):
        raise ValueError("price_non_finite")
    normalized_unit = unit.strip().upper().replace(" ", "")
    if normalized_unit == "PLN/KWH":
        return price
    if normalized_unit == "PLN/MWH":
        return price / 1000.0
    raise ValueError("price_unit_unsupported")


def _source_metadata(
    schedule: TariffPriceConfig,
) -> tuple[str, str, str | None, str, dict[str, object]]:
    profile = (
        get_tariff_profile(schedule.operator, schedule.tariff_type)
        if schedule.operator != MANUAL_OPERATOR
        else None
    )
    if schedule.operator != MANUAL_OPERATOR and profile is None:
        source_kind = "official_profile"
        source_id = f"official:{schedule.operator}:{schedule.tariff_type}:missing"
        source_url = None
        price_basis = "marginal_gross_import_cost_profile_missing"
        profile_payload: dict[str, object] = {"profile_missing": True}
    elif profile is not None:
        source_kind = "official_profile"
        source_id = (
            f"official:{profile.operator}:{profile.tariff_type}:"
            f"{profile.data_version}"
        )
        source_url = profile.source_url
        price_basis = (
            "marginal_gross_import_cost_including_energy_variable_distribution_"
            "quality_res_and_cogeneration_excluding_fixed_fees"
        )
        profile_payload = {
            "data_version": profile.data_version,
            "valid_from": profile.valid_from.isoformat(),
            "valid_until": profile.valid_until.isoformat(),
            "schedule_key": profile.schedule_key,
            "source_url": profile.source_url,
        }
    else:
        source_kind = "manual_configuration"
        source_id = f"manual:{schedule.tariff_type}"
        source_url = None
        price_basis = (
            "configured_marginal_import_cost_component_completeness_unverified"
        )
        profile_payload = {}
    return source_kind, source_id, source_url, price_basis, profile_payload


def tariff_source_revision(schedule: TariffPriceConfig) -> str:
    """Return a semantic revision unaffected by time, planner state or notify."""

    _, source_id, source_url, price_basis, profile_payload = _source_metadata(schedule)
    payload = {
        "schema": PRICE_SCHEDULE_SCHEMA,
        "source_id": source_id,
        "source_url": source_url,
        "price_basis": price_basis,
        "tariff_type": schedule.tariff_type,
        "operator": schedule.operator,
        "g11": schedule.g11_price_pln_kwh,
        "low": schedule.low_price_pln_kwh,
        "medium": schedule.medium_price_pln_kwh,
        "peak": schedule.peak_price_pln_kwh,
        "cheap_windows": schedule.cheap_windows,
        "medium_windows": schedule.medium_windows,
        "weekend_low": schedule.weekend_low_price,
        "holiday_low": schedule.polish_holidays_low_price,
        "profile": profile_payload,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _schedule_validation_errors(schedule: TariffPriceConfig) -> tuple[str, ...]:
    missing: list[str] = []
    for name in (
        "g11_price_pln_kwh",
        "low_price_pln_kwh",
        "medium_price_pln_kwh",
        "peak_price_pln_kwh",
    ):
        value = getattr(schedule, name, None)
        if value is None:
            missing.append(f"{name}:missing")
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            missing.append(f"{name}:invalid")
            continue
        if not math.isfinite(numeric):
            missing.append(f"{name}:non_finite")
    if schedule.operator != MANUAL_OPERATOR and get_tariff_profile(
        schedule.operator, schedule.tariff_type
    ) is None:
        missing.append("official_profile:missing")
    if schedule.operator == MANUAL_OPERATOR and schedule.tariff_type != "G11":
        windows = list(schedule.cheap_windows)
        if schedule.tariff_type == "G13":
            windows.extend(schedule.medium_windows)
        for index, pair in enumerate(windows):
            if (
                not isinstance(pair, tuple)
                or len(pair) != 2
                or any(
                    not isinstance(value, int) or not 0 <= value < 24 * 60
                    for value in pair
                )
            ):
                missing.append(f"price_window_{index}:invalid")
    return tuple(missing)


def build_tariff_price_schedule(
    schedule: TariffPriceConfig,
    *,
    start: datetime,
    end: datetime,
    local_zone: ZoneInfo,
    generated_at: datetime | None = None,
) -> TariffPriceScheduleSnapshot:
    """Build exact UTC price boundaries over the available configured horizon.

    The scan advances on real UTC minutes.  Local calendar rules are evaluated
    after conversion, so spring and autumn DST days naturally contain 23 and
    25 hours.  Prices can be positive, zero or negative; missing and non-finite
    values fail closed.
    """

    requested_start = _utc(start)
    requested_end = _utc(end)
    generated = _utc(generated_at or datetime.now(timezone.utc))
    if requested_end <= requested_start:
        raise ValueError("price schedule end must follow start")
    source_kind, source_id, source_url, price_basis, _ = _source_metadata(schedule)
    try:
        revision = tariff_source_revision(schedule)
    except (TypeError, ValueError):
        revision = "unavailable"
    validation_errors = _schedule_validation_errors(schedule)
    if validation_errors:
        return TariffPriceScheduleSnapshot(
            generated,
            requested_start,
            requested_end,
            None,
            None,
            (),
            source_kind,
            source_id,
            source_url,
            revision,
            "unavailable",
            validation_errors,
            price_basis,
        )

    intervals: list[TariffPriceInterval] = []
    cursor = requested_start
    interval_start = requested_start
    current_rate: tuple[float, str] | None = None
    missing_reasons: list[str] = []

    while cursor < requested_end:
        try:
            price_raw, zone = configured_tariff_rate(
                cursor.astimezone(local_zone),
                operator=schedule.operator,
                tariff_type=schedule.tariff_type,
                g11_price_pln_kwh=schedule.g11_price_pln_kwh,
                low_price_pln_kwh=schedule.low_price_pln_kwh,
                medium_price_pln_kwh=schedule.medium_price_pln_kwh,
                peak_price_pln_kwh=schedule.peak_price_pln_kwh,
                cheap_windows=schedule.cheap_windows,
                medium_windows=schedule.medium_windows,
                weekend_low_price=schedule.weekend_low_price,
                polish_holidays_low_price=schedule.polish_holidays_low_price,
            )
            price = normalize_import_price(price_raw, PRICE_UNIT)
        except (TypeError, ValueError) as err:
            missing_reasons.append(str(err))
            break
        rate = (price, zone)
        if current_rate is None:
            current_rate = rate
            interval_start = cursor
        elif rate != current_rate:
            intervals.append(
                TariffPriceInterval(
                    interval_start,
                    cursor,
                    current_rate[0],
                    current_rate[1],
                )
            )
            interval_start = cursor
            current_rate = rate

        next_minute = cursor.replace(second=0, microsecond=0) + UTC_MINUTE
        cursor = min(next_minute, requested_end)

    coverage_end = cursor
    if current_rate is not None and interval_start < coverage_end:
        intervals.append(
            TariffPriceInterval(
                interval_start,
                coverage_end,
                current_rate[0],
                current_rate[1],
            )
        )
    if not intervals:
        quality = "unavailable"
        coverage_start: datetime | None = None
        coverage_end_value: datetime | None = None
    else:
        coverage_start = requested_start
        coverage_end_value = coverage_end
        quality = (
            "partial"
            if coverage_end < requested_end or missing_reasons
            else (
                "official_verified"
                if source_kind == "official_profile"
                else "configured_unverified"
            )
        )
    return TariffPriceScheduleSnapshot(
        generated,
        requested_start,
        requested_end,
        coverage_start,
        coverage_end_value,
        tuple(intervals),
        source_kind,
        source_id,
        source_url,
        revision,
        quality,
        tuple(missing_reasons),
        price_basis,
    )


def estimate_uniform_energy_cost(
    snapshot: TariffPriceScheduleSnapshot,
    *,
    start: datetime,
    end: datetime,
    energy_kwh_ac: object,
) -> TariffEnergyCost:
    """Price energy with a uniform-power estimate and explicit price bounds."""

    start_utc = _utc(start)
    end_utc = _utc(end)
    if end_utc <= start_utc:
        raise ValueError("cost interval end must follow start")
    energy = normalize_import_price(energy_kwh_ac, "PLN/kWh")
    if energy < 0.0:
        raise ValueError("import energy must be non-negative")
    duration = (end_utc - start_utc).total_seconds()
    overlaps: list[tuple[float, float]] = []
    covered = 0.0
    for interval in snapshot.intervals:
        overlap_start = max(start_utc, interval.start_utc)
        overlap_end = min(end_utc, interval.end_utc)
        seconds = max((overlap_end - overlap_start).total_seconds(), 0.0)
        if seconds <= 0.0:
            continue
        overlaps.append((seconds, interval.price_pln_kwh_ac))
        covered += seconds
    if not overlaps or not math.isclose(covered, duration, abs_tol=1e-6):
        raise ValueError("price_schedule_coverage_missing")
    estimated = sum(energy * seconds / duration * price for seconds, price in overlaps)
    prices = [price for _, price in overlaps]
    minimum = energy * min(prices)
    maximum = energy * max(prices)
    exact = energy == 0.0 or math.isclose(minimum, maximum, abs_tol=1e-12)
    return TariffEnergyCost(
        energy,
        estimated,
        minimum,
        maximum,
        exact,
        "exact_single_price" if exact else "uniform_power_estimate_with_price_bounds",
        len(overlaps),
    )


class TariffPriceScheduleCache:
    """Keep the last valid semantic publication only inside its coverage."""

    def __init__(self) -> None:
        self._snapshot: TariffPriceScheduleSnapshot | None = None

    def select(
        self,
        candidate: TariffPriceScheduleSnapshot,
        *,
        now: datetime,
    ) -> TariffPriceScheduleSnapshot:
        stamp = _utc(now)
        if candidate.available:
            self._snapshot = candidate
            return candidate
        cached = self._snapshot
        if (
            cached is not None
            and cached.coverage_start_utc is not None
            and cached.coverage_end_utc is not None
            and cached.coverage_start_utc <= stamp < cached.coverage_end_utc
        ):
            return replace(
                cached,
                generated_at_utc=candidate.generated_at_utc,
                quality=(
                    "cached_verified"
                    if cached.quality == "official_verified"
                    else "cached_configured"
                ),
                missing_reasons=candidate.missing_reasons,
                served_from_cache=True,
            )
        return candidate
