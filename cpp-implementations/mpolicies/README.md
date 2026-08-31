# mpolicies — the LlamaLend monetary policies

Wei-exact C++ ports of every rate policy live on the lending markets:

    mpolicies.hpp    * semilog: rate = exp(debt*(ln max - ln min)/reserves
                       + ln min), with the contract's solmate-style wad exp
                       and log2-based ln, bit for bit
                     * secondary (EMA variant): the hyperbolic utilization
                       curve r0*r_minf/1e18 + A*r0/(u_inf-u) + shift
                       (r_minf signed) over an EMA of an external rate
                       calculator, EMA state math included
                     * LLV2 dynamic (HyperbolicDynamicMP): the hyperbola
                       over a clamped external calculator rate, reserves
                       net of admin fees, curve params derived on-chain
                       from (target utilization, low/high ratio)
                     * flat time-linear: rate = clamp(base + slope*dt,
                       min, max) — the single-instance, multi-market
                       policy most markets now point at
    replay.cpp       driver: job JSON of historical samples -> recomputed
                       rate() vs the chain's answer per block

`make` builds `build/replay` (deps: brew boost + nlohmann-json; u256.hpp
shared with the llamalend ports).

Validated by sampling each market's policy rate() daily across its full
on-chain history and recomputing from the same block's inputs: 98,724
samples over 51 mainnet markets at 1-hour resolution, zero mismatches —
flat time-linear (64,000), semilog (24,000, including recomputing the
stored logarithms with our ln), EMA-secondary (8,000; the EMA layer is
checked against ma_rate() from raw storage state and the hyperbola on top
of it), and LLV2 dynamic (2,724; the stored curve is re-derived from the
raw knobs each sample and must match before the rate is checked). The two
LLV2 dynamic markets on optimism run the same verified source but are not
sampled (mainnet-only validation). Policy assignments are
re-read from every controller each refresh cycle, so swapped policies are
detected, logged and re-classified (which is how the flat time-linear
migration of most markets was caught).
