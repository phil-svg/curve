# cpp-implementations — the C++ engines, by protocol family

**This is the canonical home of the engine sources.** One directory per
family:

    pools/            every Curve pool version (stableswap v1 / factory /
                      NG / meta / lending, cryptoswap classic, tricrypto2,
                      tricrypto-NG, twocrypto v2.1.0d) + the replay drivers
    llamalend/llv1/   LlamaLend LLAMMA, V1 contracts (CRV, WETH, wstETH,
                      WBTC, sfrxETH, tBTC vintage)
    llamalend/llv2/   LlamaLend LLAMMA, V2 contracts (sDOLA vintage, 2026-07)
    yieldbasis/       the Yield Basis Twocrypto v3.0.0 fork engine
                      (stableswap math, donations, WBTC policy controller)

`make` inside pools/, llamalend/llv1/ and llamalend/llv2/ builds each
`build/replay` (deps: brew boost + nlohmann-json). The Yield Basis engine
is a header compiled into pools' replay via the include path — pools/ and
yieldbasis/ build together. Note the OLDER Yield Basis pool family
(Twocrypto v2.1.0d, also iREET) is covered by pools/src/twocrypto_ng.hpp,
while yieldbasis/ holds the current v3.0.0 fork.

## Who consumes these sources

- pool engines: validated by replaying real on-chain events and matching
  every output (dy, fees, minted/burned supply, price_scale after every
  repeg) plus the final state wei-for-wei against the chain, over the
  top-80 Curve pools by TVL. The validation harness and its job caches are
  maintained privately alongside this repo; each engine header documents
  its own job JSON schema, so the replay drivers are usable standalone.
- llamalend/llv1 + llv2: byte-exact LLAMMA replay ports; the companion
  deploy-state/event fetch pipeline (Python) and each version's reference
  vyper are maintained in a separate project.
- cpp-src/ (elsewhere in this repo) is a DIFFERENT, extended LLAMMA
  variant powering the S.L./D.L. simulation module — related lineage, not
  a duplicate.
