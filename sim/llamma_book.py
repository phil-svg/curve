"""llamma_book.py — Python port of the C++ LLAMMA engine (llama_amm.cpp) for
the SYNTHETIC single-borrower book.

Why this exists: the venue-routed soft-liquidation arb must quote LLAMMA and
the venue pool in the same process — the arb's size is bounded by venue depth,
so "push LLAMMA to the schedule price" (the C++ sweep's model) is replaced by
"equilibrate LLAMMA against the venue, profitably". The venue engines live in
Python (venues.py), so the LLAMMA book comes to Python too.

Faithfulness: line-for-line port of llama_amm.cpp, which is itself a
line-for-line port of Curve's amm.py (Vyper 0.3.10). All arithmetic is Python
int; every C++ u256 division here operates on non-negatives, where `//` is
identical. solmate_exp is reused from single_user.py (exact, EVM idiv
semantics). Verified wei-exact against the C++ binary by replaying identical
trade scripts — see verify_book_port.py.

Scope deliberately excluded: deposit_range (books are built by make_snapshot),
calc_swap_in (no exchange_dy in the synthetic flow), and event replay.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from single_user import solmate_exp

ONE = 10 ** 18
ONE36 = 10 ** 36
DEAD_SHARES = 1000
MAX_TICKS = 50
MAX_SKIP_TICKS = 1024
PREV_P_O_DELAY = 2 * 60
MAX_P_O_CHG = 1_250_000_000_000_000_000  # 1.25e18


class DetailedTrade:
    __slots__ = ("in_amount", "out_amount", "n1", "n2", "ticks_in",
                 "last_tick_j", "admin_fee")

    def __init__(self):
        self.in_amount = 0
        self.out_amount = 0
        self.n1 = 0
        self.n2 = 0
        self.ticks_in: list[int] = []
        self.last_tick_j = 0
        self.admin_fee = 0


class Book:
    """LLAMMA immutables + mutable state, loaded from the make_snapshot JSON
    (the same file the C++ sweep loads)."""

    def __init__(self, j: dict):
        im = j["immutables"]
        self.A = int(im["A"])
        self.Aminus1 = int(im["Aminus1"])
        self.A2 = int(im["A2"])
        self.Aminus12 = int(im["Aminus12"])
        self.BORROWED_PRECISION = int(im["BORROWED_PRECISION"])
        self.COLLATERAL_PRECISION = int(im["COLLATERAL_PRECISION"])
        self.BASE_PRICE = int(im["BASE_PRICE"])
        self.SQRT_BAND_RATIO = int(im["SQRT_BAND_RATIO"])
        self.LOG_A_RATIO = int(im["LOG_A_RATIO"])
        self.MAX_ORACLE_DN_POW = int(im["MAX_ORACLE_DN_POW"])
        st = j["state"]
        self.fee = int(st["fee"])
        self.admin_fee = int(st["admin_fee"])
        self.rate = int(st["rate"])
        self.rate_time = int(st["rate_time"])
        self.rate_mul = int(st["rate_mul"])
        self.active_band = int(st["active_band"])
        self.min_band = int(st["min_band"])
        self.max_band = int(st["max_band"])
        self.admin_fees_x = int(st["admin_fees_x"])
        self.admin_fees_y = int(st["admin_fees_y"])
        self.old_p_o = int(st["old_p_o"])
        self.old_dfee = int(st["old_dfee"])
        self.prev_p_o_time = int(st["prev_p_o_time"])
        self.block_timestamp = int(j["timestamp"])
        self.external_price = int(j["external_oracle_price"])
        # bands: n -> [x, y, shares]
        self.bands: dict[int, list[int]] = {
            int(k): [int(v["x"]), int(v["y"]), int(v["shares"])]
            for k, v in j["bands"].items()}
        # users: addr -> dict(ns0, ns1, shares{n:amt}, collateral, stablecoin, debt)
        self.users: dict[str, dict] = {}
        for addr, u in j["users"].items():
            self.users[addr] = {
                "ns0": int(u["ns0"]), "ns1": int(u["ns1"]),
                "shares": {int(k): int(v) for k, v in u.get("shares", {}).items()},
                "collateral": int(u.get("collateral", 0)),
                "stablecoin": int(u.get("stablecoin", 0)),
                "debt": int(u.get("debt", 0)),
            }

    @classmethod
    def load(cls, path: Path | str) -> "Book":
        return cls(json.loads(Path(path).read_text()))

    def clone(self) -> "Book":
        c = copy.copy(self)
        c.bands = {n: v[:] for n, v in self.bands.items()}
        c.users = {a: {**u, "shares": dict(u["shares"])} for a, u in self.users.items()}
        return c

    # ---- price basics ----------------------------------------------------
    def rate_mul_current(self) -> int:
        dt = self.block_timestamp - self.rate_time
        return self.rate_mul * (ONE + self.rate * dt) // ONE

    def base_price(self) -> int:
        return self.BASE_PRICE * self.rate_mul_current() // ONE

    def p_oracle_up(self, n: int) -> int:
        e = solmate_exp(-n * self.LOG_A_RATIO)
        return self.base_price() * e // ONE

    def limit_p_o(self, p: int) -> tuple[int, int]:
        p_new = p
        dt_raw = self.block_timestamp - self.prev_p_o_time
        dt = PREV_P_O_DELAY - min(PREV_P_O_DELAY, dt_raw)
        ratio = 0
        if dt > 0:
            old_p_o = self.old_p_o
            old_ratio = self.old_dfee
            if p > old_p_o:
                ratio = old_p_o * ONE // p
                floor_ = ONE36 // MAX_P_O_CHG
                if ratio < floor_:
                    p_new = old_p_o * MAX_P_O_CHG // ONE
                    ratio = floor_
            else:
                ratio = p * ONE // old_p_o
                floor_ = ONE36 // MAX_P_O_CHG
                if ratio < floor_:
                    p_new = old_p_o * ONE // MAX_P_O_CHG
                    ratio = floor_
            r3 = ratio * ratio * ratio // ONE36
            inner = (ONE + old_ratio) - r3
            ratio = min(inner * dt // PREV_P_O_DELAY, ONE - 1)
        return p_new, ratio

    def price_oracle_ro(self) -> tuple[int, int]:
        return self.limit_p_o(self.external_price)

    def tick_oracle(self) -> tuple[int, int]:
        p, dfee = self.limit_p_o(self.external_price)
        self.prev_p_o_time = self.block_timestamp
        self.old_p_o = p
        self.old_dfee = dfee
        return p, dfee

    def bypass_oracle_guardrail(self) -> None:
        """Disable limit_p_o's 1.25×/120s anti-manipulation clamp for the
        synthetic aggregate arb — mirrors arb_to_target_price's preamble in the
        C++ (the clamp protects against single-tx manipulation; our one trade
        stands for a block's worth of collective flow)."""
        if self.block_timestamp > 120:
            self.prev_p_o_time = self.block_timestamp - 120
            self.old_p_o = self.external_price
            self.old_dfee = 0

    # ---- band curve ------------------------------------------------------
    @staticmethod
    def _sqrt_int(x: int) -> int:
        if x == 0:
            return 0
        from math import isqrt
        return isqrt(x)

    def get_y0(self, x: int, y: int, p_o: int, p_o_up: int) -> int:
        if p_o == 0:
            raise ValueError("get_y0: p_o=0")
        b = 0
        if x != 0:
            b = p_o_up * self.Aminus1 * x // p_o
        if y != 0:
            b += self.A * (p_o * p_o) // p_o_up * y // ONE
        if x > 0 and y > 0:
            D = b * b + (4 * self.A * p_o * y // ONE) * x
            return (b + self._sqrt_int(D)) * ONE // (2 * self.A * p_o)
        return b * ONE // (self.A * p_o)

    def get_p_in_band(self, n: int, x: int, y: int) -> int:
        p_o_up = self.p_oracle_up(n)
        p_o = self.price_oracle_ro()[0]
        if p_o_up == 0:
            raise ValueError("get_p: p_o_up=0")
        if x == 0:
            if y == 0:
                return ((p_o * p_o) // p_o_up) * p_o // p_o_up * self.A // self.Aminus1
            return ((p_o * p_o) // p_o_up) * p_o // p_o_up
        if y == 0:
            p_o_down = p_o_up * self.Aminus1 // self.A
            return (p_o * p_o) // p_o_down * p_o // p_o_down
        y0 = self.get_y0(x, y, p_o, p_o_up)
        f = self.A * y0 * p_o // p_o_up * p_o
        g = self.Aminus1 * y0 * p_o_up // p_o
        return (f + x * ONE) // (g + y)

    def get_p(self) -> int:
        b = self.bands.get(self.active_band)
        x, y = (b[0], b[1]) if b else (0, 0)
        return self.get_p_in_band(self.active_band, x, y)

    def get_dynamic_fee(self, p_o: int, p_o_up: int) -> int:
        p_c_d = ((p_o * p_o) // p_o_up) * p_o // p_o_up
        p_c_u = p_c_d * self.A // self.Aminus1 * self.A // self.Aminus1
        quarter = ONE // 4
        if p_o < p_c_d:
            return (p_c_d - p_o) * quarter // p_c_d
        if p_o > p_c_u:
            return (p_o - p_c_u) * quarter // p_o
        return 0

    # ---- swap engine -----------------------------------------------------
    def calc_swap_out(self, pump: bool, in_amount: int, p_o: int, p_dfee: int,
                      in_precision: int, out_precision: int) -> DetailedTrade:
        out = DetailedTrade()
        out.n2 = self.active_band
        p_o_up = self.p_oracle_up(out.n2)
        b = self.bands.get(out.n2)
        x, y = (b[0], b[1]) if b else (0, 0)

        in_amount_left = in_amount
        fee = max(self.fee, p_dfee)
        admin_fee = self.admin_fee
        j = MAX_TICKS

        for i in range(MAX_TICKS + MAX_SKIP_TICKS):
            y0 = f = g = Inv = 0
            dyn_fee = fee
            if x > 0 or y > 0:
                if j == MAX_TICKS:
                    out.n1 = out.n2
                    j = 0
                y0 = self.get_y0(x, y, p_o, p_o_up)
                f = self.A * y0 * p_o // p_o_up * p_o // ONE
                g = self.Aminus1 * y0 * p_o_up // p_o
                Inv = (f + x) * (g + y)
                dyn_fee = max(self.get_dynamic_fee(p_o, p_o_up), fee)

            antifee = (ONE * ONE) // (ONE - min(dyn_fee, ONE - 1))

            if j != MAX_TICKS:
                out.ticks_in.append(x if pump else y)

            p_ratio = p_o_up * ONE // p_o

            if pump:
                if y != 0 and g != 0:
                    x_dest = (Inv // g - f) - x
                    dx = x_dest * antifee // ONE
                    if dx >= in_amount_left:
                        x_dest = in_amount_left * ONE // antifee
                        out.last_tick_j = min(Inv // (f + (x + x_dest)) - g + 1, y)
                        x_dest = (in_amount_left - x_dest) * admin_fee // ONE
                        x += in_amount_left
                        out.out_amount += y - out.last_tick_j
                        out.ticks_in[j] = x - x_dest
                        out.in_amount = in_amount
                        out.admin_fee += x_dest
                        break
                    dx = max(dx, 1)
                    x_dest = (dx - x_dest) * admin_fee // ONE
                    in_amount_left -= dx
                    out.ticks_in[j] = x + dx - x_dest
                    out.in_amount += dx
                    out.out_amount += y
                    out.admin_fee += x_dest
                if i != MAX_TICKS + MAX_SKIP_TICKS - 1:
                    if out.n2 == self.max_band:
                        break
                    if j == MAX_TICKS - 1:
                        break
                    if p_ratio < ONE36 // self.MAX_ORACLE_DN_POW:
                        break
                    out.n2 += 1
                    p_o_up = p_o_up * self.Aminus1 // self.A
                    x = 0
                    b2 = self.bands.get(out.n2)
                    y = b2[1] if b2 else 0
            else:
                if x != 0 and f != 0:
                    y_dest = (Inv // f - g) - y
                    dy = y_dest * antifee // ONE
                    if dy >= in_amount_left:
                        y_dest = in_amount_left * ONE // antifee
                        out.last_tick_j = min(Inv // (g + (y + y_dest)) - f + 1, x)
                        y_dest = (in_amount_left - y_dest) * admin_fee // ONE
                        y += in_amount_left
                        out.out_amount += x - out.last_tick_j
                        out.ticks_in[j] = y - y_dest
                        out.in_amount = in_amount
                        out.admin_fee += y_dest
                        break
                    dy = max(dy, 1)
                    y_dest = (dy - y_dest) * admin_fee // ONE
                    in_amount_left -= dy
                    out.ticks_in[j] = y + dy - y_dest
                    out.in_amount += dy
                    out.out_amount += x
                    out.admin_fee += y_dest
                if i != MAX_TICKS + MAX_SKIP_TICKS - 1:
                    if out.n2 == self.min_band:
                        break
                    if j == MAX_TICKS - 1:
                        break
                    if p_ratio > self.MAX_ORACLE_DN_POW:
                        break
                    out.n2 -= 1
                    p_o_up = p_o_up * self.A // self.Aminus1
                    b2 = self.bands.get(out.n2)
                    x = b2[0] if b2 else 0
                    y = 0

            if j != MAX_TICKS:
                j += 1

        out.in_amount = (out.in_amount + in_precision - 1) // in_precision * in_precision
        out.out_amount = out.out_amount // out_precision * out_precision
        return out

    def apply_trade_dx(self, i: int, j: int, dx: int) -> tuple[int, int]:
        """Execute a swap of dx of token i (0=borrowed, 1=collateral) into the
        book. Returns (in_done, out_done) in token units. Mirrors the C++
        apply_trade_dx (calc_swap_out + band writes)."""
        if dx == 0:
            return 0, 0
        po, dfee = self.tick_oracle()
        pump = (i == 0 and j == 1)
        in_prec = self.BORROWED_PRECISION if pump else self.COLLATERAL_PRECISION
        out_prec = self.COLLATERAL_PRECISION if pump else self.BORROWED_PRECISION
        tr = self.calc_swap_out(pump, dx * in_prec, po, dfee, in_prec, out_prec)
        in_done = tr.in_amount // in_prec
        out_done = tr.out_amount // out_prec
        if in_done == 0 or out_done == 0:
            return 0, 0
        if i == 0:
            self.admin_fees_x += tr.admin_fee // in_prec
        else:
            self.admin_fees_y += tr.admin_fee // in_prec
        n = min(tr.n1, tr.n2)
        nd = abs(tr.n2 - tr.n1)
        for k in range(min(nd + 1, MAX_TICKS)):
            bs = self.bands.setdefault(n, [0, 0, 0])
            if i == 0:
                idx = k
                if idx < len(tr.ticks_in):
                    bs[0] = tr.ticks_in[idx]
                    bs[1] = tr.last_tick_j if n == tr.n2 else 0
            else:
                idx = nd - k
                if idx < len(tr.ticks_in):
                    bs[1] = tr.ticks_in[idx]
                    bs[0] = tr.last_tick_j if n == tr.n2 else 0
            n += 1
        self.active_band = tr.n2
        return in_done, out_done

    # ---- user summaries --------------------------------------------------
    def _user_ticks(self, user: str) -> list[int]:
        u = self.users.get(user)
        if not u:
            return []
        return [u["shares"].get(n, 0) for n in range(u["ns0"], u["ns1"] + 1)]

    def get_sum_xy(self, user: str) -> tuple[int, int]:
        u = self.users.get(user)
        if not u:
            return 0, 0
        ticks = self._user_ticks(user)
        if not ticks or ticks[0] == 0:
            return 0, 0
        sx = sy = 0
        for idx, n in enumerate(range(u["ns0"], u["ns1"] + 1)):
            b = self.bands.get(n)
            bx, by, sh = (b[0], b[1], b[2]) if b else (0, 0, 0)
            total = sh + DEAD_SHARES
            ds = ticks[idx]
            sx += (bx + 1) * ds // total
            sy += (by + 1) * ds // total
        return sx // self.BORROWED_PRECISION, sy // self.COLLATERAL_PRECISION

    def get_xy_up(self, user: str, use_y: bool) -> int:
        u = self.users.get(user)
        if not u:
            return 0
        ticks = self._user_ticks(user)
        if not ticks or ticks[0] == 0:
            return 0
        p_o = self.price_oracle_ro()[0]
        if p_o == 0:
            raise ValueError("get_xy_up: p_o=0")
        n_lo, n_hi = u["ns0"], u["ns1"]
        n_active = self.active_band
        p_o_down = self.p_oracle_up(n_lo)
        total = 0
        for idx, n in enumerate(range(n_lo, n_hi + 1)):
            b = self.bands.get(n)
            x = y = 0
            if b:
                if n >= n_active:
                    y = b[1]
                if n <= n_active:
                    x = b[0]
            p_o_up = p_o_down
            p_o_down = p_o_down * self.Aminus1 // self.A
            if x == 0 and y == 0:
                continue
            total_share = b[2] if b else 0
            user_share = ticks[idx]
            if total_share == 0 or user_share == 0:
                continue
            total_share += DEAD_SHARES
            p_current_mid = (p_o * p_o) // p_o_down * p_o // p_o_up
            if x == 0 or y == 0:
                if p_o > p_o_up:
                    y_equiv = y if y != 0 else x * ONE // p_current_mid
                    if use_y:
                        total += y_equiv * user_share // total_share
                    else:
                        total += (y_equiv * p_o_up // self.SQRT_BAND_RATIO) * user_share // total_share
                    continue
                elif p_o < p_o_down:
                    x_equiv = x if x != 0 else y * p_current_mid // ONE
                    if use_y:
                        total += (x_equiv * self.SQRT_BAND_RATIO // p_o_up) * user_share // total_share
                    else:
                        total += x_equiv * user_share // total_share
                    continue
            y0 = self.get_y0(x, y, p_o, p_o_up)
            f = (self.A * y0 * p_o // p_o_up) * p_o // ONE
            g = self.Aminus1 * y0 * p_o_up // p_o
            Inv = (f + x) * (g + y)
            if p_o > p_o_up:
                y_o = max(Inv // f, g) - g
                if use_y:
                    total += y_o * user_share // total_share
                else:
                    total += (y_o * p_o_up // self.SQRT_BAND_RATIO) * user_share // total_share
            elif p_o < p_o_down:
                x_o = max(Inv // g, f) - f
                if use_y:
                    total += (x_o * self.SQRT_BAND_RATIO // p_o_up) * user_share // total_share
                else:
                    total += x_o * user_share // total_share
            else:
                y_o = self.A * y0 * (p_o - p_o_down) // p_o
                x_o = max(Inv // (g + y_o), f) - f
                if use_y:
                    root = self._sqrt_int(p_o_up * p_o)
                    total += (y_o + x_o * ONE // root) * user_share // total_share
                else:
                    root = self._sqrt_int(p_o_down * p_o)
                    total += (x_o + y_o * root // ONE) * user_share // total_share
        return total // (self.COLLATERAL_PRECISION if use_y else self.BORROWED_PRECISION)

    def get_x_down(self, user: str) -> int:
        return self.get_xy_up(user, False)

    def compute_health(self, user: str, liquidation_discount: int,
                       full: bool = True, debt_override: int | None = None,
                       p_oracle_override: int | None = None) -> int:
        """Signed health ×1e18. debt_override lets the caller pass debt reduced
        by prior partial hard-liquidations (the snapshot's debt field is
        static)."""
        u = self.users.get(user)
        if not u:
            return 0
        debt = u["debt"] if debt_override is None else debt_override
        if debt == 0:
            return 0
        x_down = self.get_x_down(user)
        _, sum_y = self.get_sum_xy(user)
        p_oracle = (p_oracle_override if p_oracle_override is not None
                    else self.price_oracle_ro()[0])
        h = ONE - liquidation_discount
        h = x_down * h // debt - ONE
        if full and u["ns0"] > self.active_band:
            p_up = self.p_oracle_up(u["ns0"])
            if p_oracle > p_up:
                num = (p_oracle - p_up) * sum_y * self.COLLATERAL_PRECISION
                den = debt * self.BORROWED_PRECISION
                if den > 0:
                    h += num // den
        return h

    def apply_withdraw(self, user: str, frac: int) -> tuple[int, int]:
        """Remove frac (1e18) of the user's shares from the bands — the AMM leg
        of Controller.liquidate/liquidate_extended. Returns (dx, dy) removed."""
        u = self.users.get(user)
        if not u:
            return 0, 0
        n_lo, n_hi = u["ns0"], u["ns1"]
        total_x = total_y = 0
        min_band = self.min_band
        old_max_band = self.max_band
        max_band = n_lo - 1
        n = n_lo
        while True:
            bs = self.bands.setdefault(n, [0, 0, 0])
            x, y = bs[0], bs[1]
            user_share = u["shares"].get(n, 0)
            ds = frac * user_share // ONE
            u["shares"][n] = user_share - ds
            sh = bs[2]
            new_shares = sh - ds
            bs[2] = new_shares
            s_plus = sh + DEAD_SHARES
            dx = (x + 1) * ds // s_plus
            dy = (y + 1) * ds // s_plus
            x -= dx
            y -= dy
            if new_shares == 0:
                if x > 0:
                    self.admin_fees_x += x // self.BORROWED_PRECISION
                if y > 0:
                    self.admin_fees_y += y // self.COLLATERAL_PRECISION
                x = 0
                y = 0
            if n == min_band and x == 0 and y == 0:
                min_band += 1
            if x > 0 or y > 0:
                max_band = n
            bs[0], bs[1] = x, y
            total_x += dx
            total_y += dy
            if n == n_hi:
                break
            n += 1
        if frac == ONE:
            u["shares"].clear()
        if self.min_band != min_band:
            self.min_band = min_band
        if old_max_band <= n_hi:
            self.max_band = max_band
        return total_x // self.BORROWED_PRECISION, total_y // self.COLLATERAL_PRECISION
