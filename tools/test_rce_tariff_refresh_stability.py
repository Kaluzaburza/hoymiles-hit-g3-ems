"""Real tariff snapshots: metadata refresh must not revoke an RCE plan."""
from dataclasses import replace
from datetime import timedelta
from test_rce_plan_revalidation import seed, RCE
from tariff_price_schedule import TariffPriceConfig, build_tariff_price_schedule


def main():
    settings, _ = seed()
    cfg = TariffPriceConfig('G11', .2, .2, .2, .2, ())
    schedule = build_tariff_price_schedule(cfg, start=settings.now-timedelta(minutes=2),
        end=settings.now+timedelta(days=3), local_zone=settings.now.tzinfo,
        generated_at=settings.now)
    settings = replace(settings, tariff_price_schedule=schedule)
    result = RCE.optimize_rce(settings)
    assert result.ready and result.planned_exports
    def check(name, changed, accept):
        diagnostics = {}
        got = RCE.revalidate_rce_plan(replace(settings, tariff_price_schedule=changed),
            result, captured_settings=settings, diagnostics=diagnostics)
        assert (got is not None) == accept, (name, diagnostics)
        print('PASS', name)
    check('identical', schedule, True)
    check('timestamp only', replace(schedule, generated_at_utc=schedule.generated_at_utc+timedelta(seconds=1)), True)
    check('cache metadata only', replace(schedule, served_from_cache=True), True)
    rolled = build_tariff_price_schedule(cfg,start=settings.now,end=settings.now+timedelta(days=3,minutes=2),
        local_zone=settings.now.tzinfo,generated_at=settings.now+timedelta(seconds=1))
    check('rolling unchanged price window', rolled, True)
    check('real price change', replace(schedule,intervals=tuple(replace(x,price_pln_kwh_ac=.3) for x in schedule.intervals)), False)
    check('revision changed',replace(schedule,source_revision='new-settings'),False)
    check('quality lost',replace(schedule,quality='unavailable'),False)
    check('coverage lost',replace(schedule,coverage_end_utc=schedule.coverage_end_utc-timedelta(hours=1)),False)
    check('future start',replace(rolled,coverage_start_utc=settings.now+timedelta(minutes=1)),False)
    check('missing coverage reasons',replace(schedule,missing_reasons=('gap',)),False)
    print('PASS tariff metadata/meaning distinction')


if __name__=='__main__': main()
