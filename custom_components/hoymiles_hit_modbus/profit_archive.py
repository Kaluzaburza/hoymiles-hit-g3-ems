"""Small, independent economic archive. Never feeds an EMS decision.

Energy is an estimate from the existing grid-power stream, not a billing meter.
UTC hours preserve both occurrences of a DST hour; the local date is frozen too.
Prices and attribution are fixed at observation, never recomputed on a read.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from .baseline_energy_timeline import (
    BaselineEnergyInputs, BaselineForecastSlot, build_baseline_energy_timeline,
)

SCHEMA = 1
MAX_GAP_SECONDS = 120
FIELDS = (
    "seconds", "covered_seconds", "import_kwh", "export_kwh",
    "priced_import_kwh", "priced_export_kwh", "import_cost", "export_revenue",
    "buy_covered_seconds", "sell_covered_seconds", "model_seconds",
    "model_cash_delta", "inventory_delta",
)
EMS_CATEGORIES = frozenset({"rce_export", "pv_delay", "tariff_charge", "tariff_support", "rcm"})
CATEGORIES = EMS_CATEGORIES | {"self_use", "manual", "unclassified", "gap"}


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Rate:
    start: float
    end: float
    value: float | None
    source: str
    zone: str
    basis: str


@dataclass(frozen=True)
class Observation:
    at: float
    grid_kw: float | None  # positive export, negative import
    category: str
    buy: tuple[Rate, ...] = ()
    sell: tuple[Rate, ...] = ()
    pv_kw: float | None = None
    load_kw: float | None = None
    model: BaselineEnergyInputs | None = None
    context: str = ""  # entry/source/settings identity, NOT a rolling revision


def period_bounds(period: str, selected: str, zone: str):
    """Calendar periods, ISO Monday weeks; absolute seconds respect DST."""
    day = date.fromisoformat(selected)
    if period == "day":
        start, end = day, day + timedelta(days=1)
    elif period == "week":
        start = day - timedelta(days=day.weekday())
        end = start + timedelta(days=7)
    elif period == "month":
        start = day.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    elif period == "year":
        start, end = date(day.year, 1, 1), date(day.year + 1, 1, 1)
    else:
        raise ValueError("invalid_period")
    tz = ZoneInfo(zone)
    return start, end, datetime.combine(start, time.min, tz).timestamp(), datetime.combine(end, time.min, tz).timestamp()


def _rate(rows, at):
    matches = [r for r in rows if r.start <= at < r.end]
    return replace(matches[0], value=finite(matches[0].value)) if len(matches) == 1 else Rate(at, at, None, "unavailable", "unknown", "unknown")


def _model_settings(model):
    return tuple(getattr(model, key) for key in (
        "battery_capacity_kwh", "reserve_soc_percent", "hardware_minimum_soc_percent",
        "hardware_maximum_soc_percent", "pv_to_battery_efficiency",
        "battery_to_home_efficiency", "system_ac_power_kw", "zero_export_confirmed", "export_allowed",
    ))


def _meta(buy, sell, local):
    return {"buy_source": buy.source, "buy_zone": buy.zone, "buy_net": buy.value,
            "buy_basis": buy.basis, "sell_source": sell.source, "sell_net": sell.value,
            "sell_basis": sell.basis, "local_hour": local.hour,
            "utc_offset_minutes": int(local.utcoffset().total_seconds() / 60)}


def _flow_groups(tariffs, *, purchase):
    """Aggregate the entire selected period BEFORE price-table pagination.

    Purchase zones and sale providers are independent dimensions. Missing
    prices retain their kWh; an average uses only energy actually priced.
    """
    side, flow = ("buy", "import") if purchase else ("sell", "export")
    amount = "import_cost" if purchase else "export_revenue"
    groups = {}
    for row in tariffs:
        if not row["covered_seconds"]:
            continue
        key = (row[f"{side}_source"], row["buy_zone"] if purchase else "all")
        group = groups.setdefault(key, dict(source=key[0], zone=key[1], energy_kwh=0.,
            priced_kwh=0., amount_pln=0., price_seconds=0., minimum_price=None, maximum_price=None))
        group["energy_kwh"] += row[f"{flow}_kwh"]
        group["priced_kwh"] += row[f"priced_{flow}_kwh"]
        group["amount_pln"] += row[amount]
        group["price_seconds"] += row[f"{side}_covered_seconds"]
        price = row[f"{side}_net"]
        if price is not None and row[f"priced_{flow}_kwh"] > 0:
            for field, fn in (("minimum_price", min), ("maximum_price", max)):
                group[field] = price if group[field] is None else fn(group[field], price)
    for group in groups.values():
        group["average_price"] = group["amount_pln"] / group["priced_kwh"] if group["priced_kwh"] > 0 else None
        group["unpriced_kwh"] = max(0., group["energy_kwh"] - group["priced_kwh"])
        if not group.pop("price_seconds") or (group["energy_kwh"] > 0 and not group["priced_kwh"]):
            group["amount_pln"] = None
        for key, value in group.items():
            if isinstance(value, float):
                group[key] = round(value, 6)
    return sorted(groups.values(), key=lambda row: (row["source"], row["zone"]))


class ProfitAccumulator:
    """O(1) observations plus hour/price boundaries; bounded memory, no I/O."""
    def __init__(self, zone: str):
        self.zone = ZoneInfo(zone)
        self.previous: Observation | None = None
        self.rows: dict[str, dict] = {}
        self.virtual_soc = None
        self.inventory = 0.0
        self.stock_price = None
        self.model_context = None

    def _reset_model(self):
        self.virtual_soc = self.stock_price = self.model_context = None
        self.inventory = 0.0

    def observe(self, current: Observation):
        previous = self.previous
        # Duplicate/out-of-order input never moves the accounting cursor back.
        if previous and current.at <= previous.at:
            return
        self.previous = current
        if previous is None:
            return
        duration = current.at - previous.at
        valid = (duration <= MAX_GAP_SECONDS and finite(previous.grid_kw) is not None
                 and finite(current.grid_kw) is not None and previous.context == current.context)
        if duration > 86400:
            # A stopped HA process cannot create years of empty hourly rows.
            self._reset_model()
            return
        cuts = {previous.at, current.at}
        boundary = (int(previous.at // 3600) + 1) * 3600
        while boundary < current.at:
            cuts.add(boundary)
            boundary += 3600
        for rate in (*previous.buy, *previous.sell):
            cuts.update(x for x in (rate.start, rate.end) if previous.at < x < current.at)
        cuts = sorted(cuts)
        for left, right in zip(cuts, cuts[1:]):
            seconds = right - left
            buy, sell = _rate(previous.buy, left), _rate(previous.sell, left)
            local = datetime.fromtimestamp(left, self.zone)
            category = previous.category if valid and previous.category in CATEGORIES else "gap"
            metadata = _meta(buy, sell, local)
            hour = int(left // 3600) * 3600
            raw = json.dumps([hour, category, metadata], sort_keys=True, allow_nan=False)
            key = hashlib.sha256(raw.encode()).hexdigest()[:32]
            if key not in self.rows:
                if len(self.rows) >= 4096:
                    raise ValueError("archive_buffer_full")
                self.rows[key] = {"id": key, "hour": hour, "day": local.date().isoformat(),
                                  "category": category, "meta": metadata, **dict.fromkeys(FIELDS, 0.0)}
            row = self.rows[key]
            row["seconds"] += seconds
            if not valid:
                self._reset_model()
                continue
            row["covered_seconds"] += seconds
            # Left-held power is the same signal semantics as Recorder's states.
            imported = max(0, -previous.grid_kw) * seconds / 3600
            exported = max(0, previous.grid_kw) * seconds / 3600
            row["import_kwh"] += imported
            row["export_kwh"] += exported
            if buy.value is not None:
                row["buy_covered_seconds"] += seconds
                row["priced_import_kwh"] += imported
                row["import_cost"] += imported * buy.value
            if sell.value is not None:
                row["sell_covered_seconds"] += seconds
                row["priced_export_kwh"] += exported
                row["export_revenue"] += exported * sell.value
            self._compare(previous, current, left, right, buy, sell, row)

    def _compare(self, before, after, left, right, buy, sell, row):
        """Reuse the existing Self-Use model with OBSERVED PV/LOAD, not a plan.

        Starts at a confirmed EMS action and then carries the virtual battery
        through subsequent Self-Use. Both alternatives start with the same SOC.
        Inventory correction prevents treating depletion as free profit.
        Gaps or changed system settings end a segment, never silently bridge it.
        """
        model = before.model
        if (model is None or after.model is None or buy.value is None or sell.value is None
                or before.pv_kw is None or before.load_kw is None
                or before.category in {"manual", "unclassified", "gap"}
                or _model_settings(model) != _model_settings(after.model)
                or finite(after.model.current_soc_percent) is None
                or not 0 <= after.model.current_soc_percent <= 100):
            self._reset_model()
            return
        structural = (before.context, *_model_settings(model))
        if self.model_context != structural:
            self._reset_model()
        if self.virtual_soc is None:
            if before.category not in EMS_CATEGORIES:
                return
            self.virtual_soc = model.current_soc_percent
            self.stock_price = max(0.0, buy.value)
            self.model_context = structural
        if self.virtual_soc is None:
            self._reset_model()
            return
        start = datetime.fromtimestamp(left, timezone.utc)
        end = datetime.fromtimestamp(right, timezone.utc)
        result = build_baseline_energy_timeline(
            replace(model, generated_at=start, current_soc_percent=self.virtual_soc),
            [BaselineForecastSlot(start, end, before.pv_kw, before.load_kw,
                                  "observed", "observed", "sampled_power", "sampled_power")],
        )
        point = result["points"][0]
        if (point["quality"] != "current" or point["grid_import_kw"] is None
                or point["grid_export_kw"] is None):
            self._reset_model()
            return
        self.virtual_soc = point["soc_end_percent"]
        factor = (right - before.at) / (after.at - before.at)
        actual_soc = model.current_soc_percent + factor * (
            after.model.current_soc_percent - model.current_soc_percent)
        inventory = ((actual_soc - self.virtual_soc) / 100 * model.battery_capacity_kwh
                     * model.battery_to_home_efficiency * self.stock_price)
        seconds = right - left
        actual_cash = (max(0, before.grid_kw) * sell.value - max(0, -before.grid_kw) * buy.value) * seconds / 3600
        base_cash = (point["grid_export_kw"] * sell.value - point["grid_import_kw"] * buy.value) * seconds / 3600
        row["model_cash_delta"] += actual_cash - base_cash
        row["inventory_delta"] += inventory - self.inventory
        row["model_seconds"] += seconds
        self.inventory = inventory

    def drain(self):
        result = list(self.rows.values())
        self.rows = {}
        return result


class ProfitArchive:
    """One installation-specific SQLite file. Call ONLY on HA's executor.

    Five-minute batches, UPSERT hour aggregates, no per-sample rows/Recorder
    writes. The last batch ID and data commit together, making retries safe.
    No age purge: day/week/month/year views continue beyond Recorder retention.
    """
    def __init__(self, path, identity):
        self.path = Path(path)
        self.identity = identity

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self, now):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            stored = dict(db.execute("SELECT key,value FROM metadata"))
            identity = json.dumps(self.identity, sort_keys=True)
            if stored and (stored.get("schema") != str(SCHEMA) or stored.get("identity") != identity):
                raise ValueError("archive_identity_or_schema_mismatch")
            for key, value in {"schema": str(SCHEMA), "identity": identity, "started_at": str(now)}.items():
                db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", (key, value))
            columns = ",".join(f"{field} REAL NOT NULL" for field in FIELDS)
            db.execute(f"CREATE TABLE IF NOT EXISTS hours (id TEXT PRIMARY KEY, hour INTEGER NOT NULL, day TEXT NOT NULL, category TEXT NOT NULL, meta TEXT NOT NULL,{columns})")
            db.execute("CREATE INDEX IF NOT EXISTS profit_day ON hours(day)")

    def write(self, rows, batch_id, now):
        with self._connect() as db:
            last = db.execute("SELECT value FROM metadata WHERE key='last_batch'").fetchone()
            if last and last[0] == batch_id:
                return
            placeholders = ",".join("?" for _ in range(5 + len(FIELDS)))
            additions = ",".join(f"{field}={field}+excluded.{field}" for field in FIELDS)
            sql = f"INSERT INTO hours VALUES ({placeholders}) ON CONFLICT(id) DO UPDATE SET {additions}"
            for row in rows:
                if row["category"] not in CATEGORIES or any(finite(row[k]) is None for k in FIELDS):
                    raise ValueError("invalid_archive_row")
                db.execute(sql, (row["id"], row["hour"], row["day"], row["category"],
                                 json.dumps(row["meta"], sort_keys=True), *(row[k] for k in FIELDS)))
            for key, value in (("last_batch", batch_id), ("updated_at", str(now))):
                db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, value))

    def read(self, period, selected, now):
        start, end, left, right = period_bounds(period, selected, self.identity["timezone"])
        sums = ",".join(f"SUM({field}) AS {field}" for field in FIELDS)
        where = " WHERE day>=? AND day<? "
        bounds = (start.isoformat(), end.isoformat())
        bucket_sql = "CAST(hour AS TEXT)" if period == "day" else "substr(day,1,7)" if period == "year" else "day"
        with self._connect() as db:
            db.execute("PRAGMA query_only=ON")
            metadata = dict(db.execute("SELECT key,value FROM metadata"))
            # Aggregate in SQLite: a year's raw rows never travel to HA or a browser.
            categories = {r["category"]: dict(r) for r in db.execute(
                f"SELECT category,COUNT(*) AS row_count,{sums} FROM hours" + where + "GROUP BY category", bounds)}
            measured = {r["key"]: dict(r) for r in db.execute(
                f"SELECT {bucket_sql} AS key,{sums} FROM hours" + where + f"GROUP BY {bucket_sql} ORDER BY MIN(hour)", bounds)}
            tariffs = []
            for row in db.execute(f"SELECT meta,MIN(day) AS first_day,MAX(day) AS last_day,{sums} FROM hours" + where + "GROUP BY meta ORDER BY MIN(hour),meta LIMIT 100001", bounds):
                item = dict(row)
                tariffs.append({**json.loads(item.pop("meta")), **item})
            if len(tariffs) > 100000:
                raise ValueError("archive_query_limit")
            oldest = db.execute("SELECT MIN(day) FROM hours").fetchone()[0]
        totals = {field: sum(c[field] for c in categories.values()) for field in FIELDS}
        row_count = sum(c.pop("row_count") for c in categories.values())
        series = {}
        # Explicit empty bins prevent a chart from joining points over an outage.
        cursor = left
        while cursor < right:
            local = datetime.fromtimestamp(cursor, ZoneInfo(self.identity["timezone"]))
            bucket = str(int(cursor)) if period == "day" else local.strftime("%Y-%m") if period == "year" else local.date().isoformat()
            series.setdefault(bucket, measured.get(bucket, {"key": bucket, **dict.fromkeys(FIELDS, 0.0)}))
            cursor += 3600
        elapsed = max(0, min(now, right) - left)
        totals["expected_seconds"] = elapsed
        totals["coverage_percent"] = min(100, totals["covered_seconds"] / elapsed * 100) if elapsed else 0
        totals["model_coverage_percent"] = min(100, totals["model_seconds"] / totals["covered_seconds"] * 100) if totals["covered_seconds"] else 0
        totals["benefit_status"] = ("unavailable" if not totals["model_seconds"] else
                                    "complete" if elapsed and totals["model_seconds"] >= elapsed - 1 else "partial")
        purchases = _flow_groups(tariffs, purchase=True)
        sales = _flow_groups(tariffs, purchase=False)
        for group in [totals, *categories.values(), *series.values(), *tariffs]:
            covered = group["covered_seconds"]
            priced = group["buy_covered_seconds"] or group["sell_covered_seconds"]
            group["cash_balance"] = (group["export_revenue"] - group["import_cost"]) if covered and priced else None
            group["estimated_benefit"] = (group["model_cash_delta"] + group["inventory_delta"]) if group["model_seconds"] else None
            group["unpriced_import_kwh"] = max(0, group["import_kwh"] - group["priced_import_kwh"])
            group["unpriced_export_kwh"] = max(0, group["export_kwh"] - group["priced_export_kwh"])
            for key, value in tuple(group.items()):
                if isinstance(value, float):
                    group[key] = round(value, 6)
        return {"schema": SCHEMA, "period": period, "start": start.isoformat(), "end": end.isoformat(),
                "timezone": self.identity["timezone"], "currency": "PLN", "price_basis": "net",
                "started_at": float(metadata["started_at"]), "updated_at": float(metadata.get("updated_at", metadata["started_at"])),
                "oldest_day": oldest, "totals": totals,
                "categories": [{"category": k, **v} for k, v in categories.items()],
                "series": list(series.values()), "tariffs": tariffs,
                "purchases": purchases, "sales": sales,
                "archive_bytes": self.path.stat().st_size, "row_count": row_count,
                "energy_basis": "sampled_grid_power", "historical_backfill": False,
                "benefit_basis": "observed_self_use_model_with_inventory_correction",
                "fixed_fees_and_battery_wear_excluded": True}
