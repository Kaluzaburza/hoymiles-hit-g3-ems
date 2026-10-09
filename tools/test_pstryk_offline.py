"""Pstryk input/profile/cache contracts; all HTTP and prices are synthetic.

This suite does not import Home Assistant or send network/device commands.
It is not joint-planner, real Recorder or inverter acceptance.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "hoymiles_hit_modbus"))

from pstryk_prices import (  # noqa: E402
    PriceCache, parse_prices, price_projection, request_window,
)
from dynamic_price_profile import DynamicPriceProfile  # noqa: E402
from pstryk_client import (  # noqa: E402
    PstrykClient, PstrykAPIError, decode_public_response, ENDPOINT, ORIGIN,
)

UTC = timezone.utc
ZONE = ZoneInfo("Europe/Warsaw")
START = datetime(2026, 9, 29, 0, tzinfo=UTC)
SCOPE = "test-entry:public-net"


def frame(start=START, net="0.40", gross="0.55", **changes):
    row = {
        "start": start.isoformat(),
        "end": (start + timedelta(hours=1)).isoformat(),
        "priceNet": net, "priceGross": gross,
        "isCheap": True, "isExpensive": False,
    }
    row.update(changes)
    return row


def snapshot(rows=None, *, fetched=START, start=START, hours=24, scope=SCOPE):
    return parse_prices(
        {"frames": [frame()] if rows is None else rows}, source_scope=scope,
        fetched_at=fetched, window_start=start,
        window_end=start + timedelta(hours=hours),
    )


class PricesTest(unittest.TestCase):
    def test_public_net_used_unchanged_for_both_directions(self):
        row = snapshot([frame(net="-0,00789", gross="9.99")]).hours[0]
        self.assertEqual(row.buy, Decimal("-0.00789"))
        self.assertEqual(row.sell, row.buy)

    def test_zero_negative_and_missing_are_distinct(self):
        for value, expected in [("0", Decimal(0)), ("-0.2", Decimal("-0.2")), (None, None)]:
            with self.subTest(value=value):
                self.assertEqual(snapshot([frame(net=value)]).hours[0].buy, expected)

    def test_invalid_prices_fail_closed(self):
        for value in [True, False, "NaN", "Infinity", "-Infinity", "1e999", "1e-999", {}, [], "", "none"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                snapshot([frame(net=value)])

    def test_unit_is_explicit_and_does_not_guess_mwh(self):
        with self.assertRaisesRegex(ValueError, "price_unit"):
            parse_prices({"frames": [frame()]}, source_scope=SCOPE, fetched_at=START,
                         window_start=START, window_end=START + timedelta(days=1), unit="PLN/MWh")

    def test_semantic_revision_ignores_poll_flags_and_decimal_spelling(self):
        before = snapshot()
        row = frame(net="0.4000", gross="999")
        row["isCheap"] = False
        row["unused_metadata"] = {"anything": 999}
        after = snapshot([row], fetched=START + timedelta(minutes=45))
        self.assertEqual(before.revision, after.revision)
        self.assertNotEqual(before.fetched_at, after.fetched_at)

    def test_price_coverage_scope_and_profile_change_revision(self):
        before = snapshot()
        variants = [snapshot([frame(net="0.41")]), snapshot([frame(net=None)]),
                    snapshot([], hours=24), snapshot(hours=25),
                    snapshot(scope="different-entry")]
        for value in variants:
            self.assertNotEqual(before.revision, value.revision)

    def test_pair_revision_is_atomic(self):
        before = snapshot()
        bad = frame(START + timedelta(hours=1), net="NaN")
        cache = PriceCache(SCOPE)
        cache.accept(before, now=START)
        with self.assertRaises(ValueError):
            cache.accept(snapshot([frame(net="0.1"), bad]), now=START)
        self.assertEqual(cache.view(now=START).snapshot.revision, before.revision)

    def test_sorted_intervals_identical_duplicate_deduplicated(self):
        rows = [frame(START + timedelta(hours=1)), frame(), frame()]
        result = snapshot(rows)
        self.assertEqual(len(result.hours), 2)
        self.assertEqual(result.hours[0].start, START)

    def test_conflicting_duplicate_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            snapshot([frame(), frame(net="1")])

    def test_bad_time_hour_alignment_overlap_and_window(self):
        cases = [frame(start=START + timedelta(minutes=30)),
                 frame(end=(START + timedelta(minutes=30)).isoformat()),
                 frame(start=START - timedelta(hours=1)),
                 frame(start=START + timedelta(hours=24)),
                 frame(start=START, end=START.isoformat()),
                 frame(start=START, end="2026-09-29T01:00:00"),
                 frame(start=START, end="bad")]
        cases.append({**frame(), "start": "2026-09-29T00:00:00"})
        for row in cases:
            with self.subTest(row=row), self.assertRaises(ValueError):
                snapshot([row])

    def test_frame_count_and_payload_shape_bounds(self):
        for payload in [None, [], {}, {"frames": {}}, {"frames": [None]}, {"frames": [frame()] * 51}]:
            with self.subTest(payload=type(payload)), self.assertRaises(ValueError):
                parse_prices(payload, source_scope=SCOPE, fetched_at=START,
                             window_start=START, window_end=START + timedelta(days=1))

    def test_empty_response_and_missing_pricing_are_explicit(self):
        self.assertEqual(snapshot([]).hours, ())
        empty = snapshot([frame(net=None)])
        self.assertIsNone(empty.hours[0].buy)
        self.assertIsNone(empty.hours[0].sell)

    def test_coverage_never_fills_gap_or_extends_last_hour(self):
        result = snapshot([frame(), frame(START + timedelta(hours=2))])
        self.assertTrue(result.covers(START, START + timedelta(hours=1)))
        self.assertFalse(result.covers(START, START + timedelta(hours=3)))
        self.assertIsNone(result.at(START + timedelta(hours=1)))
        self.assertIsNone(result.at(START + timedelta(hours=3)))

    def test_missing_net_never_falls_back_to_gross_or_zero(self):
        result = snapshot([frame(net=None)])
        self.assertFalse(result.covers(START, START + timedelta(hours=1), directions=("buy",)))
        self.assertFalse(result.covers(START, START + timedelta(hours=1)))
        with self.assertRaises(ValueError):
            result.slices(START, START + timedelta(hours=1))

    def test_partial_current_hour_has_only_remaining_energy_time(self):
        result = snapshot()
        slices = result.slices(START + timedelta(minutes=20), START + timedelta(hours=1))
        self.assertEqual([x.hours for x in slices], [1 / 6, 0.5])
        self.assertEqual({x.source_start for x in slices}, {START})
        self.assertAlmostEqual(sum(6 * x.hours for x in slices), 4)
        self.assertEqual({(x.buy, x.sell) for x in slices}, {(Decimal("0.4"), Decimal("0.4"))})

    def test_dst_two_day_windows_and_repeated_local_hour(self):
        for date, expected in [("2026-03-29", 47), ("2026-09-29", 48), ("2026-10-25", 49)]:
            day = datetime.fromisoformat(date).replace(tzinfo=ZONE)
            begin, end = request_window(day)
            self.assertEqual((end - begin).total_seconds() / 3600, expected)
            rows = [frame(begin + timedelta(hours=n)) for n in range(expected)]
            value = snapshot(rows, fetched=begin, start=begin, hours=expected)
            self.assertTrue(value.covers(begin, end))
            self.assertEqual(len(value.slices(begin, end)), expected * 2)
            if expected == 49:
                repeated = [x for x in value.hours if x.start.astimezone(ZONE).date() == day.date()
                            and x.start.astimezone(ZONE).hour == 2]
                self.assertEqual(len(repeated), 2)
                self.assertNotEqual(repeated[0].start, repeated[1].start)

    def test_naive_or_empty_interval_rejected(self):
        result = snapshot()
        for start, end in [(START.replace(tzinfo=None), START), (START, START),
                           (START + timedelta(hours=1), START)]:
            with self.assertRaises(ValueError):
                result.covers(start, end)


class CacheTest(unittest.TestCase):
    def setUp(self):
        self.cache = PriceCache(SCOPE)
        self.cache.accept(snapshot(), now=START)

    def test_network_failure_keeps_only_original_bounded_snapshot(self):
        self.cache.failed("timeout")
        view = self.cache.view(now=START + timedelta(minutes=30))
        self.assertEqual(view.quality, "cached")
        self.assertEqual(view.snapshot.fetched_at, START)
        self.assertEqual(self.cache.view(now=START + timedelta(hours=2, seconds=1)).quality, "unavailable")

    def test_network_failure_does_not_create_current_hour(self):
        self.cache.failed("server_error")
        attrs = price_projection(self.cache.view(now=START + timedelta(hours=1)), now=START + timedelta(hours=1))
        self.assertIsNone(attrs["net_pln_kwh"])
        self.assertEqual(attrs["reason"], "current_hour_missing")

    def test_denial_and_malformed_response_block_cache_and_survive_restart(self):
        for reason in ["source_unavailable", "invalid_response"]:
            with self.subTest(reason=reason):
                self.cache.failed(reason)
                self.assertEqual(self.cache.view(now=START).reason, reason)
                write = self.cache.storage_candidate()
                restored = PriceCache.restore(write.payload_json, source_scope=SCOPE)
                self.assertEqual(restored.view(now=START).quality, "unavailable")

    def test_successful_empty_response_replaces_old_prices(self):
        self.cache.accept(snapshot([], fetched=START + timedelta(minutes=10)), now=START + timedelta(minutes=10))
        self.assertEqual(self.cache.view(now=START + timedelta(minutes=10)).snapshot.hours, ())
        self.assertIsNone(price_projection(self.cache.view(now=START + timedelta(minutes=10)),
                                          now=START + timedelta(minutes=10))["net_pln_kwh"])

    def test_future_timestamp_clock_rollback_and_scope_are_rejected(self):
        self.assertEqual(self.cache.view(now=START - timedelta(seconds=1)).quality, "unavailable")
        for value in [snapshot(fetched=START + timedelta(seconds=1)), snapshot(scope="other")]:
            with self.assertRaises(ValueError):
                self.cache.accept(value, now=START)

    def test_late_response_cannot_overwrite_newer_snapshot(self):
        now = START + timedelta(minutes=5)
        self.cache.accept(snapshot(fetched=now), now=now)
        with self.assertRaisesRegex(ValueError, "older"):
            self.cache.accept(snapshot([frame(net="99")]), now=now)

    def test_restart_preserves_age_and_cannot_grant_freshness(self):
        write = self.cache.storage_candidate()
        self.cache.ack_storage(write)
        restored = PriceCache.restore(write.payload_json, source_scope=SCOPE)
        self.assertEqual(restored.view(now=START + timedelta(minutes=45)).quality, "cached")
        self.assertEqual(restored.view(now=START + timedelta(hours=3)).quality, "unavailable")

    def test_restore_does_not_override_stricter_current_cache_policy(self):
        raw = self.cache.storage_candidate().payload_json
        restored = PriceCache.restore(raw, source_scope=SCOPE, max_age=timedelta(minutes=15))
        self.assertEqual(restored.view(now=START + timedelta(minutes=16)).quality, "unavailable")

    def test_cache_rejects_scope_switch_corruption_unknown_schema(self):
        payload = self.cache.storage_candidate().payload_json
        with self.assertRaises(ValueError):
            PriceCache.restore(payload, source_scope="other")
        data = json.loads(payload)
        data["schema"] = 999
        for raw in ["{", json.dumps(data), "x" * 32769]:
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                PriceCache.restore(raw, source_scope=SCOPE)

    def test_no_write_ack_until_io_success(self):
        first = self.cache.storage_candidate()
        self.assertEqual(self.cache.storage_candidate(), first)
        self.cache.ack_storage(first)
        self.assertIsNone(self.cache.storage_candidate())

    def test_same_content_checkpoint_only_hourly(self):
        self.cache.ack_storage(self.cache.storage_candidate())
        for minute in range(1, 60):
            now = START + timedelta(minutes=minute)
            self.cache.accept(snapshot(fetched=now), now=now)
            self.assertIsNone(self.cache.storage_candidate())
        now = START + timedelta(hours=1)
        self.cache.accept(snapshot(fetched=now), now=now)
        self.assertIsNotNone(self.cache.storage_candidate())

    def test_correction_forces_save_and_old_ack_does_not_hide_it(self):
        old_write = self.cache.storage_candidate()
        now = START + timedelta(minutes=1)
        self.cache.accept(snapshot([frame(net="0.99")], fetched=now), now=now)
        self.cache.ack_storage(old_write)
        self.assertIsNotNone(self.cache.storage_candidate())

    def test_projection_excludes_arrays_ages_and_refresh_times(self):
        old = price_projection(self.cache.view(now=START), now=START)
        for second in [1, 20, 40, 59]:
            now = START + timedelta(seconds=second)
            self.cache.accept(snapshot(fetched=now), now=now)
            self.assertEqual(old, price_projection(self.cache.view(now=now), now=now))
        self.assertLess(len(json.dumps(old).encode()), 1024)
        self.assertFalse(any(isinstance(value, (list, dict)) for value in old.values()))

    def test_cache_is_bounded_during_72_hours(self):
        # Cost proxy for source callbacks, not a measurement of actual HA DB/WAL.
        writes = 0
        projections = set()
        largest_cache = 0
        for hour in range(72):
            at = START + timedelta(hours=hour)
            rows = [frame(at + timedelta(hours=n)) for n in range(49)]
            for minute in range(60):
                now = at + timedelta(minutes=minute)
                self.cache.accept(snapshot(rows, fetched=now, start=at, hours=49), now=now)
                write = self.cache.storage_candidate()
                if write:
                    writes += 1
                    largest_cache = max(largest_cache, len(write.payload_json.encode()))
                    self.cache.ack_storage(write)
                attrs = price_projection(self.cache.view(now=now), now=now)
                projections.add(json.dumps(attrs, sort_keys=True))
        self.assertEqual(writes, 72)
        self.assertEqual(len(projections), 72)
        self.assertLess(largest_cache, 16384)


class ProfileTest(unittest.TestCase):
    def test_both_entry_points_select_same_pair(self):
        profile = DynamicPriceProfile(classic_operator="PGE", classic_tariff="G12w")
        a, b = profile.select_sale("Pstryk"), profile.select_purchase("Pstryk")
        self.assertEqual(a, b)
        self.assertEqual((a.sale_provider, a.purchase_provider), ("Pstryk", "Pstryk"))

    def test_switch_back_restores_classic_and_does_not_touch_other_settings(self):
        profile = DynamicPriceProfile(classic_operator="TAURON", classic_tariff="G13")
        result = profile.select_sale("Pstryk").select_sale("RCE")
        self.assertEqual((result.sale_provider, result.purchase_provider, result.classic_tariff),
                         ("RCE", "TAURON", "G13"))
        data = json.loads(result.to_json())
        self.assertNotIn("charge_enabled", data)
        self.assertNotIn("export_enabled", data)
        self.assertNotIn("api_token", data)

    def test_classic_changes_blocked_while_pstryk_bound(self):
        profile = DynamicPriceProfile(classic_operator="PGE", classic_tariff="G11").select_sale("Pstryk")
        with self.assertRaises(ValueError):
            profile.select_purchase("ENEA", tariff="G12")

    def test_missing_classic_requires_selection(self):
        profile = DynamicPriceProfile().select_sale("Pstryk")
        with self.assertRaisesRegex(ValueError, "classic_profile_required"):
            profile.select_sale("RCE")
        recovered = profile.select_sale("RCE", classic=("PGE", "G12"))
        self.assertEqual((recovered.sale_provider, recovered.purchase_provider), ("RCE", "PGE"))
        self.assertGreater(recovered.revision, profile.revision)

    def test_source_record_rejects_injected_actuator_permissions(self):
        profile = DynamicPriceProfile(classic_operator="Manual", classic_tariff="G12")
        for injected in [{"charge_enabled": True}, {"export_enabled": True},
                         {"api_token": "fake-secret"}]:
            data = {**json.loads(profile.to_json()), **injected}
            with self.assertRaises(ValueError):
                DynamicPriceProfile.from_json(json.dumps(data))

    def test_aba_invalidation_and_idempotence(self):
        start = DynamicPriceProfile(classic_operator="PGE", classic_tariff="G11")
        first = start.select_sale("Pstryk")
        again = first.select_sale("RCE").select_sale("Pstryk")
        self.assertGreater(again.revision, first.revision)
        self.assertEqual(first.select_purchase("Pstryk"), first)

    def test_classic_update_requires_supported_complete_profile(self):
        profile = DynamicPriceProfile()
        updated = profile.select_purchase("PGE", tariff="G12")
        self.assertEqual(updated.purchase_provider, "PGE")
        for provider, tariff in [("PSE", "G12"), ("PGE", "Pstryk"), ("PGE", None)]:
            with self.subTest(provider=provider, tariff=tariff), self.assertRaises(ValueError):
                profile.select_purchase(provider, tariff=tariff)

    def test_store_roundtrip_and_corrupt_profile(self):
        profile = DynamicPriceProfile(classic_operator="PGE", classic_tariff="G12").select_sale("Pstryk")
        self.assertEqual(DynamicPriceProfile.from_json(profile.to_json()), profile)
        for changes in [{"schema": 2}, {"mode": "mixed"}, {"revision": -1},
                        {"revision": True}, {"classic_operator": "unknown"}, {"sale_provider": "RCE"}]:
            raw = json.loads(profile.to_json())
            raw.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                DynamicPriceProfile.from_json(json.dumps(raw))


class FakeContent:
    def __init__(self, data):
        self.data = data
    async def read(self, limit):
        data, self.data = self.data[:limit], self.data[limit:]
        return data


ACTION = "a" * 40
PUBLIC_PAGE = b'<script src="/_next/static/chunks/prices.js"></script>'
PUBLIC_SCRIPT = f'createServerReference)("{ACTION}",x,y,z,"getDailyPrices")'.encode()


def public_response(payload):
    return ('0:{"a":"$@1"}\n1:' + json.dumps(payload) + '\n').encode()


class PublicationMetadataTests(unittest.TestCase):
    def test_unpublished_placeholder_is_missing_but_published_zero_is_a_price(self):
        # Actual public response observed 2026-10-01 before next-day publication.
        placeholder={'priceNetAvg':None,'priceGrossAvg':0,'frames':[dict(frame(net=0),isCheap=None,isExpensive=None)]}
        self.assertEqual(decode_public_response(public_response(placeholder)),{'frames':[]})
        published={**placeholder,'priceNetAvg':0,'frames':[dict(frame(net=0),isCheap=False,isExpensive=False)]}
        self.assertEqual(decode_public_response(public_response(published)),published)


class FakeResponse:
    def __init__(self, status=200, data=None, raw=None):
        self.status = status
        self.content = FakeContent(raw if raw is not None else public_response(
            {"frames": [frame()]} if data is None else data))
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False


class FakeSession:
    def __init__(self, response=None):
        self.response = response
        self.calls = []
    @property
    def price_calls(self):
        return [call for call in self.calls if call[0] == "POST"]
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if method == "GET":
            return FakeResponse(raw=PUBLIC_PAGE if url == ENDPOINT else PUBLIC_SCRIPT)
        if self.response is not None and len(self.price_calls) == 1:
            return self.response
        day = datetime.fromisoformat(json.loads(kwargs["data"])[0]).replace(tzinfo=ZONE)
        return FakeResponse(data={"frames": [frame(day.astimezone(UTC))]})


class PublicContractTest(unittest.TestCase):
    def test_net_absence_cannot_use_gross_account_fields_or_summary(self):
        row = frame()
        del row["priceNet"]
        row["metrics"] = {"pricing": {"price_gross": "0.8", "price_prosumer_gross": "0.7"}}
        value = parse_prices({"frames": [row], "priceNetAvg": 1}, source_scope=SCOPE,
                             fetched_at=START, window_start=START, window_end=START + timedelta(hours=1))
        self.assertIsNone(value.hours[0].net)
        self.assertFalse(value.covers(START, START + timedelta(hours=1)))

    def test_zero_and_negative_are_equal_in_both_directions(self):
        for net in ["0", "-0.01", "0.56"]:
            row = snapshot([frame(net=net, gross="1.99")]).hours[0]
            self.assertEqual((row.buy, row.sell), (Decimal(net), Decimal(net)))

    def test_price_projection_and_cache_store_one_net_value(self):
        cache = PriceCache(SCOPE)
        cache.accept(snapshot(), now=START)
        data = json.loads(cache.storage_candidate().payload_json)
        self.assertEqual(data["schema"], 2)
        self.assertEqual(data["basis"], "public_net_same_buy_sell")
        self.assertEqual(set(data["snapshot"]["hours"][0]), {"start", "end", "priceNet"})
        attrs = price_projection(cache.view(now=START), now=START)
        self.assertEqual(attrs["net_pln_kwh"], 0.4)
        self.assertNotIn("buy_pln_kwh", attrs)

    def test_old_gross_cache_and_wrong_basis_are_rejected(self):
        cache = PriceCache(SCOPE)
        cache.accept(snapshot(), now=START)
        data = json.loads(cache.storage_candidate().payload_json)
        for change in [{"schema": 1}, {"basis": "official_gross_as_received"},
                       {"contract": "pstryk_unified_hour_v1"}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                PriceCache.restore(json.dumps({**data, **change}), source_scope=SCOPE)

    def test_rsc_only_referenced_payload_is_accepted(self):
        payload = {"frames": [frame(net="-0.01")]}
        raw = b'0:{"a":"$@a"}\n1:{"frames":[]}\na:' + json.dumps(payload).encode()
        self.assertEqual(decode_public_response(raw), payload)
        for raw in [b"html", b'1:{"frames":[]}', b'0:{"a":"$@2"}\n1:{"frames":[]}',
                    b'0:{"a":"$@1"}\n1:E{"digest":"hidden"}', b'0:{"a":"$@1"}\n1:[]',
                    b'0:{"a":"$@1"}\n1:{"frames":[]}\n1:{"frames":[]}']:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                decode_public_response(raw)
        self.assertEqual(decode_public_response(public_response(None)), {"frames": []})


class ClientTest(unittest.IsolatedAsyncioTestCase):
    async def test_archive_fetch_does_not_backdate_availability(self):
        old = request_window(START - timedelta(days=7))[0]
        session = FakeSession()
        client = PstrykClient(session, source_scope=SCOPE)
        result = await client.fetch_window(start=old, end=old + timedelta(days=1), received_at=START)
        self.assertEqual(result.hours[0].start, old)
        self.assertEqual(result.fetched_at, START)
        self.assertIsNone(result.at(START))
        self.assertEqual(json.loads(session.price_calls[0][2]["data"]), ["2026-09-22"])

    async def test_archive_requests_are_bounded_before_network_io(self):
        session = FakeSession()
        client = PstrykClient(session, source_scope=SCOPE)
        for start, end in [(START, START + timedelta(days=14)), (START, START + timedelta(days=1))]:
            with self.assertRaises(ValueError):
                await client.fetch_window(start=start, end=end, received_at=START)
        self.assertEqual(session.calls, [])

    async def test_json_number_precision_preserved_before_planner_conversion(self):
        response = FakeResponse(raw=public_response({"frames": [frame()]}).replace(
            b'"0.40"', b'0.1234567890123456789012345'))
        result = await PstrykClient(FakeSession(response), source_scope=SCOPE).fetch(now=START)
        self.assertEqual(result.hours[0].buy, Decimal("0.1234567890123456789012345"))
        self.assertEqual(result.hours[0].buy, result.hours[0].sell)

    async def test_public_requests_no_key_and_no_redirects(self):
        session = FakeSession()
        result = await PstrykClient(session, source_scope=SCOPE).fetch(now=START)
        self.assertEqual(result.hours[0].buy, Decimal("0.4"))
        self.assertEqual(len(session.price_calls), 2)
        self.assertEqual([json.loads(call[2]["data"])[0] for call in session.price_calls],
                         ["2026-09-29", "2026-09-30"])
        for method, url, request in session.calls:
            self.assertTrue(url.startswith(ORIGIN + "/"))
            self.assertFalse(request["allow_redirects"])
            self.assertFalse({"authorization", "cookie"} & {key.lower() for key in request.get("headers", {})})
            if method == "POST":
                self.assertEqual(url, ENDPOINT)
                self.assertEqual(request["headers"]["Next-Action"], ACTION)

    async def test_authenticated_session_is_rejected(self):
        for attrs in [{"headers": {"Authorization": "secret"}}, {"auth": "secret"},
                      {"cookie_jar": ["secret"]}, {"cookie_jar": []}, {"_default_auth": "secret"}]:
            session = FakeSession()
            session.__dict__.update(attrs)
            with self.assertRaises(ValueError):
                PstrykClient(session, source_scope=SCOPE)

    async def test_http_errors_are_coded_without_response_body(self):
        for status, reason in [(401, "source_unavailable"), (403, "source_unavailable"),
                               (429, "rate_limited"), (503, "server_error"),
                               (302, "invalid_response"), (404, "invalid_response")]:
            session = FakeSession(FakeResponse(status, {"sensitive": "hidden-text"}))
            with self.subTest(status=status), self.assertRaises(PstrykAPIError) as caught:
                await PstrykClient(session, source_scope=SCOPE).fetch(now=START)
            self.assertEqual(caught.exception.reason, reason)
            self.assertNotIn("hidden-text", str(caught.exception))

    async def test_response_size_and_invalid_json(self):
        for raw in [b"x" * (128 * 1024 + 1), b"not json", b'0:{"a":"$@1"}\n1:{"frames":[NaN]}']:
            with self.assertRaises(PstrykAPIError) as caught:
                await PstrykClient(FakeSession(FakeResponse(raw=raw)), source_scope=SCOPE).fetch(now=START)
            self.assertEqual(caught.exception.reason, "invalid_response")

    async def test_same_day_from_server_cannot_fill_tomorrow(self):
        class WrongDaySession(FakeSession):
            def request(self, method, url, **kwargs):
                if method == "POST":
                    self.calls.append((method, url, kwargs))
                    return FakeResponse(data={"frames": [frame()]})
                return super().request(method, url, **kwargs)
        with self.assertRaises(PstrykAPIError) as caught:
            await PstrykClient(WrongDaySession(), source_scope=SCOPE).fetch(now=START)
        self.assertEqual(caught.exception.reason, "invalid_response")

    async def test_unpublished_tomorrow_is_missing_without_extending_today(self):
        class FutureSession(FakeSession):
            def request(self, method, url, **kwargs):
                if method == "POST" and json.loads(kwargs["data"]) == ["2026-09-30"]:
                    self.calls.append((method, url, kwargs))
                    return FakeResponse(raw=public_response(None))
                return super().request(method, url, **kwargs)
        result = await PstrykClient(FutureSession(), source_scope=SCOPE).fetch(now=START)
        self.assertEqual(len(result.hours), 1)
        self.assertFalse(result.covers(*request_window(START)))

    async def test_dst_public_requests_use_two_calendar_days(self):
        for stamp in [datetime(2026, 3, 29, tzinfo=ZONE), datetime(2026, 10, 25, tzinfo=ZONE)]:
            session = FakeSession()
            result = await PstrykClient(session, source_scope=SCOPE).fetch(now=stamp)
            self.assertEqual((result.window_start, result.window_end), request_window(stamp))
            self.assertEqual(len(session.price_calls), 2)

    async def test_simultaneous_buy_sell_refresh_is_single_flight(self):
        session = FakeSession()
        client = PstrykClient(session, source_scope=SCOPE)
        first, second = await asyncio.gather(client.fetch(now=START), client.fetch(now=START))
        self.assertEqual(len(session.price_calls), 2)
        self.assertIs(first, second)

    async def test_discovery_is_cached_without_changing_prices(self):
        session = FakeSession()
        client = PstrykClient(session, source_scope=SCOPE)
        a = await client.fetch(now=START)
        b = await client.fetch(now=START + timedelta(minutes=5))
        self.assertEqual(len([call for call in session.calls if call[0] == "GET"]), 2)
        self.assertEqual(len(session.price_calls), 4)
        self.assertEqual(a.revision, b.revision)

    async def test_changed_public_action_is_rediscovered_at_next_refresh(self):
        class RotatedSession(FakeSession):
            action = ACTION
            def request(self, method, url, **kwargs):
                if method == "GET" and url != ENDPOINT:
                    self.calls.append((method, url, kwargs))
                    return FakeResponse(raw=PUBLIC_SCRIPT.replace(ACTION.encode(), self.action.encode()))
                if method == "POST" and kwargs["headers"]["Next-Action"] != self.action:
                    self.calls.append((method, url, kwargs))
                    return FakeResponse(status=404)
                return super().request(method, url, **kwargs)
        session = RotatedSession()
        client = PstrykClient(session, source_scope=SCOPE)
        before = await client.fetch(now=START)
        session.action = "b" * 40
        with self.assertRaises(PstrykAPIError):
            await client.fetch(now=START + timedelta(minutes=1))
        self.assertIsNone(client._action_id)
        after = await client.fetch(now=START + timedelta(minutes=2))
        self.assertEqual(client._action_id, session.action)
        self.assertEqual(before.revision, after.revision)
        self.assertEqual(len([call for call in session.calls if call[0] == "GET"]), 4)

    async def test_discovery_never_follows_untrusted_paths(self):
        class ForeignSession(FakeSession):
            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                return FakeResponse(raw=b'<script src="https://evil.invalid/prices.js"></script>'
                                    b'<script src="/_next/static/chunks/../../secret.js"></script>')
        session = ForeignSession()
        with self.assertRaises(PstrykAPIError):
            await PstrykClient(session, source_scope=SCOPE).fetch(now=START)
        self.assertEqual(len(session.calls), 1)

    async def test_discovery_has_asset_byte_and_request_limits(self):
        class PageSession(FakeSession):
            def __init__(self, page, script=PUBLIC_SCRIPT):
                super().__init__()
                self.page, self.script = page, script
            def request(self, method, url, **kwargs):
                self.calls.append((method, url, kwargs))
                return FakeResponse(raw=self.page if url == ENDPOINT else self.script)
        too_many = b''.join(f'<script src="/_next/static/chunks/a{i}.js"></script>'.encode() for i in range(33))
        for session in [PageSession(b'x' * (512 * 1024 + 1)), PageSession(too_many),
                        PageSession(PUBLIC_PAGE, b'x' * (2 * 1024 * 1024 + 1)),
                        PageSession(PUBLIC_PAGE, b'no public read action')]:
            with self.assertRaises(PstrykAPIError):
                await PstrykClient(session, source_scope=SCOPE).fetch(now=START)
            self.assertLessEqual(len(session.calls), 2)

    async def test_cancelled_consumer_does_not_cancel_shared_fetch(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class SlowResponse(FakeResponse):
            async def __aenter__(self):
                entered.set()
                await release.wait()
                return self
        session = FakeSession(SlowResponse())
        client = PstrykClient(session, source_scope=SCOPE)
        first = asyncio.create_task(client.fetch(now=START))
        await entered.wait()
        second = asyncio.create_task(client.fetch(now=START))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        release.set()
        self.assertEqual((await second).hours[0].sell, Decimal("0.4"))
        self.assertEqual(len(session.price_calls), 2)
        await client.close()

    async def test_close_drains_worker_and_rejects_later_calls(self):
        client = PstrykClient(FakeSession(), source_scope=SCOPE)
        await client.close()
        with self.assertRaises(PstrykAPIError) as caught:
            await client.fetch(now=START)
        self.assertEqual(caught.exception.reason, "client_closed")

    async def test_timeout_is_bounded_and_leaves_no_live_worker(self):
        class HungResponse(FakeResponse):
            async def __aenter__(self):
                await asyncio.Event().wait()
        client = PstrykClient(FakeSession(HungResponse()), source_scope=SCOPE)
        with patch("pstryk_client.FETCH_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(PstrykAPIError) as caught:
                await client.fetch(now=START)
        self.assertEqual(caught.exception.reason, "timeout")
        self.assertTrue(client._task.done())
        await client.close()

    async def test_close_cancels_and_awaits_inflight_transport(self):
        entered, drained = asyncio.Event(), asyncio.Event()
        class HungResponse(FakeResponse):
            async def __aenter__(self):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    drained.set()
        client = PstrykClient(FakeSession(HungResponse()), source_scope=SCOPE)
        caller = asyncio.create_task(client.fetch(now=START))
        await entered.wait()
        await client.close()
        self.assertTrue(drained.is_set())
        with self.assertRaises(asyncio.CancelledError):
            await caller

    async def test_midnight_request_cannot_receive_previous_window(self):
        entered, release = asyncio.Event(), asyncio.Event()
        before = datetime(2026, 9, 29, 21, 59, tzinfo=UTC)
        after = before + timedelta(minutes=2)
        class SlowResponse(FakeResponse):
            async def __aenter__(self):
                entered.set()
                await release.wait()
                return self
        session = FakeSession(SlowResponse(data={"frames": []}))
        client = PstrykClient(session, source_scope=SCOPE)
        first = asyncio.create_task(client.fetch(now=before))
        await entered.wait()
        second = asyncio.create_task(client.fetch(now=after))
        await asyncio.sleep(0)
        self.assertEqual(len(session.price_calls), 1)
        release.set()
        old, new = await asyncio.gather(first, second)
        self.assertEqual(len(session.price_calls), 4)
        self.assertNotEqual(old.window_start, new.window_start)
        await client.close()

    async def test_transport_error_is_redacted(self):
        class BrokenSession:
            def request(self, *args, **kwargs):
                raise OSError("hidden-text from transport")
        with self.assertRaises(PstrykAPIError) as caught:
            await PstrykClient(BrokenSession(), source_scope=SCOPE).fetch(now=START)
        self.assertEqual(caught.exception.reason, "network_error")
        self.assertNotIn("hidden-text", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
