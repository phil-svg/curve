"""Produce a MINIMAL "empty state" snapshot at the AMM's deploy block.

The AMM's constructor sets:
  self.prev_p_o_time = block.timestamp
  self.old_p_o       = price_oracle_contract.price()
  self.rate_mul      = 10**18
  self.old_dfee      = 0
  self.fee, self.admin_fee — passed as constructor args
  everything else defaults to 0 (bands, users, admin_fees_x/y, min/max/active_band)

This lets the C++ replayer start from a blank slate and rebuild everything
by replaying events. Way faster than fetching populated state at any later block.

Usage:
  python fetch_deploy_state.py --controller 0x… --amm 0x… --deploy-block N \
                               --out data/snapshots/deploy_state.json
"""
from __future__ import annotations
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from common import block_info, call_uint256, eth_get_storage_at
from fetch_snapshot import (
    AMM_SLOTS,
    _theoretical_log_a_ratio,
    _mpmath_sqrt_band_ratio,
    _mpmath_max_oracle_dn_pow,
    _autotune_log_a_ratio,
)

REPO = Path(__file__).resolve().parent.parent


def fetch_immutables_at(amm: str, block: int) -> dict:
    """Same idea as fetch_snapshot.fetch_immutables but at deploy block; there
    rate_mul=1e18 exactly so BASE_PRICE derivation is trivial."""
    A = call_uint256(amm, "A()", block=block)
    # At deploy block: rate_mul_stored=1e18, dt=0 → rate_mul_now = 1e18
    # So BASE_PRICE == get_base_price()
    BASE_PRICE = call_uint256(amm, "get_base_price()", block=block)
    # Autotune LOG_A_RATIO against a MID-LIFE block (not deploy) so any
    # rate_mul>1e18 amplifies the base_price signal — the exp polynomial's
    # rounding jitter at 1e18 base can flip the derived LOG_A by 1 unit at
    # deploy but resolves cleanly at higher base prices. We pick the block
    # where we already verified byte-exact match on p_oracle_up.
    _canonical_block = 23549898
    _canonical_bp = call_uint256(amm, "get_base_price()", block=_canonical_block)
    seed = _theoretical_log_a_ratio(A)
    LOG_A_RATIO = _autotune_log_a_ratio(amm, _canonical_block, _canonical_bp, seed)
    return {
        "A": A,
        "Aminus1": A - 1,
        "A2": A * A,
        "Aminus12": (A - 1) ** 2,
        "BORROWED_PRECISION": 1,
        "COLLATERAL_PRECISION": 1,
        "BASE_PRICE": BASE_PRICE,
        "SQRT_BAND_RATIO": _mpmath_sqrt_band_ratio(A),
        "LOG_A_RATIO": LOG_A_RATIO,
        "MAX_ORACLE_DN_POW": _mpmath_max_oracle_dn_pow(A),
    }


def fetch_deploy_state(amm: str, price_oracle_contract: str, deploy_block: int) -> dict:
    ts = block_info(deploy_block)["timestamp"]
    ONE = 10 ** 18
    fee = call_uint256(amm, "fee()", block=deploy_block)
    admin_fee = call_uint256(amm, "admin_fee()", block=deploy_block)
    # These come from the price_oracle_contract itself at deploy time:
    old_p_o = call_uint256(price_oracle_contract, "price()", block=deploy_block)
    return {
        "fee": fee,
        "admin_fee": admin_fee,
        "rate": 0,             # rate defaults to 0 (Controller sets it later via set_rate)
        "rate_time": ts,
        "rate_mul": ONE,
        "active_band": 0,
        "min_band": 0,
        "max_band": 0,
        "admin_fees_x": 0,
        "admin_fees_y": 0,
        "old_p_o": old_p_o,
        "old_dfee": 0,
        "prev_p_o_time": ts,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--controller", required=True)
    ap.add_argument("--amm",        required=True)
    ap.add_argument("--deploy-block", type=int, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    t0 = time.time()
    print(f"[deploy] fetching immutables + deploy-time state at block {args.deploy_block}...", flush=True)
    imm = fetch_immutables_at(args.amm, args.deploy_block)
    poc_int = call_uint256(args.amm, "price_oracle_contract()", block=args.deploy_block)
    poc = "0x" + f"{poc_int:040x}"
    st = fetch_deploy_state(args.amm, poc, args.deploy_block)
    info = block_info(args.deploy_block)

    out = {
        "market": {"controller": args.controller, "amm": args.amm,
                   "price_oracle_contract": poc},
        "block": str(args.deploy_block),
        "timestamp": str(info["timestamp"]),
        "immutables": {k: str(v) for k, v in imm.items()},
        "state": {k: str(v) for k, v in st.items()},
        "bands": {},                   # empty at deploy
        "users": {},                   # empty at deploy
        "external_oracle_price": str(st["old_p_o"]),
        "timings_sec": {"total": round(time.time() - t0, 3)},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2))
    print(f"[deploy] wrote {args.out}  ({time.time()-t0:.1f}s)")


if __name__ == "__main__":
    main()
