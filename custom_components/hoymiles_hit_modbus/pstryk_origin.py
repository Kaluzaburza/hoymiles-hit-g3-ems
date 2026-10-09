"""Bounded conservative PV-origin ledger, independently durable before credit.

Unknown energy starts at zero. Import is subtracted from charging even if it
also served home; all battery output debits PV first. This can undercount PV,
but cannot invent sale authority from SOC growth or from grid charging.
"""
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import math
from zoneinfo import ZoneInfo

WARSAW = ZoneInfo('Europe/Warsaw')

@dataclass(frozen=True, slots=True)
class CounterSample:
    at: datetime
    pv_charge_kwh: float
    charge_kwh: float
    discharge_kwh: float
    grid_import_kwh: float
    physical_kwh: float
    fresh: bool = True

class OriginLedger:
    def __init__(self, scope: str):
        self.scope = scope
        self.anchor = None
        self.balance = 0.
        self.confirmed = 0.
        self.reconciled = False
        self.reason = 'unknown_origin'
        self._serial_at = None

    @property
    def available_kwh(self) -> float:
        # Four cumulative 0.1 kWh counters: one fixed conservative uncertainty
        # allowance, NOT one loss per tiny counter update.
        return max(min(self.balance,self.confirmed)-.4,0) if self.reconciled else 0.

    def observe(self, sample: CounterSample) -> None:
        numbers = (sample.pv_charge_kwh,sample.charge_kwh,sample.discharge_kwh,
                   sample.grid_import_kwh,sample.physical_kwh)
        valid = (sample.fresh and sample.at.tzinfo is not None and
                 all(type(v) in (float,int) and math.isfinite(v) and v>=0 for v in numbers))
        prior = self.anchor
        changed = prior is None or numbers[:4] != (prior.pv_charge_kwh,prior.charge_kwh,prior.discharge_kwh,prior.grid_import_kwh)
        delta = [(getattr(sample,k)-getattr(prior,k)) if prior else 0.
                 for k in ('pv_charge_kwh','charge_kwh','discharge_kwh','grid_import_kwh')]
        continuity = bool(valid and prior and prior.at.astimezone(WARSAW).date()==sample.at.astimezone(WARSAW).date()
                          and 0 <= (sample.at-prior.at).total_seconds() <= 300 and min(delta)>=0)
        old_balance = self.balance
        if not continuity:
            self.balance = self.confirmed = 0.
            self.reason = 'origin_anchor_reset'
        else:
            debit = delta[2]/.90  # conservative for either AC or DC output counter
            credit = min(delta[0],max(delta[1]-delta[3],0))*.90
            # Counter intervals do not reveal flow order. Debit AFTER adding
            # PV so a charge followed by discharge cannot leave phantom PV.
            self.balance = min(max(self.balance+credit-debit,0),sample.physical_kwh)
            # Debits are effective immediately, even before disk I/O.
            self.confirmed = min(max(self.confirmed-debit,0),self.balance)
            self.reason = 'measured_conservative_pv'
        self.reconciled = bool(valid)
        self.anchor = sample if valid else None
        if changed or self.balance != old_balance or not continuity:
            self._serial_at = sample.at if valid else None

    def storage_candidate(self) -> str:
        anchor = asdict(self.anchor) if self.anchor else None
        if anchor:
            anchor['at'] = self._serial_at.isoformat()
            # Physical SOC is only a live upper bound, not a history stream.
            anchor['physical_kwh'] = round(self.balance,6)
        return json.dumps({'schema':1,'scope':self.scope,'balance':round(self.balance,6),'anchor':anchor},
                          sort_keys=True,separators=(',',':'),allow_nan=False)

    def acknowledge(self, raw: str) -> None:
        if raw == self.storage_candidate():
            self.confirmed = self.balance

    @classmethod
    def restore(cls, scope: str, raw: str):
        try:
            if not isinstance(raw,str) or len(raw)>2048: raise ValueError()
            data = json.loads(raw)
            if data['schema']!=1 or data['scope']!=scope: raise ValueError()
            obj = cls(scope)
            if data['anchor'] is not None:
                row = data['anchor']; row['at'] = datetime.fromisoformat(row['at'])
                obj.anchor = CounterSample(**row); obj._serial_at = obj.anchor.at
            balance = data['balance']
            if type(balance) not in (float,int) or not math.isfinite(balance) or balance<0: raise ValueError()
            obj.balance = obj.confirmed = balance
            return obj  # no execution authority until a fresh sample reconciles
        except (ValueError,TypeError,KeyError,OverflowError,RecursionError):
            raise ValueError('origin_store_invalid') from None
