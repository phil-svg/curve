# ZCHF/crvUSD simulation

This folder contains the sweep script and the two input snapshots used for the ZCHF/crvUSD simulations:

- `sweep_parameters.py`
- `zchf_crvusd_1m.json.gz` — ZCHF/crvUSD one-minute market data
- `crvusd_usd_1m.csv` — crvUSD/USD one-minute oracle data

It is sufficient to rerun the sweep inside `llamma-simulator_v2`; the script imports the repository's generic `simulator/amm/lending_amm.py`.

From the repository root:

```bash
python -m zchf_crvusd.sweep_parameters --output zchf_crvusd/parameter_sweep.csv
```

Use `--help` to see or override the sweep parameters.
