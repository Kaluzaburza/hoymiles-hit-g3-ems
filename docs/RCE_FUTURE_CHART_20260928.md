# Future RCE blocks missing from the canonical chart

Base: `c7ec96ea8395f616c8462143453bf55ecd06cf02`.

On 2026-09-28 at 07:06 UTC both installation_2 and installation_3 published current
RCE candidates but canonical future slots rejected them as `direction_unavailable`.
The canonical SOC at 16:30 UTC was about 100% / 98.54%, above the respective
65% / 72% reserve. Arbitration incorrectly reused the morning live SOC instead.
Evidence: `2026-09-28_RCE_FUTURE_CHART_FIX` in the external reports directory.

The output-only canonical adapter now supplies its continuous stored-energy
cursor as SOC to future-slot arbitration. The current interval and observed
hardware, ownership, topology, BMS and GCF gates are unchanged. Future selected
slots remain UNVERIFIED; the display never grants live write authority.

Regression covers prior tariff/PV charge, no recharge, depletion before sale,
no frame mutation and physical blocking gates. Dual-track tests now require
selection to stop at the projected reserve/RCEm target. Their former expectation
allowed additional P50-only discharge intervals after the conservative path had
already reached its target; that depended on the same frozen live-SOC bug.

This is a display/projection repair, not natural-cycle M01 acceptance.
