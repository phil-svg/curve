# curve-sim

LlamaLend / crvUSD analytics and simulation UI. Everything the UI needs
lives inside this directory.

## Requirements

- Python 3.11+ with `matplotlib` and `numpy`
  (`pip install matplotlib numpy`)
- The C++ sim engines — built once from `cpp-src/` (see Run below;
  binaries land in `bin/`).

## Configure

Create `.env` in this directory (never commit it) with one line:

```
WEB3_HTTP_MAINNET=<your Ethereum mainnet RPC url>
```

Used only by the on-chain fetchers. The UI boots and serves all cached
data without it — live market refresh just stays off.

## Run

Linux:

```
apt install g++ make nlohmann-json3-dev libboost-dev   # once, for the engines
make -C cpp-src NATIVE=-march=native                   # once; binaries land in bin/
python3 ui_server.py --port 8765
```

macOS:

```
brew install nlohmann-json boost                       # once, for the engines
make -C cpp-src                                        # once; binaries land in bin/
python3 ui_server.py --port 8765
```

Windows: use WSL and follow the Linux steps. The server itself also runs
on native Python (`python ui_server.py --port 8765`), but the sim
engines need a Unix build.

Open **http://localhost:8765/**. Tabs deep-link by path
(`/sim`, `/sldl`, `/oracles`, `/util`, `/map`, …). Stop with Ctrl+C.

The engines build from `cpp-src/` (C++17, nlohmann-json, boost headers);
`make` writes them to `bin/`.

## Data

- All candles/klines ship as packed `.bin` files (the 0-byte `.json`
  twins beside them are intentional — the engines read the binaries).
- Market snapshots and charts refresh themselves every 30 minutes while the server
  runs (needs the RPC url). To force a refresh: `python3 fetchers/fetch_markets.py`.
- Sim runs (Bad-debt sim, S.L./D.L.) execute the C++ engines in `bin/`
  on the server machine.

## Adding a price history

Each collateral series is a JSON array of 1-minute candles,
Binance-kline style — `[timestamp_ms, open, high, low, close, volume]`:

```json
[[1735689600000, 0.912, 0.914, 0.909, 0.913, 12345.6],
 [1735689660000, 0.913, 0.915, 0.912, 0.914,  9876.5]]
```

Drop it in `data/` as `_ref_table_klines_<SYMBOL>_<hours>h.json`
(e.g. `_ref_table_klines_PEPE_8760h.json` for one year) and it becomes
selectable in the S.L./D.L. data dropdown. Any source works — e.g.
Binance's public kline API or their bulk archive at
`data.binance.vision` — as long as rows are minute-spaced, oldest first.
On first use the engine converts the JSON to a packed `.bin` beside it
and reads only that from then on.

## Notes

- Restart the server after editing `ui_server.py` — Ctrl+C and start
  again (macOS/Linux shortcut: `pkill -f ui_server.py`).
- Dev tooling lives in `dev/`, research material in `research/` — both
  are local-only and not part of the served site.
