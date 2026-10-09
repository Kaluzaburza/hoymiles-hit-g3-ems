# PV Delay: inverter mode owns power routing

The owner explicitly requested removal of PV-delay flow vetoes after installation_3's
2026-10-05 08:35:13 UTC stop. New GRID 3978 W was compared with old LOAD 2160 W;
LOAD 616 W arrived 1.11773 s later. With PV 4594 W the completed balance matched
exactly. The measured callback sequence and failed cycle remain in the external
`2026-10-05_EMS_CHARGE_STABILITY/pv-delay-flow-contract` report.

For `PV_CHARGE_HOLD` only, PV/LOAD/GRID/BAT and independent BMS **power** are
diagnostic data. Missing, changing or contradictory power values do not veto the
mode or suppress its lease renewal. There is no minimum PV/export power and no
new balance tolerance. Inverter Mode 5 is responsible for energy routing.

Execution requires two new physical FC03 generations spanning the existing
15-second qualification, followed by fresh full-block confirmation. The whole
command and its pre-command generation are checked by the executor and lease
adapter. Confirmation evidence explicitly says `pv_hold_mode5_fc03`,
`power_flows=diagnostic_only`, and `energy_effect=not_attested_by_mode_readback`.
This confirms configuration, not energy quantity or coverage of house demand.
Parallel confirmation remains Master readback, not per-Slave acknowledgement.

Keep plan/replan qualification, consent, topology, SOC, device/BMS readiness,
exclusive owner, full FC16/FC03, fixed deadline, retry budget, restart recovery,
Off-Grid preservation and verified restore. Lease 120 s/renew 20 s and startup
budgets are unchanged. RCE SELL, tariff and RCEm flow contracts are unchanged.

RED test commit `8595773` establishes the old behavior. New regressions replay
the house-load step, absent/mismatched flows, real HA renewal gate, firmware
lease model, three replans and natural deadline/restore. Existing PV tests now
assert the owner's new contract; physical-readback and control veto tests remain.
Exact tested SHA, test logs, installation_3-specific manifest, receipt and field result
are recorded outside the repository in `pv-delay-mode-authority`.

Deployment authority is limited to installation_3. Other three installations and public
release remain HOLD. Offline validation and technical deployment cannot be
reported as successful natural-cycle acceptance.
