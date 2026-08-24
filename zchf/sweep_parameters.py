#!/usr/bin/env python3
"""Sweep ZCHF/crvUSD LLAMMA parameters using the v2 AMM model.

Market OHLC is denominated in crvUSD/ZCHF.  The LLAMMA oracle is a midpoint
EMA of that market multiplied by Ethereum AggregateStablePrice (USD/crvUSD),
giving USD/ZCHF.  All parameter combinations use identical window selections.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import random
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from simulator.amm.lending_amm import LendingAMM

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MARKET = SCRIPT_DIR / "zchf_crvusd_1m.json.gz"
DEFAULT_AGGREGATE = SCRIPT_DIR / "crvusd_usd_1m.csv"
AGGREGATE_ADDRESS = "0x18672b1b0c623a30089a280ed9256379fb0e4e62"

MARKET: list[list[float]] = []
ORACLE: list[float] = []
ES99_TAIL_FRACTION = 0.01
ES99_75_TAIL_FRACTION = 0.0025


def format_duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    days, seconds = divmod(seconds, 86_400)
    hours, seconds = divmod(seconds, 3_600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def validate_market(rows: list[list[float]]) -> list[list[float]]:
    """Validate and return a timestamp-ordered, contiguous one-minute market."""
    if not rows:
        raise ValueError("market data is empty")
    for previous, current in zip(rows, rows[1:]):
        if current[0] != previous[0] + 60:
            raise ValueError(f"market data is not contiguous at {current[0]}")
    return rows


def load_market_csv(path: Path) -> list[list[float]]:
    """Load timestamp/open/high/low/close/volume candles from a CSV file."""
    rows: list[list[float]] = []
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"market CSV must contain {sorted(required)}")
        for line_number, row in enumerate(reader, 2):
            try:
                timestamp = datetime.fromisoformat(row["timestamp"])
                if timestamp.tzinfo is None:
                    raise ValueError("timestamp has no UTC offset")
                rows.append(
                    [
                        int(timestamp.timestamp()),
                        float(row["open"]),
                        float(row["high"]),
                        float(row["low"]),
                        float(row["close"]),
                        float(row["volume"]),
                    ]
                )
            except (TypeError, ValueError) as error:
                raise ValueError(f"invalid market CSV row at line {line_number}") from error
    return validate_market(rows)


def load_market(path: Path) -> list[list[float]]:
    if path.suffix.lower() == ".csv":
        return load_market_csv(path)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        raw = json.load(stream)
    rows = [[int(row[0]) // 1000, *map(float, row[1:6])] for row in raw]
    return validate_market(rows)


def load_aggregate(path: Path, market: list[list[float]]) -> list[float]:
    values: dict[int, float] = {}
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        required = {"chain_id", "aggregator_address", "epoch_time", "answer"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"aggregate CSV must contain {sorted(required)}")
        for row in reader:
            if int(row["chain_id"]) != 1:
                raise ValueError("aggregate CSV contains a non-Ethereum row")
            if row["aggregator_address"].lower() != AGGREGATE_ADDRESS:
                raise ValueError("aggregate CSV contains an unexpected aggregator")
            timestamp = int(row["epoch_time"])
            value = int(row["answer"]) / 1e18
            if timestamp in values or value <= 0:
                raise ValueError(f"invalid aggregate observation at {timestamp}")
            values[timestamp] = value
    try:
        return [values[int(row[0])] for row in market]
    except KeyError as error:
        raise ValueError(f"aggregate CSV does not cover market timestamp {error.args[0]}") from error


def build_oracle(market: list[list[float]], aggregate: list[float], half_life: int) -> list[float]:
    ema = market[0][1]
    previous = int(market[0][0])
    result = []
    for row, usd_per_crvusd in zip(market, aggregate):
        timestamp, _open, high, low, _close, _volume = row
        midpoint = (high + low) / 2
        decay = 2 ** (-(timestamp - previous) / half_life) if half_life else 0.0
        ema = ema * decay + midpoint * (1 - decay)
        result.append(ema * usd_per_crvusd)
        previous = int(timestamp)
    return result


def select_windows(
    warmup: int,
    latest_start: int,
    length: int,
    samples: int,
    rng: random.Random,
    all_windows: bool,
    stride: int,
) -> list[tuple[int, int]]:
    """Select fixed-length windows while preserving legacy random sampling."""

    if all_windows:
        starts = range(warmup, latest_start + 1, stride)
    else:
        starts = (rng.randint(warmup, latest_start) for _ in range(samples))
    return [(start, length) for start in starts]


def upper_tail_mean(ordered_losses: list[float], tail_fraction: float) -> tuple[float, int]:
    """Return the mean and count of an upper loss tail."""

    if not ordered_losses:
        raise ValueError("losses must not be empty")
    if not math.isfinite(tail_fraction) or not 0 < tail_fraction <= 1:
        raise ValueError("tail fraction must be finite and in (0, 1]")
    tail_samples = max(1, math.ceil(len(ordered_losses) * tail_fraction))
    return statistics.mean(ordered_losses[-tail_samples:]), tail_samples


def summarize_losses(losses: list[float], top_samples: int) -> dict[str, float | int]:
    """Calculate ordinary, configurable worst-N, and percentile-tail metrics."""

    if not losses:
        raise ValueError("losses must not be empty")
    ordered = sorted(losses)
    worst_n = min(top_samples, len(ordered))
    es99_loss, es99_samples = upper_tail_mean(ordered, ES99_TAIL_FRACTION)
    es99_75_loss, es99_75_samples = upper_tail_mean(ordered, ES99_75_TAIL_FRACTION)
    return {
        "mean_loss": statistics.mean(ordered),
        "median_loss": statistics.median(ordered),
        "max_loss": ordered[-1],
        "worst_n_mean_loss": statistics.mean(ordered[-worst_n:]),
        "worst_n": worst_n,
        "es99_loss": es99_loss,
        "es99_samples": es99_samples,
        "es99_75_loss": es99_75_loss,
        "es99_75_samples": es99_75_samples,
    }


def simulate(task: tuple[int, float, int, int, int, float, float]) -> float:
    A, fee, start, length, bands, external_fee, dynamic_fee_multiplier = task
    prices = MARKET[start : start + length]
    oracles = ORACLE[start : start + length]
    start_oracle = oracles[0]
    p_base = start_oracle * (A / (A - 1) + 1e-4)
    amm = LendingAMM(p_base, A, fee, dynamic_fee_multiplier)
    amm.deposit_nrange(1.0, start_oracle, bands)
    initial_value = amm.get_all_x()
    # A newly opened loan has no historical oracle jump.  Initialise the
    # timestamped memory explicitly instead of treating p_base -> oracle as a
    # real price observation and charging a fictitious first-candle fee.
    amm.p_oracle = start_oracle
    amm.prev_p_oracle = start_oracle
    amm.raw_p_oracle = start_oracle
    amm.old_p_oracle = start_oracle
    amm.old_dfee = 0.0
    amm.prev_p_oracle_time = prices[0][0]
    amm.current_timestamp = prices[0][0]

    def target(price: float, timestamp: int, up: bool) -> float:
        scan = range(amm.max_band, amm.min_band - 1, -1) if up else range(amm.min_band, amm.max_band + 1)
        for band in scan:
            dynamic_fee = amm.dynamic_fee(band, timestamp=timestamp)
            boundary = amm.p_down(band) * (1 + dynamic_fee) if up else amm.p_up(band) * (1 - dynamic_fee)
            if (up and price > boundary) or (not up and price < boundary):
                return price * (1 - dynamic_fee if up else 1 + dynamic_fee)
        band = amm.min_band if up else amm.max_band
        dynamic_fee = amm.dynamic_fee(band, timestamp=timestamp)
        return price * (1 - dynamic_fee if up else 1 + dynamic_fee)

    for (timestamp, _open, high, low, _close, _volume), oracle in zip(prices, oracles):
        amm.set_p_oracle(oracle, timestamp=timestamp)
        high_target = target(high * (1 - external_fee), timestamp, True)
        low_target = target(low * (1 + external_fee), timestamp, False)
        if high_target > amm.get_p():
            amm.trade_to_price(high_target)
        if low_target < amm.get_p():
            amm.trade_to_price(low_target)
    return 1 - amm.get_all_x() / initial_value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", type=Path, default=DEFAULT_MARKET)
    parser.add_argument("--aggregate", type=Path, default=DEFAULT_AGGREGATE)
    parser.add_argument(
        "--direct-usd-market",
        action="store_true",
        help="treat --market as USD/ZCHF directly and do not load --aggregate",
    )
    parser.add_argument("--A", nargs="+", type=int, default=[122, 155, 180])
    parser.add_argument("--fee", nargs="+", type=float, default=[0.00076, 0.0015, 0.00171])
    parser.add_argument("--samples", type=int, default=20_000)
    parser.add_argument(
        "--all-windows",
        action="store_true",
        help="use every eligible complete window instead of random sampling",
    )
    parser.add_argument(
        "--window-stride-minutes",
        type=int,
        default=1,
        help="start spacing with --all-windows; default: 1",
    )
    parser.add_argument("--bands", type=int, default=4)
    parser.add_argument("--ema-half-life", type=int, default=3603)
    parser.add_argument(
        "--duration-days",
        nargs="+",
        type=float,
        default=[1.0, 3.0],
        help="fixed window durations to sweep; default: 1 3",
    )
    parser.add_argument("--external-fee", type=float, default=0.0005)
    parser.add_argument("--dynamic-fee-multiplier", type=float, default=0.25)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--processes", type=int, default=None)
    parser.add_argument("--top-samples", type=int, default=50)
    parser.add_argument("--output", type=Path, default=Path("results/ZCHF_CRVUSD/parameter_sweep.csv"))
    return parser.parse_args()


def main() -> int:
    global MARKET, ORACLE
    args = parse_args()
    if args.samples <= 0 or args.bands <= 0 or args.top_samples <= 0:
        raise SystemExit("samples, bands, and top-samples must be positive")
    if args.window_stride_minutes <= 0:
        raise SystemExit("window-stride-minutes must be positive")
    if any(not math.isfinite(days) or days <= 0 for days in args.duration_days):
        raise SystemExit("every duration must be finite and positive")
    if args.ema_half_life < 0 or not 0 <= args.external_fee < 1 or args.dynamic_fee_multiplier < 0:
        raise SystemExit("invalid EMA or fee setting")
    MARKET = load_market(args.market)
    aggregate = [1.0] * len(MARKET) if args.direct_usd_market else load_aggregate(args.aggregate, MARKET)
    ORACLE = build_oracle(MARKET, aggregate, args.ema_half_life)
    warmup = math.ceil(10 * args.ema_half_life / 60)
    rng = random.Random(args.seed)
    durations = list(dict.fromkeys(args.duration_days))
    windows_by_duration: dict[float, list[tuple[int, int]]] = {}
    for days in durations:
        length = math.ceil(days * 1_440)
        latest_start = len(MARKET) - length
        if latest_start < warmup:
            raise SystemExit(f"duration {days:g} days does not fit the market history")
        # Select independently by duration, but reuse these exact windows for
        # every A/fee combination within that duration.
        windows_by_duration[days] = select_windows(
            warmup,
            latest_start,
            length,
            args.samples,
            rng,
            args.all_windows,
            args.window_stride_minutes,
        )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "A",
        "fee",
        "duration_days",
        "samples",
        "bands",
        "dynamic_fee_multiplier",
        "mean_loss",
        "median_loss",
        "max_loss",
        "worst_n_mean_loss",
        "worst_n",
        "seed",
        "es99_samples",
        "es99_loss",
        "es99_75_samples",
        "es99_75_loss",
        "all_windows",
        "window_stride_minutes",
    ]
    total = len(args.A) * len(args.fee) * len(durations)
    work_by_duration = {days: len(windows) * windows[0][1] for days, windows in windows_by_duration.items()}
    total_work = len(args.A) * len(args.fee) * sum(work_by_duration.values())
    completed_work = 0
    completed_simulations = 0
    started_at = monotonic()
    with args.output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        with ProcessPoolExecutor(max_workers=args.processes) as pool:
            combination = 0
            for days in durations:
                windows = windows_by_duration[days]
                for A in args.A:
                    for fee in args.fee:
                        combination += 1
                        selection = (
                            f"all windows, stride={args.window_stride_minutes}m" if args.all_windows else "random"
                        )
                        print(
                            f"[{combination}/{total}] duration={days:g}d, "
                            f"A={A}, fee={fee:.6g}, samples={len(windows):,}, "
                            f"selection={selection}",
                            file=sys.stderr,
                            flush=True,
                        )
                        tasks = (
                            (
                                A,
                                fee,
                                start,
                                length,
                                args.bands,
                                args.external_fee,
                                args.dynamic_fee_multiplier,
                            )
                            for start, length in windows
                        )
                        losses = list(pool.map(simulate, tasks, chunksize=100))
                        summary = summarize_losses(losses, args.top_samples)
                        writer.writerow(
                            {
                                "A": A,
                                "fee": fee,
                                "duration_days": days,
                                "samples": len(losses),
                                "bands": args.bands,
                                "dynamic_fee_multiplier": args.dynamic_fee_multiplier,
                                "mean_loss": summary["mean_loss"],
                                "median_loss": summary["median_loss"],
                                "max_loss": summary["max_loss"],
                                "worst_n_mean_loss": summary["worst_n_mean_loss"],
                                "worst_n": summary["worst_n"],
                                "seed": args.seed,
                                "es99_samples": summary["es99_samples"],
                                "es99_loss": summary["es99_loss"],
                                "es99_75_samples": summary["es99_75_samples"],
                                "es99_75_loss": summary["es99_75_loss"],
                                "all_windows": int(args.all_windows),
                                "window_stride_minutes": (args.window_stride_minutes if args.all_windows else 0),
                            }
                        )
                        stream.flush()
                        completed_simulations += len(losses)
                        completed_work += work_by_duration[days]
                        elapsed = monotonic() - started_at
                        work_rate = completed_work / elapsed
                        eta = (total_work - completed_work) / work_rate
                        print(
                            f"[{combination}/{total}] complete; "
                            f"elapsed={format_duration(elapsed)}; "
                            f"ETA={format_duration(eta)}",
                            file=sys.stderr,
                            flush=True,
                        )
    elapsed = monotonic() - started_at
    print(
        f"progress: 100.00%; {completed_simulations:,} simulations complete; "
        f"elapsed={format_duration(elapsed)}; ETA=0s",
        file=sys.stderr,
    )
    print(f"wrote {args.output} at {datetime.now(UTC).isoformat()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
