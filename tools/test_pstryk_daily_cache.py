"""RCE-style daily persistence, dated authority and bounded Pstryk I/O."""
from dataclasses import replace
from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'custom_components/hoymiles_hit_modbus'))
from pstryk_daily_cache import DailyPriceCache, day_bounds, daily_snapshot_valid
from pstryk_prices import PriceCache, WARSAW, HOUR, parse_prices, utc

NOW = datetime(2026,10,2,10,tzinfo=WARSAW)
SCOPE = 'entry:pstryk:1'


def publication(day=NOW.date(), fetched=NOW, net='0.42', scope=SCOPE):
    start,end=day_bounds(day)
    return parse_prices({'frames':[{'start':(start+HOUR*i).isoformat(),
        'end':(start+HOUR*(i+1)).isoformat(),'priceNet':net}
        for i in range(int((end-start)/HOUR))]}, source_scope=scope,
        fetched_at=fetched,window_start=start,window_end=end)


class DailyCacheTests(unittest.TestCase):
    def test_day_survives_failure_age_and_restart_without_rejuvenation(self):
        c=DailyPriceCache(SCOPE);c.accept(publication(),now=NOW)
        for reason in ('timeout','invalid_response','server_error','source_unavailable'):
            c.failed(reason)
            later=NOW+timedelta(hours=10)
            self.assertEqual(c.view(now=later).snapshot.fetched_at,utc(NOW))
            self.assertTrue(daily_snapshot_valid(c.view(now=later).snapshot,NOW.date(),later))
        raw=c.storage_candidate().payload_json
        restored=DailyPriceCache.restore(raw,source_scope=SCOPE,now=later)
        self.assertEqual(restored.view(now=later).snapshot.fetched_at,utc(NOW))
        self.assertNotIn(NOW.date(),restored.due_days(later))
        self.assertIsNone(restored.view(now=NOW+timedelta(days=1)).snapshot)

    def test_dst_rollover_keeps_tomorrow_with_real_receipt(self):
        for day,count in ((datetime(2026,3,29,12,tzinfo=WARSAW),23),
                          (datetime(2026,10,25,12,tzinfo=WARSAW),25)):
            previous=day-timedelta(days=1)
            c=DailyPriceCache(SCOPE)
            c.accept(publication(day.date(),previous,net='-0.15'),now=previous)
            raw=c.storage_candidate().payload_json
            c=DailyPriceCache.restore(raw,source_scope=SCOPE,now=day)
            self.assertEqual(len(c.get(day.date(),day).hours),count)
            self.assertEqual(c.view(now=day).snapshot.fetched_at,utc(previous))
            self.assertTrue(daily_snapshot_valid(c.view(now=day).snapshot,day.date(),day))
            self.assertIn(day.date(),c.due_days(day))
            c.before_fetch(day.date(),day)
            raw=c.storage_candidate().payload_json
            restored=DailyPriceCache.restore(raw,source_scope=SCOPE,now=day)
            self.assertNotIn(day.date(),restored.due_days(day))
            c.accept(publication(day.date(),day,net='-0.15'),now=day)
            self.assertEqual(c.get(day.date(),day).fetched_at,utc(previous))

    def test_partial_empty_foreign_future_and_old_cannot_replace_day(self):
        c=DailyPriceCache(SCOPE);v=publication();c.accept(v,now=NOW)
        before=c.storage_candidate().payload_json
        for bad in (replace(v,hours=v.hours[:-1]),replace(v,hours=()),
                    replace(v,source_scope='foreign'),replace(v,fetched_at=utc(NOW+HOUR)),
                    replace(v,fetched_at=utc(NOW-timedelta(days=3))),
                    replace(v,hours=(replace(v.hours[0],net=None),)+v.hours[1:])):
            with self.assertRaises(ValueError): c.accept(bad,now=NOW)
            self.assertEqual(c.storage_candidate().payload_json,before)

    def test_valid_correction_changes_prices_but_invalid_never_erases_them(self):
        c=DailyPriceCache(SCOPE);c.accept(publication(),now=NOW)
        before=c.view(now=NOW).snapshot.revision
        c.accept(publication(fetched=NOW+HOUR,net='0'),now=NOW+HOUR)
        self.assertNotEqual(c.view(now=NOW+HOUR).snapshot.revision,before)
        self.assertEqual(c.get(NOW.date(),NOW+HOUR).hours[0].net,0)
        with self.assertRaises(ValueError):c.accept(publication(),now=NOW+HOUR)

    def test_missing_day_retry_is_persisted_15_30_60_minutes(self):
        c=DailyPriceCache(SCOPE);day=NOW.date();stamp=NOW
        for minutes in (15,30,60,60):
            self.assertIn(day,c.due_days(stamp))
            c.before_fetch(day,stamp);c.failed('server_error')
            raw=c.storage_candidate().payload_json
            c=DailyPriceCache.restore(raw,source_scope=SCOPE,now=stamp)
            self.assertNotIn(day,c.due_days(stamp+timedelta(minutes=minutes,seconds=-1)))
            stamp+=timedelta(minutes=minutes)
        self.assertIsNone(c.view(now=stamp).snapshot)

    def test_legacy_migration_uses_checksum_complete_days_and_real_dates(self):
        old=PriceCache(SCOPE);old.accept(publication(),now=NOW);old.failed('invalid_response')
        raw=old.storage_candidate().payload_json
        c=DailyPriceCache.restore(raw,source_scope=SCOPE,now=NOW+timedelta(hours=3))
        self.assertIsNotNone(c.get(NOW.date(),NOW+timedelta(hours=3)))
        self.assertIsNotNone(c.storage_candidate(),'Migration must be saved, not merely acknowledged')
        bad=json.loads(raw);bad['snapshot']['hours'][0]['priceNet']='123'
        with self.assertRaises(ValueError):DailyPriceCache.restore(json.dumps(bad),source_scope=SCOPE,now=NOW)

    def test_corrupt_disk_wrong_scope_clock_and_expired_days(self):
        c=DailyPriceCache(SCOPE);c.accept(publication(),now=NOW)
        raw=c.storage_candidate().payload_json
        for field,value in (('schema',99),('scope','another'),('basis','gross')):
            bad=json.loads(raw);bad[field]=value
            with self.assertRaises(ValueError):DailyPriceCache.restore(json.dumps(bad),source_scope=SCOPE,now=NOW)
        bad=json.loads(raw);bad['days'][NOW.date().isoformat()]['hours'][0]['priceNet']='bad'
        with self.assertRaises(ValueError):DailyPriceCache.restore(json.dumps(bad),source_scope=SCOPE,now=NOW)
        self.assertIsNone(c.view(now=NOW-timedelta(seconds=1)).snapshot)
        expired=DailyPriceCache.restore(raw,source_scope=SCOPE,now=NOW+timedelta(days=3))
        self.assertFalse(expired.days)

    def test_two_phase_save_and_no_write_from_30_second_ticks_72_hours(self):
        c=DailyPriceCache(SCOPE);writes=0;peak=0
        for tick in range(72*120):
            stamp=NOW+timedelta(seconds=30*tick)
            c.prune(stamp)
            if stamp.minute==0 and stamp.second==0:
                for day in (stamp.date(),stamp.date()+timedelta(days=1)):
                    if c.get(day,stamp) is None:c.accept(publication(day,stamp),now=stamp)
            c.view(now=stamp)
            write=c.storage_candidate()
            if write:
                self.assertEqual(write,c.storage_candidate())
                peak=max(peak,len(write.payload_json.encode()));writes+=1;c.ack_storage(write)
            self.assertLessEqual(len(c.days),2)
        self.assertLessEqual(writes,7)
        self.assertLess(peak,16384)
        print('72h daily-cache writes:',writes,'maximum bytes:',peak)


if __name__=='__main__':unittest.main()
