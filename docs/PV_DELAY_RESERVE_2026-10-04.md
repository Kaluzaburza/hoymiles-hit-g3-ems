# PV delay below the battery-sale reserve

PV delay exports current PV surplus while PV supplies the home. It postpones
charging; it does not sell the battery stock. Requiring the battery-sale
reserve before starting therefore incorrectly blocked a day with sufficient
PV for the home and full later charging.

The shared RCE/Pstryk delay solver now preserves the actual stock at the
start instead of requiring the sale reserve. Every interval from hold to
recovery must remain free of battery discharge and grid import. The baseline
energy and PV-origin stock must be recovered the same day, including under
the lower forecast, before another fixed action, by 16:30 local and at least
one hour before forecast surplus ends. Existing configured SOC ceilings and
charging power limits remain binding.

Pstryk previously skipped all delay planning whenever a reserve top-up was
needed. It now evaluates a PV-only alternative to an uncommitted top-up:
PV must cover the home in the displaced BUY intervals, no interval may gain
additional import, and the final stock cannot fall below the original plan.
These conditions also apply to the lower forecast. The alternative is chosen
only with a valid profitable delay window. Otherwise the original BUY/SELL
plan remains. An active BUY commitment is preserved.

This does not lower the reserve used for battery SELL or disable mandatory
tariff top-ups generally. The executor, live SOC/BMS checks, fresh full FC03,
original transaction deadline, retry budget and maximum 180-second startup
qualification are unchanged. Forecast eligibility is not physical acceptance.

## Confirmed Pstryk publication-age gap

On the base candidate, a physically confirmed PV hold stopped at
08:02:14.940505 UTC on 4 October. The last current plan publication was
08:00:14.764590 UTC: 120.176 seconds earlier. The next accepted publication
arrived at 08:02:44.710020 UTC. SOC stayed at 25%, PV covered the home, and
full FC03, BMS and lease evidence were fresh before the stop. The generic
`no_current_plan` suppression label alone did not establish the cause.

The Pstryk runtime reused its accepted result for up to 120 seconds, exactly
the maximum authority age. An existing 30-second timer tick just before the
threshold skipped refresh and left a stale gap before the next tick. The
controller correctly withdrew stale authority; the refresh cadence was wrong.

The cache reuse interval is now 60 seconds, leaving two nominal timer ticks
before the unchanged 120-second authority limit. A new solve, fresh input
qualification and revalidation must succeed before either BUY/SELL projection
gets a new accepted revision. Failed refresh never stamps old data as fresh.
No timer, Modbus polling or Recorder configuration changes are introduced.
Accepted plan updates can occur more often than before; storage deduplication
and external price-fetch cadence remain unchanged. The original PV transaction
deadline and recovery promise are preserved.

Entry points:

- `pv_charge_delay.py::_optimize_day`: actual-stock floor and same-day recovery.
- `pstryk_joint.py::optimize`: bounded comparison with the PV-only alternative.
- `pstryk_runtime.py::_calculate`: renew accepted plan evidence before expiry;
  `tools/test_pstryk_runtime.py` replays the observed timer phase, verifies PV
  continuation with the original deadline, and rejects stale input on refresh.
- `tools/test_pv_delay_reserve_independence.py`: low SOC for RCE/Pstryk,
  conservative shortfall, home demand, BUY preservation, SELL floor and two
  active-window replans without a later deadline or lower recovery promise.
- `tools/test_pv_charge_delay_control.py`: a hold below the sale reserve still
  targets a non-discharge SOC ceiling and requests no battery export energy.

Base `f26cb57`, branch `fix/pv-delay-reserve-20261004`, frontend stays 110.
RED/GREEN, exact tested SHA/tree, 110-file manifests and deployment receipts
are in the private evidence directory `2026-10-04_PV_DELAY_RESERVE`, outside
Git. Only Installation 3 is authorized for deployment. Installations 1/2
remain HOLD until successful natural PV acceptance on this same candidate.
The single acceptance monitor was resumed by the owner on 4 October; use its
latest external checkpoint. No public RC2 merge or publication. Prior field
FAIL/HOLD/PARTIAL results and the global release HOLD remain preserved.
