#!/usr/bin/env python3
"""routed_sim.py — venue-routed bad-debt sim.

The legacy pipeline (build_ema_precompute + C++ sweep + synth) models soft-
liquidation as "push LLAMMA's marginal price to the schedule" — an arbitrageur
with INFINITE external depth: it sources unlimited crvUSD and offloads
unlimited collateral at the schedule price. Fine for CRV (deep global market),
absurd for thin markets like svZCHF where the whole venue holds ~$200k of
crvUSD against multi-million debt.

Here every flow routes through the venue pool:

  per block:
    1. EXTERNAL ARB BOT  — INFINITE external market that always trades at the
       schedule spot. The venue is otherwise static, so whenever its marginal
       deviates the bot buys on the cheap side / sells on the dear side until
       venue marginal == external spot (chunked walk; profitable by
       construction). Runs at block start AND again after the liquidation
       flows, so nothing is ever valued at a transiently-dumped venue price.
       --ext-arb-cap-usd is an optional research knob (default unlimited).
    2. ORACLE            — EMA (half-life --ma-time-s) of the VENUE's marginal
       spot, not of the schedule: on-chain LLAMMA oracles read the pool.
    3. SOFT-LIQ ARB      — equilibrates LLAMMA ⇄ venue: buys collateral from
       LLAMMA with crvUSD and sells it into the venue (or the reverse), sized
       by ternary search on round-trip profit INCLUDING venue slippage and
       gas. Both curves move. Refuses unprofitable trades. This is where venue
       depth actually binds.
    4. HARD LIQ          — unchanged profit-tested partial liquidation, but the
       AMM leg is a REAL withdraw from the book (no more scale multipliers),
       and the seized collateral is dumped into the venue with no magic
       re-absorption afterwards.
    5. BAD DEBT          — debt_left − x − venue_spot·y, floored at 0.

LLAMMA state lives in llamma_book.py — a Python port of the C++ engine,
verified wei-exact (verify_book_port.py). Output row format matches the legacy
synth so the UI consumes either pipeline unchanged.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import synth_bad_debt as sb  # noqa: E402  (venue helpers, gas model, cache)
from llamma_book import Book  # noqa: E402
from venues import make_venue  # noqa: E402

ONE = 10 ** 18

# Round-trip arb gas: two swaps on the cheap route (LLAMMA exchange + venue
# swap), same constant family as the hard-liq profit test's 850k side-route.
# Overridable: one synthetic trade stands for many bundled arbs, and searchers
# in MEV bundles pay far less than a naive lone tx — --arb-gas 0 recovers the
# legacy C++ behavior (gasless arb).
ARB_GAS = sb.GAS_VIA_USDT


def ext_arb_to_spot(pool, target_spot: float, cap_usd: float = float("inf"),
                    tol: float = 5e-4, max_chunks: int = 64) -> dict | None:
    """External arbitrageur with INFINITE depth at the external market price.

    The external market always trades at `target_spot` (the schedule); the
    venue is otherwise static. Whenever the venue's marginal deviates, the bot
    buys on the cheap side and sells on the dear side until the venue's
    marginal == external spot — profitable by construction, bounded only by
    the venue's own curve. Walks in 5%-of-reserve chunks (single oversized
    trades revert in the wei-exact engines; chunked execs walk fine), bisecting
    the final chunk so it lands on the target instead of overshooting.

    cap_usd is an OPTIONAL research knob (CLI only, default unlimited)."""
    total_usd = 0.0
    dirs = set()
    spot = sb.artificial_crv_spot
    for _ in range(max_chunks):
        cur = spot(pool)
        if abs(cur - target_spot) <= tol * target_spot:
            break
        if total_usd >= cap_usd:
            break
        sell = cur > target_spot          # venue dear -> sell collateral into it
        i, j = (1, 0) if sell else (0, 1)
        chunk = pool.pair_balances[1 if sell else 0] // 20
        if chunk <= 0:
            break
        # Would this chunk cross the target? Bisect it down to land on target.
        def crossed_after(dx: int) -> bool | None:
            c = pool.clone()
            try:
                c.exec(i, j, dx)
            except Exception:
                return None               # reverts -> treat as too big
            return (spot(c) < target_spot) == sell
        cr = crossed_after(chunk)
        if cr is None:
            chunk //= 8                   # revert-shrink, then try as-is
            if chunk <= 0:
                break
        elif cr:
            lo, hi = 1, chunk
            for _ in range(40):
                if hi - lo <= max(chunk // 1000, 1):
                    break
                mid = (lo + hi) // 2
                c2 = crossed_after(mid)
                if c2 is None or c2:
                    hi = mid
                else:
                    lo = mid
            chunk = hi
        # Cap the spend if a finite cap was requested.
        if not math.isinf(cap_usd):
            left = cap_usd - total_usd
            cap_wei = int(left / target_spot * 1e18) if sell else int(left * 1e18)
            chunk = min(chunk, cap_wei)
            if chunk <= 0:
                break
        prev = cur
        try:
            pool.exec(i, j, chunk)
        except Exception:
            break
        total_usd += chunk / 1e18 * (target_spot if sell else 1.0)
        dirs.add("SELL" if sell else "BUY")
        if abs(spot(pool) - prev) < 1e-12:
            break                          # no progress — curve exhausted
    if total_usd <= 0:
        return None
    return {"dir": "/".join(sorted(dirs)), "usd": total_usd}


def soft_liq_arb(book: Book, user: str, pool, target_spot: float,
                 base_fee: int, eth_price: float,
                 arb_gas: int = ARB_GAS, max_rounds: int = 4,
                 gas_usd_flat: float | None = None) -> list[dict]:
    """Equilibrate LLAMMA against the venue. Each round picks the better of the
    two round-trip directions, sized by ternary search on profit:

      LIQ  : dx crvUSD → LLAMMA → dy coll → venue → dz crvUSD ; pnl = dz−dx−gas
      DELIQ: dx crvUSD → venue → dc coll → LLAMMA → dz crvUSD ; pnl = dz−dx−gas

    (both parametrized by the crvUSD the arb fronts, so the venue leg's
    slippage and the LLAMMA leg's band-walk are inside the objective).
    Executes only while pnl > 0. Logged in the C++ arb-log schema so the
    server-side aggregation is unchanged."""
    gas_usd = (gas_usd_flat if gas_usd_flat is not None
               else (arb_gas * eth_price * base_fee) / 1e18)
    trades = []

    for _ in range(max_rounds):
        book_y = sum(v[1] for v in book.bands.values())
        book_x = sum(v[0] for v in book.bands.values())

        def pnl_liq(dx: int) -> tuple[float, int, int]:
            if dx <= 0:
                return 0.0, 0, 0
            c = book.clone()
            _, coll_out = c.apply_trade_dx(0, 1, dx)
            if coll_out <= 0:
                return -1e30, 0, 0
            try:
                dz = pool.get_dy(1, 0, coll_out)
            except Exception:
                return -1e30, 0, 0
            return (dz - dx) / 1e18 - gas_usd, coll_out, dz

        def pnl_deliq(dx: int) -> tuple[float, int, int]:
            if dx <= 0:
                return 0.0, 0, 0
            try:
                coll = pool.get_dy(0, 1, dx)
            except Exception:
                return -1e30, 0, 0
            if coll <= 0:
                return -1e30, 0, 0
            c = book.clone()
            _, dz = c.apply_trade_dx(1, 0, coll)
            if dz <= 0:
                return -1e30, 0, 0
            return (dz - dx) / 1e18 - gas_usd, coll, dz

        def golden(fn, hi: int) -> tuple[float, int]:
            """Golden-section search for the profit-maximising size.

            Probes sit at the golden ratio so one of them is exactly where the
            next iteration needs a probe — 1 new evaluation per shrink instead
            of ternary's 2, and the bracket shrinks x0.618 instead of x0.667.
            The reused point is carried, never recomputed, so integer
            truncation cannot drift it. Mirrors routed_engine.cpp exactly."""
            K, D = 618034, 1_000_000                 # 1/phi, fixed point
            a, b = 0, max(hi, 1)
            tol = max(hi // 10_000, 10 ** 15)
            c = b - (b - a) * K // D
            d = a + (b - a) * K // D
            fc, fd = fn(c)[0], fn(d)[0]
            it = 0
            while b - a > tol and it < 64:
                it += 1
                if fc > fd:                          # max lies in [a, d]
                    b, d, fd = d, c, fc
                    c = b - (b - a) * K // D
                    fc = fn(c)[0]
                else:                                # max lies in [c, b]
                    a, c, fc = c, d, fd
                    d = a + (b - a) * K // D
                    fd = fn(d)[0]
            best_dx = (a + b) // 2
            return fn(best_dx)[0], best_dx

        # Upper bounds: LIQ can at most buy the book's collateral (valued at
        # the schedule); DELIQ at most re-fill against the book's crvUSD.
        hi_liq = int(book_y * target_spot) * 2 if book_y else 0
        hi_deliq = book_x * 2 if book_x else 0

        best = None  # (pnl, kind, dx)
        if hi_liq > 0:
            p, dx = golden(pnl_liq, hi_liq)
            if p > 0:
                best = (p, "LIQ", dx)
        if hi_deliq > 0:
            p, dx = golden(pnl_deliq, hi_deliq)
            if p > 0 and (best is None or p > best[0]):
                best = (p, "DELIQ", dx)
        if best is None:
            break

        _, kind, dx = best
        if kind == "LIQ":
            _, coll_out = book.apply_trade_dx(0, 1, dx)
            if coll_out <= 0:
                break
            pool.exec(1, 0, coll_out)
            trades.append({"i": 0, "j": 1, "dx": str(dx), "dy": str(coll_out)})
        else:
            coll = pool.exec(0, 1, dx)
            if coll is None:  # exec returns dy for venues; guard anyway
                coll = 0
            if coll <= 0:
                break
            _, dz = book.apply_trade_dx(1, 0, coll)
            if dz <= 0:
                break
            trades.append({"i": 1, "j": 0, "dx": str(coll), "dy": str(dz)})
    return trades


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", type=Path, required=True)
    ap.add_argument("--discount", type=float, required=True)
    ap.add_argument("--tvl-usd", type=float, required=True)
    ap.add_argument("--A-raw", type=float, default=67.5)
    ap.add_argument("--pool-type", default="cryptoswap")
    ap.add_argument("--n-coins", type=int, default=2)
    ap.add_argument("--ss-A", type=float, default=500)
    ap.add_argument("--crash-start-spot", type=float, required=True)
    ap.add_argument("--crash-end-spot", type=float, required=True)
    ap.add_argument("--crash-start-offset-s", type=float, required=True)
    ap.add_argument("--crash-duration-s", type=float, required=True)
    ap.add_argument("--price-path", default=None,
                    help="JSON [[t_seconds, spot], ...] replayed instead of the "
                         "linear start->end ramp; the schedule walks it "
                         "piecewise-linearly and holds the last price after it")
    ap.add_argument("--horizon-min", type=float, default=None)
    ap.add_argument("--ma-time-s", type=float, required=True)
    ap.add_argument("--oracle-seed", type=float, required=True)
    ap.add_argument("--ext-arb-cap-usd", type=float, default=float("inf"),
                    help="external arb bot's per-block $ capacity (default unlimited)")
    ap.add_argument("--arb-gas", type=int, default=ARB_GAS,
                    help="gas units charged per soft-liq arb round trip "
                         f"(default {ARB_GAS}; 0 = legacy gasless arb)")
    ap.add_argument("--gas-usd", type=float, default=None,
                    help="FLAT dollar gas cost per transaction (soft-liq arb "
                         "round trip and hard-liq); overrides the gas-units "
                         "x base-fee x ETH-price model")
    ap.add_argument("--base-fee-gwei", type=float, default=None,
                    help="fixed base fee replacing the per-block history "
                         "(parity with the C++ engine, which always uses this)")
    ap.add_argument("--eth-price", type=float, default=2000.0,
                    help="ETH price for gas costs when --base-fee-gwei is set")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--oracle-out", type=Path, default=None)
    ap.add_argument("--arb-log-out", type=Path, default=None)
    args = ap.parse_args()

    t_start = time.perf_counter()
    cs, ce = args.crash_start_spot, args.crash_end_spot
    off_s, dur_s = args.crash_start_offset_s, args.crash_duration_s

    path = None
    if args.price_path:
        raw = json.loads(Path(args.price_path).read_text()
                         if Path(args.price_path).exists() else args.price_path)
        path = sorted(((float(t), float(p)) for t, p in raw), key=lambda x: x[0])
        if not path:
            path = None

    def schedule(elapsed: float) -> float:
        if path is not None:
            # Replay a real price history: hold the first point until it starts,
            # walk piecewise-linearly between samples, hold the last afterwards.
            if elapsed <= path[0][0]:
                return path[0][1]
            if elapsed >= path[-1][0]:
                return path[-1][1]
            lo = 0
            for k in range(1, len(path)):
                if path[k][0] >= elapsed:
                    lo = k - 1
                    break
            (t0, p0), (t1, p1) = path[lo], path[lo + 1]
            return p0 if t1 == t0 else p0 + (p1 - p0) * (elapsed - t0) / (t1 - t0)
        if elapsed <= off_s:
            return cs
        if elapsed >= off_s + dur_s:
            return ce
        return cs - (cs - ce) * (elapsed - off_s) / dur_s

    # --- venue -------------------------------------------------------------
    A_ui = args.ss_A if args.pool_type.startswith("stableswap") else args.A_raw
    pool = make_venue(args.pool_type, args.n_coins,
                      int(args.tvl_usd * 1e18), int(cs * 1e18), A_ui)
    sb.push_pool_to_spot(pool, cs)      # one-time init snap (fit overhead only)

    # --- book ---------------------------------------------------------------
    book = Book.load(args.snapshot)
    user = next(iter(book.users))
    debt0 = book.users[user]["debt"]
    debt_left = debt0
    disc_wei = int(args.discount / 100.0 * 1e18)
    y0_tokens = sum(v[1] for v in book.bands.values()) / 1e18
    coll_scale = 1.0                    # fraction never hard-liquidated away

    # --- timeline (identical stretch formula to the legacy pipeline) -------
    cache = sb._load_cache()

    def block_ctx(b: int) -> tuple[int, int, float]:
        s = str(b)
        if s in cache["base_fee"]:
            return cache["base_fee"][s][0], cache["base_fee"][s][1], cache["eth_price"][s]
        bi = sb.block_info(b)
        ep = sb.eth_price_at(b)
        cache["base_fee"][s] = [bi["timestamp"], bi["baseFeePerGas"]]
        cache["eth_price"][s] = ep
        return bi["timestamp"], bi["baseFeePerGas"], ep

    n_blocks = sb.TO_BLOCK - sb.FROM_BLOCK + 1
    ts0 = block_ctx(sb.FROM_BLOCK)[0]
    sim_ts = None
    if args.horizon_min is not None:
        dt = args.horizon_min * 60.0 / (n_blocks - 1)
        sim_ts = {sb.FROM_BLOCK + i: int(round(ts0 + i * dt)) for i in range(n_blocks)}

    # --- loop ---------------------------------------------------------------
    ema = args.oracle_seed
    prev_ts = None
    rows, arb_log, oracle_out = [], [], {}
    settled = False
    n_hard_total = 0

    for i in range(n_blocks):
        b = sb.FROM_BLOCK + i
        ts, base_fee, eth_price = block_ctx(b)
        if sim_ts is not None:
            ts = sim_ts[b]
        if args.base_fee_gwei is not None:
            base_fee = int(args.base_fee_gwei * 1e9)
            eth_price = args.eth_price
        target = schedule(ts - ts0)

        # 1. external arb bot: INFINITE external market at the schedule spot.
        # Pulls the venue's marginal to the external price before anything else
        # trades this block.
        ext = ext_arb_to_spot(pool, target, args.ext_arb_cap_usd)

        # 2. oracle: EMA of the venue's marginal spot.
        spot_v = sb.artificial_crv_spot(pool)
        if prev_ts is not None and args.ma_time_s > 0:
            alpha = math.exp(-math.log(2) * (ts - prev_ts) / args.ma_time_s)
            ema = ema * alpha + spot_v * (1.0 - alpha)
        elif args.ma_time_s <= 0:
            ema = spot_v
        prev_ts = ts
        oracle_out[str(b)] = str(int(ema * 1e18))

        book.block_timestamp = ts
        book.external_price = int(ema * 1e18)
        # On-chain semantics, always: oracle state is only written by
        # tick_oracle inside actual trades, mirroring _price_oracle_w running
        # inside exchange txs. (The pre-2026-08 per-step bypass is gone.)

        # 3. soft-liq arb through the venue.
        trades = [] if settled else soft_liq_arb(
            book, user, pool, target, base_fee, eth_price, arb_gas=args.arb_gas,
            gas_usd_flat=args.gas_usd)
        for tr in trades:
            arb_log.append({"block": b, **tr,
                            "p_before_arb": "0", "p_after_arb": "0",
                            "target_p": str(int(target * 1e18))})

        # 4. hard liquidation (profit-tested partial, real withdraw).
        hard_usd = 0.0
        hard_profit = 0.0
        n_hard = 0
        if not settled and debt_left > 0:
            h = book.compute_health(user, disc_wei, True, debt_override=debt_left)
            if h < 0:
                x_wei, y_wei = book.get_sum_xy(user)
                if y_wei / 1e18 > 10 and debt_left / 1e18 > 10:
                    profit, frac, f_rem, dy_q = sb.best_partial_liquidation(
                        pool, base_fee, eth_price, y_wei, debt_left, x_wei,
                        disc_wei, gas_usd=args.gas_usd)
                    if profit > 0:
                        dx_rm, dy_rm = book.apply_withdraw(user, f_rem)
                        if dy_rm > 0:
                            pool.exec(1, 0, dy_rm)
                        repay = debt_left * frac // ONE
                        debt_left -= repay
                        hard_usd = repay / 1e18
                        hard_profit = profit
                        n_hard = 1
                        n_hard_total += 1
                        coll_scale *= 1.0 - f_rem / 1e18
                        if debt_left <= 10 ** 9:
                            settled = True
                            debt_left = 0

        # 4b. the bot re-pegs the venue after the liquidation dumps — the
        # external market absorbs them at spot within the block.
        ext2 = ext_arb_to_spot(pool, target, args.ext_arb_cap_usd)
        if ext2:
            ext = {"dir": ext2["dir"], "usd": (ext["usd"] if ext else 0) + ext2["usd"]}

        # 5. accounting off the real book state.
        spot_v = sb.artificial_crv_spot(pool)
        x_wei, y_wei = book.get_sum_xy(user)
        x_usd = x_wei / 1e18
        y_tok = y_wei / 1e18
        bad_debt = round(max(0.0, debt_left / 1e18 - x_usd - spot_v * y_tok))
        sl_user_loss = round(coll_scale * y0_tokens * spot_v - (y_tok * spot_v + x_usd))

        row_bands = [[n, round(v[0] / 1e18, 2), round(v[1] / 1e18, 4),
                      round(book.p_oracle_up(n) / 1e18, 6)]
                     for n, v in sorted(book.bands.items())
                     if v[0] > 0 or v[1] > 0]

        rows.append({
            "blockNumber": b,
            "LiquidationDiscount": args.discount,
            "timestamp": ts,
            "date": sb.hhmm_utc(ts),
            "elapsed_s": ts - ts0,
            "badDebt": bad_debt,
            "comp_lend_usd": round(x_usd),
            "comp_coll_tokens": round(y_tok, 2),
            "comp_coll_usd": round(y_tok * spot_v),
            "hardLiqUsd": round(hard_usd),
            "hardLiqProfit": round(hard_profit),
            "slUserLoss": sl_user_loss,
            "debtFrac": round(debt_left / debt0, 8) if debt0 else 1.0,
            "bands": row_bands,
            "real_curve_spot": round(spot_v, 6),
            "target_spot": round(target, 6),
            "pool_crv_spot": round(spot_v, 6),
            "pool_crvusd_bal": round(pool.pair_balances[0] / 1e18, 0),
            "pool_crv_bal": round(pool.pair_balances[1] / 1e18, 0),
            "profitable_this_block": n_hard,
            "settled_total": 1 if settled else 0,
            "extArbUsd": round(ext["usd"]) if ext else 0,
            "extArbDir": ext["dir"] if ext else None,
        })

        if (i + 1) % 20 == 0 or i == n_blocks - 1:
            print(f"  block {b} ({rows[-1]['date']}) badDebt=${bad_debt:>10,}  "
                  f"venue=${spot_v:.4f}  oracle=${ema:.4f}  "
                  f"x=${x_usd:,.0f} y={y_tok:,.0f}  debt=${debt_left/1e18:,.0f}")

    sb._save_cache(cache)
    args.out.write_text(json.dumps(rows))
    if args.oracle_out:
        args.oracle_out.write_text(json.dumps(oracle_out))
    if args.arb_log_out:
        args.arb_log_out.write_text(json.dumps(arb_log))
    print(f"[routed] {n_blocks} blocks in {time.perf_counter()-t_start:.1f}s  "
          f"soft-liq trades={len(arb_log)}  hard liqs={n_hard_total}")


if __name__ == "__main__":
    main()
