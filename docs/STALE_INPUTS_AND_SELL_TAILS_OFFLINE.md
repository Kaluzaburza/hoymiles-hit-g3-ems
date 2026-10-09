# Offline follow-up: stale inputs and small sale tails

Source base: `164508b353806a2cd66c56a41567bba5dc2c203f`, tree
`f40cb0a3c7e5cb7047b28d97bbe70c97afa334e4`. This copy builds on the separate
LOAD-margin proposal, content SHA256
`35b74a4f33c67d7da66b10fe4747b3582304e6f3b5854f2454592efe0275ad93`.
Neither the frozen checkout nor the previous proposal is modified. There is
no new Git commit, deployment, live control, OTA or public release.

## First stale-input STOP evidence

The older miernik stop at 2026-10-08 15:57:41.596954 UTC belongs to runtime
`8e143f6fba9bc707df9b7157c1b47f216d9c48f6`. Its retained plan was current,
continuation was eligible, and recent physical export/BMS evidence existed.
The journal omitted the actual failed cohort timestamp. The original restore
snapshot and an earlier retarget check are not stop-time telemetry. That
historical cause remains unresolved; no five-minute timeout is inferred.

This change records the exact failed continuation check in the existing
first-STOP journal: channel, generation, coherence, observation time, age,
applied limit and exclusive expiry boundary. EMS decoding failures retain
the bounded field-specific error. Other paths are explicitly marked
`stop_frame_only`, with `cause_attested=false`, rather than guessing a cause.
The eight-transaction retention and existing event publication remain.
No new entities, timer, periodic journal or Recorder frequency is introduced.

No authority limits change: new-write FC03 is 15 seconds; confirmed RCE
continuation can use the existing exclusive 30-second EMS ceiling; required
GCF/306 use their existing 30-second checks. Future/incoherent/missing data,
hardware support, BMS, ownership and deadlines retain their existing gates.
The diagnostic is excluded from persisted execution authority and cannot
authorize a retry, write or lease renewal.

## Sale-tail comparison

`tools/test_sale_tail_options.py` compares 15 controlled scenarios, four
variants and both production physical balances: spread, shorter block,
deferral into an already-selected later sale, and retaining only that sale.
Commands use existing integer 4306 steps and at least the current five-minute
start requirement. Energy/home reserves, BMS/AC/GCF limits, unavailable future
prices and a locked destination are checked. Terminal stock is explicitly
valued in the test; no start penalty, cooldown or fabricated LOAD margin is
used. This is a finite what-if comparison, not the production optimizer or
an estimate of actual daily energy/profit.

With the observed prices 0.752455 and 0.820125 PLN/kWh and controlled stock,
deferring 1.325 kWh into an existing feasible later sale preserves energy,
reduces starts from two to one, and improves the modeled value by
0.08966275 PLN. If the later price is lower or its capacity is full, this is
not the preferred choice. A shorter block can meet the export-power floor,
but retains the extra start and needs an explicit sub-slot end in the live
plan/command contract. Simply increasing power would overstate its safety.

The recorded LOAD step from 0.933 to 2.312 kW can still take the modeled
3.84 kW short-block command below the 2 kW export minimum (1.528 kW remains).
Thus shorter duration alone does not prove stable execution under changing
LOAD. No unmeasured headroom or relaxation of RCE flow confirmation is added.

One PV counterexample intentionally remains separate: the RCE aggregate
physical-slot model allows PV charge within a slot with controlled export;
Pstryk's explicit SELL action suppresses that charge. Sub-slot timing trials
therefore have different end stock/value. This is a model-interpretation gap,
not proof of a production inverter fault. PV parity is PARTIAL, and no common
economic policy is promoted into production on this evidence.

The runtime planner, command powers, run boundaries, retry/deadline and LOAD
model are unchanged by this follow-up. Its runtime change is diagnostic only.
To implement a selected tail strategy later, explicitly represent and test
its shorter end or deferred allocation through planner publication, actual
command, FC03 ACK, lease, natural end and restore. Never retime a committed
transaction. Exact-candidate field acceptance remains a separate gate.
