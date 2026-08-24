"""Fetch AMM events (TokenExchange, Deposit, Withdraw, SetRate, SetFee, SetAdminFee)
plus per-block external oracle price for [from_block+1, to_block]. Emits a
single events.json used by the C++ replay driver.

Event format on disk:
  [
    {"kind": "SetRate",     "block": N, "ts": T, "rate": "…", "rate_mul": "…"},
    {"kind": "Deposit",     "block": N, "ts": T, "user": "0x…", "amount": "…", "n1": …, "n2": …},
    {"kind": "TokenExchange","block": N, "ts": T, "buyer": "0x…", "i": …, "sold": "…", "j": …, "bought": "…",
                              "external_price": "…"},
    {"kind": "Withdraw",    "block": N, "ts": T, "user": "0x…", "amount_borrowed": "…", "amount_collateral": "…"},
    ...
  ]

We inject `external_price` per event since the C++ replay needs the raw
price_oracle_contract.price() reading to feed limit_p_o. For non-TokenExchange
events (which invoke _price_oracle_w indirectly? — no, only exchange does),
we could skip; but we always record it to make the C++ side trivial.
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from common import (
    block_info,
    call_uint256,
    get_logs,
    keccak256,
    rpc_call,
    hex_int,
)

REPO = Path(__file__).resolve().parent.parent
EV_DIR = REPO / "data" / "events"
EV_DIR.mkdir(parents=True, exist_ok=True)


def topic0(sig: str) -> str:
    return "0x" + keccak256(sig.encode()).hex()


TOPICS = {
    "TokenExchange": topic0("TokenExchange(address,uint256,uint256,uint256,uint256)"),
    "Deposit":       topic0("Deposit(address,uint256,int256,int256)"),
    "Withdraw":      topic0("Withdraw(address,uint256,uint256)"),
    "SetRate":       topic0("SetRate(uint256,uint256,uint256)"),
    "SetFee":        topic0("SetFee(uint256)"),
    "SetAdminFee":   topic0("SetAdminFee(uint256)"),
}


def dec_addr(topic_or_data_slot: str) -> str:
    return "0x" + topic_or_data_slot[-40:].lower()


def dec_int256(hex_word: str) -> int:
    x = int(hex_word, 16)
    if x >= (1 << 255): x -= 1 << 256
    return x


def parse_event(kind: str, log: dict) -> dict:
    blk = hex_int(log["blockNumber"])
    topics = log["topics"]
    data = log["data"][2:]  # strip 0x

    # data is a hex string of consecutive 32-byte words
    def word(i):
        return data[i * 64 : (i + 1) * 64]

    if kind == "TokenExchange":
        # buyer indexed, then (sold_id, tokens_sold, bought_id, tokens_bought) in data
        buyer = dec_addr(topics[1])
        sold_id = int(word(0), 16)
        tokens_sold = int(word(1), 16)
        bought_id = int(word(2), 16)
        tokens_bought = int(word(3), 16)
        return {
            "kind": "TokenExchange",
            "block": blk,
            "log_index": hex_int(log["logIndex"]),
            "tx_hash": log["transactionHash"],
            "buyer": buyer,
            "i": sold_id,
            "sold": str(tokens_sold),
            "j": bought_id,
            "bought": str(tokens_bought),
        }
    if kind == "Deposit":
        provider = dec_addr(topics[1])
        amount = int(word(0), 16)
        n1 = dec_int256(word(1))
        n2 = dec_int256(word(2))
        return {
            "kind": "Deposit",
            "block": blk,
            "log_index": hex_int(log["logIndex"]),
            "tx_hash": log["transactionHash"],
            "user": provider,
            "amount": str(amount),
            "n1": n1,
            "n2": n2,
        }
    if kind == "Withdraw":
        provider = dec_addr(topics[1])
        return {
            "kind": "Withdraw",
            "block": blk,
            "log_index": hex_int(log["logIndex"]),
            "tx_hash": log["transactionHash"],
            "user": provider,
            "amount_borrowed": str(int(word(0), 16)),
            "amount_collateral": str(int(word(1), 16)),
        }
    if kind == "SetRate":
        return {
            "kind": "SetRate",
            "block": blk,
            "log_index": hex_int(log["logIndex"]),
            "rate": str(int(word(0), 16)),
            "rate_mul": str(int(word(1), 16)),
            "time": str(int(word(2), 16)),
        }
    if kind == "SetFee":
        return {"kind": "SetFee", "block": blk, "log_index": hex_int(log["logIndex"]),
                "fee": str(int(word(0), 16))}
    if kind == "SetAdminFee":
        return {"kind": "SetAdminFee", "block": blk, "log_index": hex_int(log["logIndex"]),
                "admin_fee": str(int(word(0), 16))}
    raise RuntimeError(f"unknown kind {kind}")


def fetch_range(amm: str, price_oracle_contract: str, from_block: int, to_block: int) -> list[dict]:
    events: list[dict] = []
    for kind, t in TOPICS.items():
        print(f"[events] fetching {kind}…", flush=True)
        logs = get_logs(amm, [t], from_block, to_block)
        for log in logs:
            events.append(parse_event(kind, log))
    events.sort(key=lambda e: (int(e["block"]), int(e["log_index"])))

    from common import block_info as _bi

    # Fetch external price + timestamp for EVERY block in [from, to]. The AMM's
    # `_price_oracle_ro` re-reads price_oracle_contract.price() on every call,
    # so its result changes between swap events. To keep the C++ replay's
    # health-scan bit-exact vs on-chain, we inject per-block synthetic
    # "BlockTick" events which set external_price + block_timestamp before any
    # real event on that block.
    print(f"[events] fetching per-block external price + timestamp for {to_block - from_block + 1} blocks…",
          flush=True)
    per_block: list[dict] = []
    for b in range(from_block, to_block + 1):
        ep = call_uint256(price_oracle_contract, "price()", block=b)
        ts = _bi(b)["timestamp"]
        per_block.append({
            "kind": "BlockTick",
            "block": b,
            "log_index": -1,  # sort before all real events at that block
            "external_price": str(ep),
            "ts": ts,
        })
        if (b - from_block + 1) % 20 == 0:
            print(f"  block {b - from_block + 1}/{to_block - from_block + 1}", flush=True)

    # Also stamp external_price on each real event (for backward compat)
    ep_by_block = {int(t["block"]): t["external_price"] for t in per_block}
    ts_by_block = {int(t["block"]): t["ts"] for t in per_block}
    for e in events:
        b = int(e["block"])
        e["external_price"] = ep_by_block[b]
        e["ts"] = ts_by_block[b]

    all_events = per_block + events
    all_events.sort(key=lambda e: (int(e["block"]), int(e["log_index"])))
    return all_events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--amm", required=True)
    ap.add_argument("--price-oracle", required=True)
    ap.add_argument("--from-block", type=int, required=True)
    ap.add_argument("--to-block", type=int, required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    t0 = time.time()
    events = fetch_range(args.amm, args.price_oracle, args.from_block, args.to_block)
    dt = time.time() - t0

    out_path = args.out or (
        EV_DIR / f"amm_{args.amm.lower()}_{args.from_block}_{args.to_block}.json"
    )
    out_path.write_text(json.dumps(events, indent=2))
    counts = {}
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    print(f"[events] {sum(counts.values())} events in {dt:.1f}s  {counts}  -> {out_path}")


if __name__ == "__main__":
    main()
