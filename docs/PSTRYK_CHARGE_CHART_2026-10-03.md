# Pstryk: planned charging on the dynamic-sale chart

The Pstryk chart now includes blue **Ładowanie taryfowe / Tariff charging**
blocks, a separate legend and plan cards. Selecting a block shows the net
price, stored battery energy and total grid import (including home demand
and charging losses). This is a plan, not a physical execution claim.

Canonical UI source: `home_assistant/www/hoymiles-rce-chart-card.js`.
The generated resource copy comes from `tools/build_hacs_assets.py`.
Frontend revision 109 is shared by the module, bootstrap and `assets.py`.

Data contract:

- Read `sensor.hoymiles_hit_tariff_charge_plan.planned_slots` only when
  both provider helpers are Pstryk, both plans are current/not pending, and
  their joint plan/profile revisions match the current sale cohort.
- Use offset-aware `start_utc`/`end_utc`; never infer a BUY block from cheap
  prices or ambiguous local clock times. Preserve clipped starts, gaps,
  date boundaries and distinct occurrences of the repeated DST hour.
- Require positive stored energy and `grid_support_and_charge`. Home-only
  `grid_support` is not battery charging. Do not add BUY kWh to sale totals
  or label imports as sale revenue. The sale lockout does not suppress BUY.
- Unavailable/pending paired plans hide charging geometry and show an
  unavailable notice; a current empty BUY plan has a distinct no-plan notice.
- No new subscriptions, Modbus reads, Recorder writes or control commands.
  Prices retain the public hourly Pstryk net contract without added fees.

Validation entry points: `tools/test_pstryk_charge_chart.js`,
`test_pstryk_ui.js`, `test_rce_48h_ui_contract.js`,
`test_frontend_bootstrap_revision_contract.js`, `test_pv_charge_delay_ui.js`.
Historical `validate_rce_card.js` is pinned to frontend 107 and the historical
Task 02 freeze validator has separate HOLD conditions; neither is weakened.
The current bootstrap test verifies all three revision declarations and
actual registration/cache behavior in five load orders.

Private evidence: `2026-10-03_PSTRYK_CHARGE_CHART` (under the existing report
directory outside Git), including fresh source read, tests, exact manifest,
scoped backup/rollback, remote hashes and screenshots. Base: `4e537ff`.
Only Installation 3 may be updated. Installations 1/2 wait for the natural
PV-delay acceptance of this exact candidate; the monitor stays paused until
resumed by the user. No publication or current source ZIP is implied.
