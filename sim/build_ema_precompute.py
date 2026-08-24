"""Build an oracle-file whose per-block value is the EMA of the artificial pool's
imposed linear crash, then invoke C++ sweep_precompute with that oracle so the
resulting precompute reflects THIS case study's own price signal.

Rationale: previously the health of users was determined by the real CRV-market
oracle (0xe0a4c53408f5acf3246c83b9b8bd8d36d5ee38b8). That's incorrect for an
abstracted-liquidity case study — the underwater condition should be derived
from the pool we're actually simulating. Since our pool is forced to follow a
linear crash schedule (63¢→27¢ over 21:15→21:23), we EMA that trajectory with a
constant ma_half_time (tweakable) and use the smoothed series as the oracle.

Usage:
    python build_ema_precompute.py --ma-half-time 300 --discount 12
    → writes data/oracle_ema_300s.json (once per ma_half_time)
    → writes data/precompute_ema_300s_12pct.json (per (ma, discount))
"""
from __future__ import annotations
import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

FROM_BLOCK = 23_549_898
TO_BLOCK   = 23_550_007

# Defaults for the crash schedule (real 10-Oct-2025 crash: 21:15→21:23 UTC,
# spot 0.6072 → 0.2657). Overridable via CLI so the UI can sweep them.
DEFAULT_CRASH_START_OFFSET_S = 10 * 60   # window-start (21:05) → 21:15
DEFAULT_CRASH_DURATION_S     = 8 * 60    # 21:15 → 21:23
DEFAULT_CRASH_START_SPOT     = 0.6072
DEFAULT_CRASH_END_SPOT       = 0.2657

HERE  = Path(__file__).resolve().parent.parent
V1SIM = Path(__file__).resolve().parent.parent / "v1sim_data"
SNAP_PATH   = V1SIM / "snapshots/amm_0xafca625321df8d6a068bdd8f1585d489d2acf11b_23549898.json"
EVENTS_PATH = V1SIM / "events/merged_23549899_23550007.json"
SWEEP_BIN   = Path(__file__).resolve().parent.parent / "bin" / "sweep_precompute"
ONCHAIN_U2L = HERE / "results" / "onchain_measured_bad_debt.json"


def timeline(horizon_min: float | None) -> dict[int, int]:
    """Per-block timestamps for the sim.

    The 110-block window is REAL Ethereum blocks spanning 21.8 minutes, so a
    crash longer than that could never play out — a 20,000-minute ramp advanced
    0.059% and the price looked flat. `horizon_min` keeps the same 110 steps but
    stretches the seconds BETWEEN them, so any horizon runs at constant cost.
    Passing None reproduces the real timestamps bit-for-bit.
    """
    real = {r["blockNumber"]: r["timestamp"]
            for r in json.loads(ONCHAIN_U2L.read_text())}
    if horizon_min is None:
        return real
    blocks = sorted(real)
    ts0 = real[blocks[0]]
    dt = horizon_min * 60.0 / (len(blocks) - 1)
    return {b: int(round(ts0 + i * dt)) for i, b in enumerate(blocks)}


def target_spot(elapsed_s: float, start_off: float, end_off: float,
                start_spot: float, end_spot: float) -> float:
    if elapsed_s <= start_off:
        return start_spot
    if elapsed_s >= end_off:
        return end_spot
    dur = end_off - start_off
    frac = (elapsed_s - start_off) / dur
    return start_spot - (start_spot - end_spot) * frac


def build_oracle(ma_half_time_s: int, start_off: float, end_off: float,
                 start_spot: float, end_spot: float,
                 seed: float | None = None,
                 ts_by_block: dict[int, int] | None = None) -> dict[str, str]:
    """Piecewise-linear crash → per-block EMA. Seed = the oracle's value at
    window start. The REAL LLAMMA oracle is itself an EMA and was still at
    pre-crash levels (0.6646 at 21:05) — seeding at the schedule's start spot
    instead makes the sim oracle systematically ~0.03-0.06 too low, which flags
    users too early at too-good prices (measured RMSE 0.0420 vs 0.0098)."""
    ts_by_block = ts_by_block or timeline(None)
    ts0 = ts_by_block[FROM_BLOCK]
    ema = seed if seed is not None else start_spot
    prev_ts = ts0
    oracle: dict[str, str] = {}
    for b in sorted(ts_by_block):
        ts = ts_by_block[b]
        dt = ts - prev_ts
        raw = target_spot(ts - ts0, start_off, end_off, start_spot, end_spot)
        alpha = math.exp(-math.log(2) * dt / ma_half_time_s) if ma_half_time_s > 0 else 0.0
        ema = ema * alpha + raw * (1.0 - alpha)
        oracle[str(b)] = str(int(ema * 1e18))
        prev_ts = ts
    return oracle


def build_target_spot(start_off: float, end_off: float,
                      start_spot: float, end_spot: float,
                      ts_by_block: dict[int, int] | None = None) -> dict[str, str]:
    """Raw (un-EMA'd) target price per block — arb equilibrates AMM to this."""
    ts_by_block = ts_by_block or timeline(None)
    ts0 = ts_by_block[FROM_BLOCK]
    out: dict[str, str] = {}
    for b in sorted(ts_by_block):
        raw = target_spot(ts_by_block[b] - ts0, start_off, end_off, start_spot, end_spot)
        out[str(b)] = str(int(raw * 1e18))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ma-half-time", type=int, default=300,
                    help="EMA half-life in seconds for the pool-spot smoothing (default 300 = 5 min)")
    ap.add_argument("--discount", type=float, required=True,
                    help="Liquidation discount in percent (fractional OK, e.g. 8.42)")
    ap.add_argument("--precompute-out", type=Path, default=None,
                    help="Override the output precompute path")
    ap.add_argument("--oracle-out", type=Path, default=None,
                    help="Override the output oracle path (used by the UI to keep runs isolated)")
    ap.add_argument("--crash-start-spot", type=float, default=DEFAULT_CRASH_START_SPOT)
    ap.add_argument("--crash-end-spot",   type=float, default=DEFAULT_CRASH_END_SPOT)
    ap.add_argument("--crash-start-offset-s", type=float, default=DEFAULT_CRASH_START_OFFSET_S,
                    help="Seconds after window-start (21:05 UTC) at which the linear crash begins")
    ap.add_argument("--crash-duration-s",     type=float, default=DEFAULT_CRASH_DURATION_S,
                    help="Duration of the linear crash in seconds")
    ap.add_argument("--no-soft-liq", action="store_true",
                    help="Disable synthesized soft-liquidation arbs. Bands stay frozen "
                         "(real TokenExchange events are still skipped) — models the "
                         "counterfactual where the soft-liq mechanism is dead.")
    ap.add_argument("--arb-log-out", type=Path, default=None,
                    help="Write the per-block synthesized arb trades (soft-liq activity) here")
    ap.add_argument("--real-path-file", type=Path, default=None,
                    help="chart2_prices.json-style file. When set, oracle = its 'curve' "
                         "series and target-spot = its 'spot' series (the REAL crash), "
                         "and the EMA/schedule machinery is bypassed entirely.")
    ap.add_argument("--target-out", type=Path, default=None,
                    help="Explicit output path for the target-spot file")
    ap.add_argument("--oracle-seed", type=float, default=None,
                    help="Oracle EMA seed at window start (real LLAMMA oracle at 21:05 = 0.6646). "
                         "Defaults to crash-start-spot when omitted.")
    ap.add_argument("--market-discount", type=float, default=None,
                    help="If set, run a SECOND health sweep at this discount and write it to "
                         "--accounting-out. This is the bad-debt ACCOUNTING basis (who the "
                         "market flags), mirroring TS: settlements use the swept --discount, "
                         "accounting uses the market's real discount (CRV market: 8).")
    ap.add_argument("--accounting-out", type=Path, default=None,
                    help="Output path for the market-discount accounting sweep")
    ap.add_argument("--snapshot", type=Path, default=None,
                    help="LLAMMA snapshot to replay (default: the real 130-user book at "
                         "block 23,549,898). Point this at a make_snapshot output to run "
                         "an abstracted book instead.")
    ap.add_argument("--horizon-min", type=float, default=None,
                    help="Total simulated minutes across the 110 steps. Default = the "
                         "real 21.8-minute block window. Larger values stretch the "
                         "seconds between steps so long crashes actually play out.")
    ap.add_argument("--events", type=Path, default=None,
                    help="Event file to replay. A synthetic book MUST use a BlockTick-only "
                         "file — real Deposit/Withdraw/UserState events reference real "
                         "addresses that do not exist in an abstracted book.")
    args = ap.parse_args()

    snap_path   = args.snapshot or SNAP_PATH
    events_path = args.events   or EVENTS_PATH
    ts_by_block = timeline(args.horizon_min)

    # A stretched horizon must reach the C++ replay too: its clock comes from
    # BlockTick.ts, which drives rate accrual and the oracle-move guardrail.
    if args.horizon_min is not None:
        ev = json.loads(events_path.read_text())
        for e in ev:
            if e.get("kind") == "BlockTick" and e["block"] in ts_by_block:
                e["ts"] = ts_by_block[e["block"]]
        stretched = (args.oracle_out or Path(".")).parent / f"events_h{int(args.horizon_min)}.json"
        stretched.write_text(json.dumps(ev))
        events_path = stretched
        span = (max(ts_by_block.values()) - min(ts_by_block.values())) / 60
        print(f"[timeline] horizon {args.horizon_min:g} min -> {span:.1f} min across "
              f"{len(ts_by_block)} steps ({span*60/(len(ts_by_block)-1):.0f}s per step)")

    end_off = args.crash_start_offset_s + args.crash_duration_s
    if args.real_path_file is not None:
        rows = json.loads(args.real_path_file.read_text())
        oracle = {str(r["block"]): str(int(r["curve"] * 1e18)) for r in rows}
        real_target = {str(r["block"]): str(int(r["spot"] * 1e18)) for r in rows}
        print(f"[oracle] REAL-PATH mode: oracle='curve', target='spot' from {args.real_path_file.name}")
    else:
        real_target = None
        oracle = build_oracle(args.ma_half_time,
                              args.crash_start_offset_s, end_off,
                              args.crash_start_spot, args.crash_end_spot,
                              seed=args.oracle_seed, ts_by_block=ts_by_block)
    oracle_path = args.oracle_out or (
        HERE / "data" / f"oracle_ema_{args.ma_half_time}s.json")
    oracle_path.parent.mkdir(parents=True, exist_ok=True)
    oracle_path.write_text(json.dumps(oracle))
    # Sample dump
    print(f"[oracle] wrote {oracle_path}  ({len(oracle)} blocks)")
    for b in [FROM_BLOCK, FROM_BLOCK + 20, FROM_BLOCK + 50, FROM_BLOCK + 80, TO_BLOCK]:
        v = int(oracle[str(b)]) / 1e18
        print(f"    block {b}  oracle = ${v:.4f}")

    # Also emit the raw per-block target spot — the C++ replay uses it to
    # synthesize arb trades that bring the AMM to target each block, replacing
    # the real on-chain TokenExchange events (see sweep_precompute --target-spot-file).
    # With --no-soft-liq the file is EMPTY: auto-arb mode still activates (so
    # real TokenExchange events are skipped) but zero targets → zero arb trades
    # → bands frozen. That's the "soft-liq mechanism dead" counterfactual.
    target_path = args.target_out or (
        oracle_path.parent / f"target_spot_{oracle_path.stem.replace('oracle_ema_','').replace('s','')}s.json")
    if args.no_soft_liq:
        target_path.write_text(json.dumps({}))
        print(f"[target ] soft-liq DISABLED — wrote empty target file {target_path}")
    elif real_target is not None:
        target_path.write_text(json.dumps(real_target))
        print(f"[target ] wrote REAL spot path {target_path}")
    else:
        target_path.write_text(json.dumps(
            build_target_spot(args.crash_start_offset_s, end_off,
                              args.crash_start_spot, args.crash_end_spot,
                              ts_by_block=ts_by_block)))
        print(f"[target ] wrote {target_path}")

    pre_path = args.precompute_out or (
        HERE / "data" / f"precompute_ema_{args.ma_half_time}s_{args.discount}pct.json")
    pre_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[precompute] running {SWEEP_BIN.name}...")
    cmd = [
        str(SWEEP_BIN), str(snap_path), str(events_path),
        str(FROM_BLOCK), str(TO_BLOCK), str(args.discount),
        str(pre_path), str(oracle_path), str(target_path),
    ]
    if args.arb_log_out:
        cmd.append(str(args.arb_log_out))
    subprocess.run(cmd, check=True)
    print(f"[precompute] wrote {pre_path}")

    # Second sweep at the MARKET discount → bad-debt accounting basis.
    if args.market_discount is not None and args.accounting_out is not None:
        cmd2 = [
            str(SWEEP_BIN), str(snap_path), str(events_path),
            str(FROM_BLOCK), str(TO_BLOCK), str(args.market_discount),
            str(args.accounting_out), str(oracle_path), str(target_path),
        ]
        subprocess.run(cmd2, check=True)
        print(f"[precompute] wrote accounting basis {args.accounting_out} "
              f"(market discount {args.market_discount}%)")


if __name__ == "__main__":
    main()
