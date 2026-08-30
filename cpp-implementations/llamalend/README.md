# llamalend — LLAMMA C++ ports

Byte-exact reimplementations of the LlamaLend AMM (LLAMMA) state machine.
The two protocol versions have different Vyper contracts and different
event shapes, so each is its own engine:

    llv1/   V1 markets (CRV, WETH, wstETH, WBTC, sfrxETH, tBTC vintage)
    llv2/   V2 markets (sDOLA vintage, deployed 2026-07)

`make` in either dir builds `build/replay` (brew boost + nlohmann-json).
The companion project carrying the Python deploy-state/event fetch
pipeline and each version's reference vyper is maintained separately;
these are the engine cores.
