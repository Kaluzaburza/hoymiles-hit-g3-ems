"""Enabled, non-acting RCEm must not cancel a verified RCE predecessor.

Replay the real adapter callback between retarget persistence and dispatch.
The live 2026-09-26 incident had allow_rcm=on, rcm_enabled=on, all RCEm
active flags off, a current RESTORE plan, and an unsent RCE power retarget.
This is local protocol evidence, not a physical inverter acceptance test.
"""
from __future__ import annotations

import asyncio

import test_rce_unsent_retarget_cohort as fixture


SCENARIOS = (
    "disabled_rcm_pending",
    "disabled_rcm_restore_pending",
    "disabled_rcm_release_pending",
    "disabled_rcm_restore_99_to_100",
    "disabled_rcm_release_100_to_99",
    "reject_rcm_active",
    "reject_rcm_enabled_bms",
    "reject_rcm_unknown",
    "reject_rcm_alarm",
    "reject_rcm_mixed",
    "reject_rcm_cleanup",
    "reject_rcm_gcf_mismatch",
    "reject_rcm_306_mismatch",
    "reject_rcm_gcf_stale",
    "reject_rcm_stale_result",
    "reject_rcm_recalculating",
)


if __name__ == "__main__":
    loaded_count = fixture.h.SENSOR._loaded_entry_count
    for outcome in SCENARIOS:
        try:
            asyncio.run(fixture.scenario(outcome, enabled_rcm=True))
            print(f"PASS enabled non-acting RCEm: {outcome}")
        finally:
            fixture.h.SENSOR._loaded_entry_count = loaded_count
