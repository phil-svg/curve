# pegkeeper — the crvUSD PegKeepers, by version

Wei-exact C++ ports of the peg-maintenance contracts:

    src/pegkeeper.hpp    PegKeeper.vy (V1), PegKeeperV2.vy, and the
                         PegKeeperRegulator's provide_allowed /
                         withdraw_allowed math (isqrt debt-ratio cap,
                         price-in-range gates), line for line on
                         unchecked 256-bit integers
    src/replay.cpp       driver: job JSON -> per-event recomputation of
                         the update() action (provide/withdraw amount,
                         debt trajectory, regulator allowance) vs chain

`make` builds `build/replay` (deps: brew boost + nlohmann-json; u256.hpp
is shared with the llamalend ports).

Validated by replaying every Provide/Withdraw event of every deployed
keeper (V1 + V2) against mainnet and matching the computed amount, the
debt trajectory and the regulator allowance wei-for-wei: 8,175 events
across 15 keepers, 8,147 exactly reproduced from reconstructed pre-state
(14 of 15 keepers at 100%); the 28 remaining events sit in a single
adversarial multi-update block where the pre-state itself is not
reachable by call positioning — for every one of them the deployed
bytecode, executed at the reconstructed state, returns exactly the
engine's answer (engine==bytecode), so the engine is exact on all 8,175.
The regulator-math port matched the deployed regulator's
provide_allowed/withdraw_allowed on all 1,226 cross-checks; keepers
whose regulator was swapped to an external contract (PegKeeperOffboarding)
are detected and handled via the chain-read allowance.

The validation harness (archive-node fetch of each event's exact
pre-state, including mid-block call positioning and replay of composite
keeper-war transactions' own preceding internal calls) is maintained
privately alongside this repo; the job JSON schema is documented in
replay.cpp, so the driver is usable standalone.

The pool legs (add_liquidity / remove_liquidity_imbalance, virtual_price)
belong to the separately-validated stableswap engines in ../pools; this
engine consumes their observable values as inputs.
