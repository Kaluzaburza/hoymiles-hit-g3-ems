# PSTRYK-BUY-RESERVE-01 — integer reserve target

Scope: confirmed small BUY quantization defect, Installation 3 only. Base
`0ac9a8a862549376aaf7fc2bd289fe8bea60026d`, branch
`fix/pstryk-reserve-target-20261004`, frontend `1.5.8rc2.110`.

The field plan repeatedly advertised required charging from SOC 13% to 14%
while its own protected reserve was 15%. The unchanged tariff executor correctly
rejected this target; the inverter stayed in Self-Use. Independent local replay
reproduced the rejection. LOAD readiness and inverter stabilization were not
the cause. Evidence is outside Git in `2026-10-04_TARIFF_OPERATION`.

## Minimal correction

- Only mandatory reserve restoration rounds the required stop target and
  requested power upward to their register steps. Actual command power remains
  bounded downward by AC, configured and physical limits. Stored energy remains
  capped by the target, maximum SOC, BMS energy budget and fixed revalidation
  budget. The target stops charging before the remaining slot can overfill it.
- Projection cannot advertise start/continuation readiness when the current
  hourly BUY run cannot reach the protected integer target, or when a required
  charge contains only grid support. It publishes `reserve_target_unreachable`
  and the modeled target shortfall, without inventing stored energy.
- The EMS next-action summary distinguishes a currently blocked tariff start
  from a future planned block. It uses the current tariff revision and a fresh
  matching executor candidate; reasons are available in Polish and English.

Reserve settings and economic policy remain unchanged. No weakening of the
executor, owner/lease, FC03, BMS, SOC, retry or deadline checks. BUY stays hourly;
SELL continuity and the public net price shared by BUY/SELL are preserved.
This does not implement the deferred 1.5.9 BUY pair search.

## Evidence and remaining work

`2026-10-04_PSTRYK_BUY_RESERVE` holds RED/GREEN tests, exact tested SHA/tree,
110-file manifests, deployment backup/receipts, settings comparison and live
postflight. Start at its `RESULT.json` and `REPORT.md` for the actual outcome.
Regression cases cover small top-ups, fractional floors, maximum SOC, power/BMS
limits, short slots/hour boundaries, unchanged revalidation budgets, equal-price
non-trades and current-versus-future UI availability. Generated assets must
remain deterministic. Historical global freeze checks remain a separate gate.

Installation 3 deployment is authorized. Installation 1/2 packages are local
preparation only, held until a successful natural PV-delay cycle on the same
candidate. The PV monitor remains PAUSED. No merge to publication RC2, push,
tag or publication. Earlier FAIL/HOLD/PARTIAL evidence stays intact.
