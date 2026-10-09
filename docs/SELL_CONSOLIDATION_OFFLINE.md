# Qualified SELL consolidation — offline candidate, 8 October 2026

This stage follows the diagnostic proposal in
[STALE_INPUTS_AND_SELL_TAILS_OFFLINE.md](STALE_INPUTS_AND_SELL_TAILS_OFFLINE.md).
It implements qualified deferral in an isolated source copy based on Git
`164508b353806a2cd66c56a41567bba5dc2c203f`. The canonical frozen checkout and
both preceding source proposals remain unchanged. A SHA256 source manifest
identifies this proposal; it does not have a new Git commit.

## Behavior

An isolated SELL allocation can be absorbed into an already selected later
nonadjacent slot. The destination must have at least the source price, enough
physical capacity, and a feasible complete horizon without loss of objective.
The check covers continuous allocations and the whole-register 4306 trajectory.
Up to 32 transfer attempts are allowed. No extra energy or new sale slot is
created. A cheaper/full/unsafe destination leaves a valid original allocation
in place. Existing unusable-tail cleanup remains applicable.

This refinement belongs to a new plan calculation. Fixed revalidation is
reduction-only and does not retime accepted energy. Existing active commitment,
ownership, readback, lease and immutable deadline rules remain authoritative.
No arbitrary minimum kWh, cooldown, synthetic startup charge or short-block
deadline is introduced.

## Physical and economic interpretation

A controlled Mode-5 SELL occupies its entire modeled interval: it cannot also
refill the battery from PV during that same interval. PV charging remains
available during Self-Use, including a separate interval after sale. RCE now
uses the same explicit SELL interpretation as Pstryk. Historical aggregate
timeline payloads retain their decoding compatibility.

The search uses the smallest executable integer-register command rather than
pricing a near-zero allocation that changes the operating mode. Physical
feasibility and objective caches use exact allocations rather than rounded
energy keys. Both conservative and expected trajectories must be feasible.

Changing the PV refill discontinuity exposed incomplete search seeds. Existing
whole-horizon alternatives are retained. Short horizons gain minimum-command
seeds; longer horizons gain a bounded beam over ten highest-price rows, width
eight, four candidate amounts per row, followed by one refinement pass on its
selected rows. Up to four additional minimum-command alternatives repair a
residual that fits a mathematical tail but cannot start an executable command;
each alternative refills at most ten existing choices over the complete horizon.
A replacement must improve the full objective. This is a bounded
heuristic, not a proof of a global daily optimum.

## Evidence and limits

Tests cover two complete production optimizers reducing two starts to one,
equal/higher prices, full/lower-price destinations, reserve/capacity vetoes,
integer-register value loss, bounded work and fixed revalidation. Fifteen
controlled scenarios compare two physical balances and four scheduling variants;
a shorter block alone still fails after the recorded LOAD increase.

Full-cycle tests use real planner/publication/Jinja/HA adapter/controller and
lease-client code with qualified synthetic inputs, a virtual clock and modeled
ESP/FC03 responses. They check one 30-minute transaction, published completed
replans, two or more newer matching FC03 generations, every accepted renewal,
natural deadline, complete restore and 610 seconds of postflight. Energy and SOC
in these tests are model values, not site-meter measurements.

An archived economic benchmark contains a 0.3845 kW tail below the existing
0.52 kW start gate. Its historical value is preserved. The executable reference
tops up that tail from its earlier adjacent sale while preserving total energy;
the same 0.001 PLN comparison tolerance and all reserve/import checks remain.
The new reference first reproduced a 0.02750 PLN search gap, which the bounded
minimum-command alternatives correct. The long revalidation fixture gives its
first slot a strict price premium to test active revalidation independently of
legitimate equal-price deferral. Neither adjustment changes runtime authority.

The final external report binds each result to script/runtime/source/log hashes
and preserves failed trials and their resolutions. Existing oracle tolerances,
reserve invariants and performance ceilings are not weakened. A focused
regression result is not the full CI/release union or field acceptance.

The earlier `stale_inputs` evidence patch is retained: it records the first
failed channel, age, limit, generation and stage without changing authority.
The historical event on the older miernik SHA remains unresolved because its
source timestamps were not recorded. Offline replay cannot recover them.

## Remaining external gates

A separate authorized stage must create the exact Git candidate, complete the
required release-validation union, and prepare a matching deployment package.
Any deployment needs fresh host identity, baseline hashes, state checks,
backup/rollback and postflight, followed by natural acceptance on that host.
No hosts, device modes, settings, firmware, Recorder cadence or public release
are changed by this offline proposal.
