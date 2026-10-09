"""Refresh real tariff snapshots while the production RCE worker is pending.

Uses production publication, YAML helpers, Supervisor and a firmware lease
model. The deterministic clock/transport are simulated, not field acceptance.
"""
import asyncio
from dataclasses import replace
from datetime import timedelta
import importlib
import json
from pathlib import Path
import sys

from test_rce_publication_continuity import Continuity, NOW, h


class TariffContinuity(Continuity):
    async def setup(self):
        await super().setup()
        self.source.attach_supervisor_commitment_source(self.supervisor)
        tariff = importlib.import_module(
            "custom_components.hoymiles_hit_modbus.tariff_price_schedule")
        config = tariff.TariffPriceConfig('G11', .2, .2, .2, .2, ())
        provider = self.source._optimizer_input
        self.tariff_reads = []
        self.protection_calls = []
        original_protect = self.module.retain_active_rce_slot

        def protect(*args, **kwargs):
            self.protection_calls.append(self.second)
            return original_protect(*args, **kwargs)

        self.module.retain_active_rce_slot = protect

        def refreshed_provider():
            settings, metadata = provider()
            # Minute publication overlaps each four-second solver job.
            report = NOW + timedelta(seconds=int(self.second // 2) * 2)
            schedule = tariff.build_tariff_price_schedule(
                config, start=NOW-timedelta(minutes=2),
                end=NOW+timedelta(days=3), local_zone=NOW.tzinfo,
                generated_at=report)
            self.tariff_reads.append(report.isoformat())
            return replace(settings, tariff_price_schedule=schedule), metadata

        self.source._optimizer_input = refreshed_provider

    async def tick(self, second):
        await super().tick(second)
        if second == 12:
            assert self.source._active_rce_commitment(h.CLOCK['now']) is not None


async def main():
    probe = TariffContinuity()
    try:
        result = await probe.run('master_stop')
        assert len(set(probe.tariff_reads)) >= 4
        assert len(probe.protection_calls) >= 3
        result['protection_calls'] = probe.protection_calls
        result['tariff_publications'] = probe.tariff_reads
        print('PASS 270 virtual seconds, tariff refresh during each solver job, '
              'same transaction, no restore, fresh lease renewals; manual STOP respected')
    except AssertionError as error:
        result = {'status': 'FAIL', 'error': str(error),
                  'protection_calls': probe.protection_calls,
                  'attributes': probe.source._attributes,
                  'frames': probe.frames, 'publications': probe.publications,
                  'renewals': probe.services.renewals}
        raise
    finally:
        if len(sys.argv) > 1:
            Path(sys.argv[1]).write_text(json.dumps(result, indent=2, default=str), encoding='utf-8')


if __name__ == '__main__':
    asyncio.run(main())
