"""Fetch the FULL event history for AMM + Controller from deploy block to
some target block. Emits a single events.json used by the C++ replay driver
to reconstruct state at the target block starting from an empty deploy state.

AMM events (address = --amm):
  TokenExchange(buyer, sold_id, tokens_sold, bought_id, tokens_bought)
  Deposit(provider, amount, n1, n2)
  Withdraw(provider, amount_borrowed, amount_collateral)
  SetRate(rate, rate_mul, time)
  SetFee(fee)
  SetAdminFee(admin_fee)

Controller events (address = --controller):
  UserState(user, collateral, debt, N)  ← captures debt after every op
  Liquidate(liquidator, user, ...)      ← we also decode to know when a user is removed

We combine both event streams, sort by (block, log_index), and emit chronologically.
For each unique block with events, we also fetch external_price + timestamp.

Usage:
  python fetch_full_history.py --amm 0x… --controller 0x… \
      --price-oracle 0x… --from-block <deploy> --to-block <target> --out out.json
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from common import (
    block_info, call_uint256, get_logs, keccak256, rpc_call, hex_int,
)


def topic0(sig: str) -> str:
    return "0x" + keccak256(sig.encode()).hex()


AMM_TOPICS = {
    "TokenExchange": topic0("TokenExchange(address,uint256,uint256,uint256,uint256)"),
    "Deposit":       topic0("Deposit(address,uint256,int256,int256)"),
    "Withdraw":      topic0("Withdraw(address,uint256,uint256)"),
    "SetRate":       topic0("SetRate(uint256,uint256,uint256)"),
    "SetFee":        topic0("SetFee(uint256)"),
    "SetAdminFee":   topic0("SetAdminFee(uint256)"),
}
CTRL_TOPICS = {
    # Correct sig per controllerCRVLong.py:
    # UserState(user indexed, collateral, debt, n1, n2, liquidation_discount)
    "UserState": topic0("UserState(address,uint256,uint256,int256,int256,uint256)"),
}


def dec_addr(w: str) -> str:
    return "0x" + w[-40:].lower()


def dec_int256(w: str) -> int:
    x = int(w, 16)
    if x >= (1 << 255): x -= 1 << 256
    return x


def parse(kind: str, log: dict) -> dict:
    blk = hex_int(log["blockNumber"])
    idx = hex_int(log["logIndex"])
    topics = log["topics"]
    data = log["data"][2:]

    def word(i): return data[i * 64 : (i + 1) * 64]

    common_ = {"kind": kind, "block": blk, "log_index": idx, "tx_hash": log["transactionHash"]}

    if kind == "TokenExchange":
        common_.update({"buyer": dec_addr(topics[1]),
                       "i": int(word(0), 16), "sold": str(int(word(1), 16)),
                       "j": int(word(2), 16), "bought": str(int(word(3), 16))})
    elif kind == "Deposit":
        common_.update({"user": dec_addr(topics[1]), "amount": str(int(word(0), 16)),
                       "n1": dec_int256(word(1)), "n2": dec_int256(word(2))})
    elif kind == "Withdraw":
        common_.update({"user": dec_addr(topics[1]),
                       "amount_borrowed": str(int(word(0), 16)),
                       "amount_collateral": str(int(word(1), 16))})
    elif kind == "SetRate":
        common_.update({"rate": str(int(word(0), 16)),
                       "rate_mul": str(int(word(1), 16)),
                       "time": str(int(word(2), 16))})
    elif kind == "SetFee":
        common_.update({"fee": str(int(word(0), 16))})
    elif kind == "SetAdminFee":
        common_.update({"admin_fee": str(int(word(0), 16))})
    elif kind == "UserState":
        common_.update({
            "user": dec_addr(topics[1]),
            "collateral": str(int(word(0), 16)),
            "debt":       str(int(word(1), 16)),
            "n1":         dec_int256(word(2)),
            "n2":         dec_int256(word(3)),
            "liquidation_discount": str(int(word(4), 16)),
        })
    return common_


def fetch_chunked_logs(address: str, topics_or: list[str], from_block: int, to_block: int,
                       chunk_size: int = 100_000, label: str = "", max_workers: int = 30) -> list[dict]:
    """eth_getLogs with chunking + concurrent chunk fetching (capped at
    max_workers to stay under Dwellir's per-key rate limit).

    topics_or is a list of topic0 hex strings — eth_getLogs matches any of them
    in a single query, so we can pull ALL of an address's event types in one
    round-trip per chunk (6× speedup vs a separate query per topic).
    Chunks are fetched in parallel via a ThreadPoolExecutor. Each worker
    handles range-too-wide errors by narrowing its own chunk internally.
    """
    import time as _t
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # Pre-compute chunk ranges
    ranges: list[tuple[int, int]] = []
    cur = from_block
    while cur <= to_block:
        end = min(cur + chunk_size - 1, to_block)
        ranges.append((cur, end))
        cur = end + 1

    def fetch_one(rng: tuple[int, int]) -> list[dict]:
        cur, end = rng
        my_size = end - cur + 1
        # local halving on range-too-wide errors
        while True:
            try:
                return rpc_call("eth_getLogs", [{
                    "address": address,
                    "fromBlock": hex(cur),
                    "toBlock": hex(end),
                    "topics": [topics_or],
                }])
            except Exception as e:
                msg = str(e)
                if my_size > 5_000 and ("Log response size" in msg or "range" in msg or "429" in msg):
                    my_size = max(5_000, my_size // 2)
                    end = cur + my_size - 1
                    continue
                raise

    out: list[dict] = []
    completed = 0
    total = len(ranges)
    t0 = _t.time()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(fetch_one, r) for r in ranges]
        for fut in as_completed(futures):
            logs = fut.result()
            out.extend(logs)
            completed += 1
            if completed % 5 == 0 or completed == total:
                print(f"  [{label}] chunk {completed}/{total}  "
                      f"logs_so_far={len(out)}  elapsed={_t.time()-t0:.0f}s", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--amm", required=True)
    ap.add_argument("--controller", required=True)
    ap.add_argument("--price-oracle", required=True)
    ap.add_argument("--from-block", type=int, required=True)
    ap.add_argument("--to-block", type=int, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--chunk", type=int, default=100_000)
    args = ap.parse_args()

    t0 = time.time()
    events: list[dict] = []

    # ALL AMM topics in a single OR-filter, one round-trip per chunk (~6× faster).
    amm_topic_to_kind = {v: k for k, v in AMM_TOPICS.items()}
    tk = time.time()
    raw_amm = fetch_chunked_logs(
        args.amm, list(AMM_TOPICS.values()),
        args.from_block, args.to_block, args.chunk, label="AMM.*")
    print(f"[full-history] AMM.* total: {len(raw_amm)} events  ({time.time()-tk:.1f}s)", flush=True)
    for log in raw_amm:
        kind = amm_topic_to_kind.get(log["topics"][0])
        if kind: events.append(parse(kind, log))

    ctrl_topic_to_kind = {v: k for k, v in CTRL_TOPICS.items()}
    tk = time.time()
    raw_ctrl = fetch_chunked_logs(
        args.controller, list(CTRL_TOPICS.values()),
        args.from_block, args.to_block, args.chunk, label="Ctrl.*")
    print(f"[full-history] Ctrl.* total: {len(raw_ctrl)} events  ({time.time()-tk:.1f}s)", flush=True)
    for log in raw_ctrl:
        kind = ctrl_topic_to_kind.get(log["topics"][0])
        if kind: events.append(parse(kind, log))

    events.sort(key=lambda e: (int(e["block"]), int(e["log_index"])))
    print(f"[full-history] fetched {len(events)} events; adding external_price+ts per unique block...",
          flush=True)

    # External price is only READ by the AMM inside _price_oracle_w which is
    # only called from _exchange (via TokenExchange). Fetching it for every
    # event block wastes RPC on Deposit/Withdraw/UserState blocks. So we only
    # fetch it for UNIQUE TokenExchange blocks.
    # For non-swap events we still need the timestamp (block_info) so replay
    # can set block_timestamp before applying the event, but that's just one
    # cheap call per unique block.
    tk = time.time()
    swap_blocks = {int(e["block"]) for e in events if e["kind"] == "TokenExchange"}
    other_blocks = {int(e["block"]) for e in events if e["kind"] != "TokenExchange"}
    print(f"[full-history] unique blocks: {len(swap_blocks)} swap, "
          f"{len(other_blocks - swap_blocks)} non-swap", flush=True)

    # Only real fetches: (external_price + real ts) for swap blocks.
    # For non-swap blocks: ts = deploy_ts + (block - deploy_block) * SEC_PER_BLOCK.
    # Non-swap events don't consume ts in the replay engine, so estimation is
    # safe. Real ts is only needed at swap blocks because tick_oracle reads it.
    SEC_PER_BLOCK = 12
    deploy_info = block_info(args.from_block)
    deploy_ts_val = deploy_info["timestamp"]

    ep_cache: dict[int, int] = {}
    ts_cache: dict[int, int] = {}
    swap_blocks_sorted = sorted(swap_blocks)

    # 28k swap blocks × 2 sequential RPCs = ~2 hours. Fan out with a thread
    # pool: 24 workers × ~200ms per call cuts wall clock to ~4-5 min.
    from concurrent.futures import ThreadPoolExecutor
    def fetch_one(b: int) -> tuple[int, int, int]:
        ep = call_uint256(args.price_oracle, "price()", block=b)
        ts = block_info(b)["timestamp"]
        return b, ep, ts

    with ThreadPoolExecutor(max_workers=24) as pool:
        for i, (b, ep, ts) in enumerate(pool.map(fetch_one, swap_blocks_sorted)):
            ep_cache[b] = ep
            ts_cache[b] = ts
            if (i + 1) % 500 == 0:
                print(f"  swap-block {i+1}/{len(swap_blocks_sorted)}  elapsed={time.time()-tk:.0f}s",
                      flush=True)

    for e in events:
        b = int(e["block"])
        if b in ts_cache:
            e["ts"] = ts_cache[b]
        else:
            e["ts"] = deploy_ts_val + (b - args.from_block) * SEC_PER_BLOCK
        e["external_price"] = str(ep_cache.get(b, 0))
    print(f"[full-history] price/ts done for {len(swap_blocks)} swap blocks  ({time.time()-tk:.1f}s)")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(events))
    counts: dict[str, int] = {}
    for e in events: counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    print(f"[full-history] wrote {args.out}  events={len(events)}  {counts}  ({time.time()-t0:.1f}s total)")


if __name__ == "__main__":
    main()
