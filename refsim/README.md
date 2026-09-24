Vendored from github.com/curvefi/llamma-simulator (master, fetched 2026-08).
Used by sweep_ref_table.py — the S.L./D.L. tab's "reference table" mode — to
reproduce the published loss-from-soft-liquidation tables (single-band
positions, 0.15-day loans, deep-tail statistic).

One change against upstream (libsimulate.py, 2026-09-24): arbitrage trades go
to the market price net of the external fee. The original passed the
fee-adjusted target into trade_to_price, which applies the AMM fee per band
itself, so the fee was charged twice and arbitrage stopped a full fee short.
llamma-simulator_v2 fixed the same bug upstream in f18e123 (PR #10); this
repo's v1 upstream is unfixed, so tables made with it (including the
published ones) carry the double fee.
