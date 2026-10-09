# Installation verification checklist

Offline validation and successful compilation do not establish physical wiring,
per-model operation or individual Slave acknowledgement. Keep acceptance records
with the actual installation, timestamps, exact HA/ESP versions and settings.
Do not publish device secrets or raw private diagnostic bundles.

1. Confirm device identity, source entities, exact software/package versions,
   fresh complete FC03 and unchanged configuration.
2. Observe a naturally qualified start and two newer matching readback generations.
   Distinguish commanded mode from the actual response required by that policy.
3. Correlate the transaction, completed published replans, original deadline and
   each accepted lease renewal in the bounded diagnostic journal. Mark gaps.
4. Confirm the natural end and neutral/restore outcome, followed by the +2 minute
   check and ten minutes of postflight when performing full cycle acceptance.
5. Assess measured energy, chart/history and notifications separately. Planned
   kWh, a Master readback or HA notification acceptance are not respectively a
   measurement, individual Slave ACK or phone delivery proof.

For update preparation use [the migration guide](../../UPGRADE_1_5_7.md).
For scope limitations use [compatibility](../../COMPATIBILITY.md).
