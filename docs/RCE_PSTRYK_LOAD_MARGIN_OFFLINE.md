# RCE / Pstryk: bounded LOAD-only loss of minimum export

Local candidate based on `164508b353806a2cd66c56a41567bba5dc2c203f`.
This source copy is an offline proposal; it is not a new frozen Git commit,
deployment receipt, real-inverter acceptance or public release.

The current version was reproduced through the production optimizer,
publication, HA templates, Supervisor and lease client with simulated I/O.
RCE could discard the current slot when a brief positive LOAD pulse left
less than the minimum net export, while the previous diagnostic covered only
complete consumption of the discharge budget. Pstryk could retain a nominal
SELL but withhold execution as `live_power_insufficient` for the same pulse.

The additional `current_slot_load_only_export_suppressed` observation is
separate from the old full-budget diagnostic. RCE revalidates only the
already accepted allocation with its earlier qualified LOAD; every other
input remains current. Pstryk checks whether replacing only its live LOAD
restores execution on the same current joint plan. Changed prices,
forecast/reserve/capability restrictions and unavailable data do not acquire
new authority. The counterfactual is never published as a plan or command.

The observation is consumed only by the existing post-command controller
hold. It neither starts a transaction nor retargets/increases power. The
existing physical readback/response, BMS, SOC, topology, GCF, consent, lease
and immutable run deadline still apply. Its 180-second ceiling is anchored
to the actual sent command; renewals and replans cannot move it. A confirmed
retarget retains its existing per-command contract. The planner's additional
proof is restricted to the existing first 150 seconds of the commitment.

Pstryk publishes the non-controlling member of the pair first during an
already confirmed sale. The incumbent's still eligible predecessor then
uses the existing bounded publication wait while the joint revisions are
temporarily unequal. The matching revision gate remains mandatory; this
does not authorize mismatched pairs or change BUY ownership.

Regressions:

- `python tools/test_rce_pstryk_load_margin.py`: eleven full-stack cases,
  including three recorded LOAD amplitudes for each planner, whole-budget
  and stable controls, and both Pstryk replan timings.
- `python tools/test_rce_pstryk_load_margin_guards.py`: both negative-plan
  shapes, fresh/counterfactual input restrictions, fixed timer, restore,
  original deadline, retarget, restart and physical safety gates.
- `python tools/test_pstryk_settling.py`: joint-plan and live-only proof,
  unchanged nominal LOAD plus missing/changed input rejection.
- The broader existing RCE/Pstryk/Supervisor/lease and scheduler matrix is
  recorded in the external evidence manifest, including original failures.

The pre-existing Pstryk continuity fixture also supplies the mandatory
false PV-delay settling metadata. Its missing dictionary key was a fixture
error, not evidence of a production Pstryk failure.

The change does not introduce a cooldown, price-window merging, an invented
start cost or a new power/SOC setting. A small sale still needs sufficient
LOAD headroom after settling. Full-day economic optimality and the older
field `stale_inputs` stop have not been established by this replay.
Exact-candidate field validation and publication remain separate gates.
