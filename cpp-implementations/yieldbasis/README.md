# yieldbasis — the Yield Basis stack, engine by engine

Wei-exact C++ ports of the three layers of a YB market:

    twocrypto_yb.hpp   the Twocrypto v3.0.0 fork pool (stableswap math,
                       donations, WBTC policy controller) — a header
                       compiled into ../pools' replay driver; pools/ and
                       yieldbasis/ build together
    levamm.hpp         the LEVAMM leverage AMM (AMM.vy): the constant-
                       leverage invariant (get_x0), exchange in both
                       directions with the final-state check, interest
                       accrual, the LT deposit/withdraw legs and fee
                       collection
    lt.hpp             the LT vault (LT.vy): the staker/admin value split
                       with loss recovery (_calculate_values), deposit
                       share minting, withdraw fractions, admin-fee
                       minting, borrower-fee distribution
    replay_yb.cpp      driver: job JSON -> sequential simulation of each
                       transaction's full event cascade (an arb tx can
                       exchange several times; a deposit adds liquidity,
                       mints shares and distributes fees) vs the chain

`make` builds `build/replay_yb` (deps: brew boost + nlohmann-json;
u256.hpp shared with the llamalend ports).

Validated by replaying the complete event history of every deployed YB
market — 90,199 transactions across all 11 markets (three deployed LT
generations, including the pre-2025-10 vintage that values liquidity by
pool price_oracle instead of price_scale) — against mainnet, wei-for-wei
per field. Every deterministic leg reproduced exactly: all 65,036
exchanges, all 19,914 deposit invariants, all 102,127 interest-fee
collections, all 74,470 borrower-fee distributions, 15,850/15,851
withdraw pairs, 644/646 admin-fee mints, 19,726/19,901 deposit share
mints (99.80% of transactions fully exact). The residual ~0.2% are
±1..9-wei share roundings in the value-split's oracle-read position
(the stablecoin aggregator's price() vs price_w() differing by a wei at
exact mid-transaction positions, amplified by near-100%-staked dust
vaults) — the split math itself was cross-proven against the chain's own
pricePerShare at reconstructed states.

The harness reconstructs each transaction's exact pre-state from an
archive node — mid-block positioning, exact interest accrual from raw
rate storage, and replay of the transaction's own leading internal frames
for values the contracts read mid-transaction (the LT oracle after the
tx's cryptopool deposit). The borrower-fee min_amount guard depends on
the cryptopool's lp_price a frame earlier still and is checked
informationally (that leg belongs to the pool engine). Harness and job
caches are maintained privately alongside this repo; the job schema is
documented in replay_yb.cpp.
