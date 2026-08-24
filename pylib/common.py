"""Shared RPC + ABI helpers for LLamaLendSimV1."""
from __future__ import annotations
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# .env / RPC endpoint
# ---------------------------------------------------------------------------
# Reuse the same .env the TS project uses so we don't duplicate the RPC URL.
_TS_ENV = Path(__file__).resolve().parent.parent / ".env"
def rpc_url() -> str:
    env = os.environ.get("WEB3_HTTP_MAINNET")
    if env:
        return env
    if _TS_ENV.exists():
        for line in _TS_ENV.read_text().splitlines():
            if line.startswith("WEB3_HTTP_MAINNET="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("WEB3_HTTP_MAINNET not set and no .env found")


# ---------------------------------------------------------------------------
# JSON-RPC
# ---------------------------------------------------------------------------
def rpc_call(method: str, params: list[Any], retries: int = 5) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                rpc_url(), data=body, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=45) as r:
                resp = json.loads(r.read())
            if "error" in resp:
                raise RuntimeError(f"RPC error: {resp['error']}")
            return resp["result"]
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(0.3 * (attempt + 1))


def hex_int(x: str | int) -> int:
    if isinstance(x, int):
        return x
    return int(x, 16)


# ---------------------------------------------------------------------------
# Solidity selectors + call helpers
# ---------------------------------------------------------------------------
# We hand-roll the essential selectors we need. All numeric returns are 32-byte hex.
SELECTORS: dict[str, str] = {
    # AMM
    "A()":            "0xf446c1d0",
    "fee()":          "0xddca3f43",
    "admin_fee()":    "0x2a7dd7cd",
    "rate()":         "0x2c4e722e",
    "active_band()":  "0x8a76dcf4",
    "min_band()":     "0x00fee1ff",  # placeholder — will look up if needed
    "max_band()":     "0x35b6b62c",  # placeholder
    "old_p_o()":      "0x77d90f6b",  # placeholder
    "old_dfee()":     "0x0f7c2c65",  # placeholder
    "prev_p_o_time()":"0xfe9f8f47",  # placeholder
    "admin_fees_x()": "0x33b3ff2c",  # placeholder
    "admin_fees_y()": "0x25ac0a35",  # placeholder
    "price_oracle()": "0x86fbf193",  # returns limited price
    "get_base_price()": "0xb84389be",  # placeholder — will compute selector below
    "get_rate_mul()":   "0x3e6b675a",  # placeholder
    # Bands
    "bands_x(int256)": "0xed5c7477",  # placeholder — 4-byte selector of keccak256("bands_x(int256)")[:4]
    "bands_y(int256)": "0x89f5aa74",  # placeholder
    "total_shares(int256)": "0x9c1c2fa1",  # placeholder
    # User
    "read_user_tick_numbers(address)": "0xfec3c866",  # placeholder
    "get_sum_xy(address)": "0xaea70dcc",  # placeholder
    "get_x_down(address)": "0x63f81aa3",  # placeholder
    # Controller (users iteration)
    "n_loans()": "0x6ce7f6ff",
    "loans(uint256)": "0x2b9a2dd0",  # placeholder
    "user_state(address)": "0x37cb92ab",  # placeholder
}
# NOTE: several selectors above are placeholders. common.py exports a helper
# that computes selectors from a signature so we don't rely on the table.

try:
    from Crypto.Hash import keccak  # pycryptodome (may or may not be installed)

    def sel(sig: str) -> str:
        k = keccak.new(digest_bits=256)
        k.update(sig.encode())
        return "0x" + k.hexdigest()[:8]
except ImportError:
    # Fallback: allow selectors from the SELECTORS dict only.
    def sel(sig: str) -> str:
        if sig in SELECTORS and SELECTORS[sig] not in ("", "0x"):
            return SELECTORS[sig]
        raise RuntimeError(
            f"pycryptodome not installed and no baked selector for {sig!r}."
            " Install: pip install pycryptodome"
        )


def eth_call(to: str, data: str, block: int | str = "latest") -> str:
    if isinstance(block, int):
        block = hex(block)
    return rpc_call("eth_call", [{"to": to, "data": data}, block])


def eth_get_storage_at(address: str, slot: int, block: int | str = "latest") -> int:
    """Read a raw 32-byte storage slot."""
    if isinstance(block, int):
        block = hex(block)
    r = rpc_call("eth_getStorageAt", [address, hex(slot), block])
    return int(r, 16)


def eth_get_storage_at_signed(address: str, slot: int, block: int | str = "latest") -> int:
    x = eth_get_storage_at(address, slot, block)
    if x >= (1 << 255):
        x -= 1 << 256
    return x


# ---------------------------------------------------------------------------
# Solidity/Vyper mapping-slot derivation
# ---------------------------------------------------------------------------
def keccak256(b: bytes) -> bytes:
    from Crypto.Hash import keccak
    k = keccak.new(digest_bits=256)
    k.update(b)
    return k.digest()


def _pad32(x: int, signed: bool = False) -> bytes:
    if signed and x < 0:
        x = x + (1 << 256)
    return x.to_bytes(32, "big")


def _pad32_addr(addr: str) -> bytes:
    a = addr.lower().removeprefix("0x")
    a = a.rjust(64, "0")
    return bytes.fromhex(a)


def map_slot_int(base_slot: int, key: int) -> int:
    """slot for HashMap[int256, uint256] at base_slot for key `key`.

    Vyper 0.3.10 hashes SLOT then KEY (opposite of Solidity's KEY then SLOT).
    """
    return int.from_bytes(keccak256(_pad32(base_slot) + _pad32(key, signed=True)), "big")


def map_slot_addr(base_slot: int, key: str) -> int:
    """slot for HashMap[address, ...] at base_slot for key `key`. Vyper order."""
    return int.from_bytes(keccak256(_pad32(base_slot) + _pad32_addr(key)), "big")


def encode_int(x: int, signed: bool = False) -> str:
    # Any negative integer must be encoded as two's complement — regardless of
    # what the caller declared, sign-magnitude hex would be an invalid RPC arg.
    if x < 0:
        x = x + (1 << 256)
    return f"{x:064x}"


def encode_addr(a: str) -> str:
    a = a.lower().removeprefix("0x")
    return "0" * (64 - len(a)) + a


def call_uint256(to: str, sig: str, args: tuple = (), block: int | str = "latest") -> int:
    data = sel(sig) + "".join(
        encode_int(a) if isinstance(a, int) else encode_addr(a) for a in args
    )
    res = eth_call(to, data, block)
    if not res or res == "0x":
        raise RuntimeError(f"eth_call({sig}, {args}) returned empty at block {block}")
    return int(res, 16)


def call_int256(to: str, sig: str, args: tuple = (), block: int | str = "latest") -> int:
    x = call_uint256(to, sig, args, block)
    # sign-extend
    if x >= (1 << 255):
        x -= 1 << 256
    return x


def call_two_int256(to: str, sig: str, args: tuple = (), block: int | str = "latest") -> tuple[int, int]:
    data = sel(sig) + "".join(
        encode_int(a) if isinstance(a, int) else encode_addr(a) for a in args
    )
    res = eth_call(to, data, block)
    hex_body = res[2:]
    hi = int(hex_body[:64], 16)
    lo = int(hex_body[64:128], 16)
    if hi >= (1 << 255):
        hi -= 1 << 256
    if lo >= (1 << 255):
        lo -= 1 << 256
    return hi, lo


def call_two_uint256(to: str, sig: str, args: tuple = (), block: int | str = "latest") -> tuple[int, int]:
    data = sel(sig) + "".join(
        encode_int(a) if isinstance(a, int) else encode_addr(a) for a in args
    )
    res = eth_call(to, data, block)
    hex_body = res[2:]
    return int(hex_body[:64], 16), int(hex_body[64:128], 16)


def call_addr_arr(to: str, sig: str, count: int, arg_maker, block: int | str = "latest") -> list[str]:
    out = []
    for i in range(count):
        data = sel(sig) + encode_int(arg_maker(i))
        res = eth_call(to, data, block)
        out.append("0x" + res[2:][24:64])
    return out


# ---------------------------------------------------------------------------
# Convenience: block info
# ---------------------------------------------------------------------------
def block_info(block: int) -> dict[str, int]:
    r = rpc_call("eth_getBlockByNumber", [hex(block), False])
    return {
        "number": hex_int(r["number"]),
        "timestamp": hex_int(r["timestamp"]),
        "baseFeePerGas": hex_int(r.get("baseFeePerGas", "0x0")),
    }


# ---------------------------------------------------------------------------
# Event decoding helpers (topics + data)
# ---------------------------------------------------------------------------
def get_logs(address: str, topics: list[str | None], from_block: int, to_block: int) -> list[dict]:
    return rpc_call(
        "eth_getLogs",
        [
            {
                "address": address,
                "fromBlock": hex(from_block),
                "toBlock": hex(to_block),
                "topics": topics,
            }
        ],
    )
