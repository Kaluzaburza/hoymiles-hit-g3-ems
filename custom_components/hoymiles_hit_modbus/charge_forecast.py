"""Small observed SOC/charge-capability curve; forecast only, never a command."""
from math import isfinite

BANDS = (90., 98., 100.)


def valid_curve(curve):
    return (isinstance(curve, (list, tuple)) and len(curve) == 3
        and all(type(v) in (int, float) and isfinite(v) and 0 < v <= 5000 for v in curve)
        and curve[0] >= curve[1] >= curve[2])


def learn_curve(series):
    """Paired five-minute means, lower quintile, bounded six-day evidence.

    Ignore full-battery samples: zero intake with no headroom is not a rate.
    Require observations in every band and on two days below 90% SOC.
    """
    if len(series) > 1730:
        return None
    bins = [[], [], []]
    days = set()
    for stamp, soc, amps, volts in series:
        if not all(type(v) in (int, float) and isfinite(v) for v in (stamp, soc, amps, volts)):
            continue
        if not (0 <= soc < 99.9 and 0 <= amps <= 20000 and 10 <= volts <= 2000):
            continue
        i = next(i for i, ceiling in enumerate(BANDS) if soc < ceiling)
        bins[i].append(amps*volts/1000*.95)
        if i == 0:
            days.add(int(stamp//86400))
    if len(days) < 2 or any(len(b) < 6 for b in bins):
        return None
    values = [round(sorted(b)[int((len(b)-1)*.2)], 2) for b in bins]
    # Do not invent a higher rate at high SOC from noisy/noncoincident samples.
    curve = tuple(min(values[:i+1]) for i in range(3))
    return curve if valid_curve(curve) else None


def charge_budget(energy, capacity, ceiling, hours, available_kw, curve):
    """Integrate DC intake across SOC boundaries, including the slow final band."""
    if not valid_curve(curve) or hours <= 0 or capacity <= 0 or available_kw <= 0:
        return 0.
    before = energy
    for soc, rate in zip(BANDS, curve):
        end = min(capacity*soc/100, ceiling)
        room = max(end-energy, 0.)
        if room <= 1e-10:
            continue
        power = min(available_kw, rate)
        if power <= 0:
            break
        added = min(room, power*hours)
        energy += added
        hours -= added/power
        if hours <= 1e-10:
            break
    return max(energy-before, 0.)


def forecast_curve(curve, *, soc, live_kw):
    """A low-SOC derating remains a limitation, not presumed full-SOC taper."""
    if not valid_curve(curve):
        return None
    if soc < 90:
        if live_kw <= 0:
            return None
        return tuple(min(v, max(live_kw, 0.)) for v in curve)
    return tuple(curve)
