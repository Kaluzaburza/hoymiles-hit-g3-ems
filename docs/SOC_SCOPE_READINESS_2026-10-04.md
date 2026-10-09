# SOC scope and LOAD fallback correction — 4 October 2026

Base: `8bcf17a38c6e461f91efcb0b605e225c772afd91`. The user authorizes
one tested commit on all three installations. Exact SHA/tree, tests, packages,
fresh host identities, 110 hashes and deployment receipts belong in external
evidence `2026-10-04_SOC_SCOPE_READINESS_FIX`. Frontend remains 111: these are
backend corrections. The natural PV monitor remains PAUSED until requested.
Public RC2 merge/release and the existing field gates remain HOLD.

## Confirmed defects and minimal changes

- Native RCE reused its sale reserve as the hard floor for household simulation,
  shadow comparison and projection. Pstryk also used it as the common BUY floor
  and terminal home reserve. At physical Self-Use 10%, RCE margin 15 percentage
  points and SOC 20%, this incorrectly predicted grid import and could request
  a mandatory purchase. Household trajectories now use physical Self-Use.
  Separate per-slot SELL floors retain RCE's additional margin, qualified future
  household demand, whole-percent command target and active incumbent floor.
  Manual Force Discharge minimum likewise remains a sale constraint.
- Pstryk uses the same split for the priced horizon and unpriced tail. A BUY
  maximum below the sale reserve no longer rejects the whole household plan;
  it still cannot authorize sale below that reserve.
- On localhost after returning to RCE + TAURON G12w, the shared LOAD model kept
  an expired historical estimate (~22.13 kWh) above the configured 17 kWh/day
  fallback. The tariff therefore rejected it as `load_profile_data_stale` while
  showing a calculated no-charge plan. Once history exceeds its existing
  30-hour age, the source selects the explicit configured fallback. The broker
  still validates that setting, and history age/quality are not renewed.

`base_reserve_energy_kwh` now reports physical household reserve;
`sale_base_reserve_energy_kwh` reports the additional floor for sale. The
existing command and protected-sale outputs keep their execution meaning.

Tariff additional SOC remains a percentage of protected demand energy, not
percentage points of the battery floor: 10 kWh with 10% requests 1 kWh extra.
It remains consumable, bounded by capacity, with unmet margin/base reported.
No new tariff margin policy is introduced.

## Evidence and boundaries

Regressions reproduce both defects before the fix, verify household use below
the sale buffer, absence of spurious BUY, separate sale/active floors and
unchanged physical minimum. LOAD checks cover stale higher history, a current
explicit fallback, rejection of unavailable fallback and unchanged timestamps.
Existing optimizer, Pstryk, tariff margin, projection and executor suites cover
the affected paths. Offline validation and technical deployment are separate
from natural PV/SELL acceptance.

78 scoped offline suites PASS, including 86 optimizer scenarios. Updated older
assertions distinguish the 30/32, 46/50.6 and 26.45/31.05 kWh home/sale floors;
the full-horizon economic fixture explicitly uses a zero Self-Use floor.
The missing-P10 scenario still guards sale while household feasibility uses
its independent minimum. Initial failing logs remain in external evidence.

No firmware, Modbus, lease, retry, deadline, user setting or consent change.
Preserve the user's localhost RCE + G12w selection at deployment. Existing
FREEZE-LEGACY and RCEM-HISTORY-CADENCE-01 findings remain open.
