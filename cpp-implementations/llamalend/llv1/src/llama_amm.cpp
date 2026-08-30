// llama_amm.cpp — LLAMMA read-only math port.
#include "llama_amm.hpp"
#include <stdexcept>

// ---------- helpers ----------
u256 sqrt_int(const u256& x) {
    // Newton's method
    if (x == 0) return 0;
    u256 z = x;
    u256 y = (x + 1) / 2;
    while (y < z) { z = y; y = (x / y + y) / 2; }
    return z;
}

// _rate_mul(): unsafe_div(self.rate_mul * (10**18 + self.rate * (block.timestamp - self.rate_time)), 10**18)
u256 rate_mul_current(const LlammaState& s) {
    u256 dt = s.block_timestamp - s.rate_time;
    u256 num = s.rate_mul * (ONE_1E18() + s.rate * dt);
    return num / ONE_1E18();
}

u256 base_price(const LlammaImmutables& im, const LlammaState& s) {
    return im.BASE_PRICE * rate_mul_current(s) / ONE_1E18();
}

// Solmate exp — port of the exp polynomial used in Vyper's _p_oracle_up.
// Internal math uses z256 (arbitrary-precision signed int) so Vyper's signed
// two's-complement semantics for `convert(int256, uint256)` work out cleanly
// via to_uint256_mod at the end. Final result is truncated modulo 2^256.
u256 solmate_exp(i256 power_i) {
    static const i256 LOW  = i256("-41446531673892821376");
    static const i256 HIGH = i256("135305999368893231589");
    if (!(power_i > LOW && power_i < HIGH)) {
        throw std::runtime_error("solmate_exp: power out of range");
    }
    z256 power = as_z256(power_i);
    static const z256 TWO_96 = z256(1) << 96;
    static const z256 TWO_95 = z256(1) << 95;
    static const z256 E18    = z256("1000000000000000000");

    z256 x = (power * TWO_96) / E18;
    static const z256 C_K = z256("54916777467707473351141471128");
    z256 k = ((x * TWO_96) / C_K + TWO_95) / TWO_96;
    x = x - k * C_K;

    z256 y = x + z256("1346386616545796478920950773328");
    y = (y * x) / TWO_96 + z256("57155421227552351082224309758442");
    z256 p = y + x - z256("94201549194550492254356042504812");
    p = (p * y) / TWO_96 + z256("28719021644029726153956944680412240");
    p = p * x + z256("4385272521454847904659076985693276") * TWO_96;

    z256 q = x - z256("2855989394907223263936484059900");
    q = (q * x) / TWO_96 + z256("50020603652535783019961831881945");
    q = (q * x) / TWO_96 - z256("533845033583426703283633433725380");
    q = (q * x) / TWO_96 + z256("3604857256930695427073651918091429");
    q = (q * x) / TWO_96 - z256("14423608567350463180887372962807573");
    q = (q * x) / TWO_96 + z256("26449188498355588339934803723976023");

    // convert(p/q, uint256) — reinterpret signed result as unsigned mod 2^256.
    z256 pq = p / q;
    u256 pq_u = to_uint256_mod(pq);
    // Product may exceed 256 bits transiently, but the subsequent right shift
    // brings it back — do it in arbitrary precision then truncate at the end.
    z256 prod_z = z256(pq_u) * z256("3822833074963236453042738258902158003155416615667");

    z256 shift_amt = k - 195;
    z256 result_z;
    if (shift_amt >= 0) {
        result_z = prod_z << shift_amt.convert_to<int>();
    } else {
        // Vyper's `shift(u, negative)` on uint256 is a logical (unsigned) right
        // shift. Truncate prod to uint256 first so we shift the wrapped repr.
        u256 prod_u = to_uint256_mod(prod_z);
        result_z = z256(prod_u) >> (-shift_amt).convert_to<int>();
    }
    u256 exp_result = to_uint256_mod(result_z);
    if (exp_result <= 1000) throw std::runtime_error("solmate_exp: precision floor violated");
    return exp_result;
}

u256 p_oracle_up(const LlammaImmutables& im, const LlammaState& s, i256 n) {
    // power = -n * LOG_A_RATIO
    i256 power = -n * im.LOG_A_RATIO;
    u256 e = solmate_exp(power);
    return base_price(im, s) * e / ONE_1E18();
}

// limit_p_o — see amm.py::limit_p_o for algebra.
LimitedP limit_p_o(const LlammaImmutables& im, const LlammaState& s, u256 p) {
    constexpr uint64_t PREV_P_O_DELAY = 2 * 60;                     // 2 min
    static const u256 MAX_P_O_CHG = u256("1250000000000000000");    // 12500 * 10**14 = 1.25e18
    u256 p_new = p;
    u256 dt_raw = s.block_timestamp - s.prev_p_o_time;
    u256 dt_capped = std::min<u256>(u256(PREV_P_O_DELAY), dt_raw);
    u256 dt = u256(PREV_P_O_DELAY) - dt_capped;
    u256 ratio = 0;

    if (dt > 0) {
        u256 old_p_o = s.old_p_o;
        u256 old_ratio = s.old_dfee;
        if (p > old_p_o) {
            ratio = old_p_o * ONE_1E18() / p;
            u256 floor_ = ONE_1E36() / MAX_P_O_CHG;
            if (ratio < floor_) {
                p_new = old_p_o * MAX_P_O_CHG / ONE_1E18();
                ratio = floor_;
            }
        } else {
            ratio = p * ONE_1E18() / old_p_o;
            u256 floor_ = ONE_1E36() / MAX_P_O_CHG;
            if (ratio < floor_) {
                p_new = old_p_o * ONE_1E18() / MAX_P_O_CHG;
                ratio = floor_;
            }
        }
        // ratio = min((1e18 + old_ratio - ratio**3/1e36) * dt / PREV_P_O_DELAY, 1e18-1)
        u256 r3 = ratio * ratio * ratio / ONE_1E36();
        u256 inner = (ONE_1E18() + old_ratio) - r3;
        u256 scaled = inner * dt / PREV_P_O_DELAY;
        u256 cap = ONE_1E18() - 1;
        ratio = std::min(scaled, cap);
    }

    return LimitedP{p_new, ratio};
}

LimitedP price_oracle_ro(const LlammaImmutables& im, const LlammaState& s) {
    return limit_p_o(im, s, s.external_price);
}

u256 get_y0(const LlammaImmutables& im, u256 x, u256 y, u256 p_o, u256 p_o_up) {
    if (p_o == 0) throw std::runtime_error("get_y0: p_o=0");
    u256 b = 0;
    if (x != 0) b = p_o_up * im.Aminus1 * x / p_o;
    if (y != 0) b += im.A * (p_o * p_o) / p_o_up * y / ONE_1E18();
    if (x > 0 && y > 0) {
        u256 D = b * b + (u256(4) * im.A * p_o * y / ONE_1E18()) * x;
        return (b + sqrt_int(D)) * ONE_1E18() / (u256(2) * im.A * p_o);
    } else {
        return b * ONE_1E18() / (im.A * p_o);
    }
}

// _get_p(n, x, y): current AMM price in a band
u256 get_p_in_band(const LlammaImmutables& im, const LlammaState& s, i256 n, u256 x, u256 y) {
    u256 p_o_up = p_oracle_up(im, s, n);
    u256 p_o = price_oracle_ro(im, s).p;
    if (p_o_up == 0) throw std::runtime_error("get_p: p_o_up=0");

    if (x == 0) {
        if (y == 0) {
            // mid-band
            return (((p_o * p_o) / p_o_up) * p_o / p_o_up) * im.A / im.Aminus1;
        }
        return ((p_o * p_o) / p_o_up) * p_o / p_o_up;
    }
    if (y == 0) {
        u256 p_o_down_local = p_o_up * im.Aminus1 / im.A;
        return (p_o * p_o) / p_o_down_local * p_o / p_o_down_local;
    }
    u256 y0 = get_y0(im, x, y, p_o, p_o_up);
    u256 f = im.A * y0 * p_o / p_o_up * p_o;
    u256 g = im.Aminus1 * y0 * p_o_up / p_o;
    return (f + x * ONE_1E18()) / (g + y);
}

u256 get_dynamic_fee(const LlammaImmutables& im, u256 p_o, u256 p_o_up) {
    // p_c_d = (p_o**2 / p_o_up) * p_o / p_o_up
    u256 p_c_d = ((p_o * p_o) / p_o_up) * p_o / p_o_up;
    // p_c_u = p_c_d * A/Aminus1 * A/Aminus1
    u256 p_c_u = p_c_d * im.A / im.Aminus1 * im.A / im.Aminus1;
    static const u256 QUARTER_ONE = ONE_1E18() / 4;
    if (p_o < p_c_d) return (p_c_d - p_o) * QUARTER_ONE / p_c_d;
    if (p_o > p_c_u) return (p_o - p_c_u) * QUARTER_ONE / p_o;
    return 0;
}

// _read_user_ticks: dev returns per-band shares vector between ns0 and ns1
std::vector<u256> read_user_ticks(const LlammaState& s, const std::string& user) {
    std::vector<u256> out;
    auto it = s.users.find(user);
    if (it == s.users.end()) return out;
    const UserTicks& ut = it->second;
    int64_t lo = ut.ns0.convert_to<int64_t>();
    int64_t hi = ut.ns1.convert_to<int64_t>();
    for (int64_t n = lo; n <= hi; ++n) {
        auto sit = ut.shares.find(n);
        out.push_back(sit == ut.shares.end() ? u256(0) : sit->second);
    }
    return out;
}

// _get_xy(user, is_sum)
XY get_xy_impl(const LlammaImmutables& im, const LlammaState& s, const std::string& user, bool is_sum) {
    XY r;
    if (is_sum) { r.sumX = 0; r.sumY = 0; }
    auto it = s.users.find(user);
    if (it == s.users.end()) return r;
    const UserTicks& ut = it->second;
    auto ticks = read_user_ticks(s, user);
    if (ticks.empty() || ticks[0] == 0) return r;
    int64_t n_lo = ut.ns0.convert_to<int64_t>();
    int64_t n_hi = ut.ns1.convert_to<int64_t>();
    static const u256 DEAD_SHARES = 1000;
    int64_t idx = 0;
    for (int64_t n = n_lo; n <= n_hi; ++n, ++idx) {
        auto bit = s.bands.find(n);
        u256 bx = bit == s.bands.end() ? u256(0) : bit->second.x;
        u256 by = bit == s.bands.end() ? u256(0) : bit->second.y;
        u256 total_shares = (bit == s.bands.end() ? u256(0) : bit->second.shares) + DEAD_SHARES;
        u256 ds = ticks[idx];
        u256 dx = (bx + 1) * ds / total_shares;
        u256 dy = (by + 1) * ds / total_shares;
        if (is_sum) { r.sumX += dx; r.sumY += dy; }
        else { r.xs.push_back(dx / im.BORROWED_PRECISION); r.ys.push_back(dy / im.COLLATERAL_PRECISION); }
    }
    if (is_sum) { r.sumX = r.sumX / im.BORROWED_PRECISION; r.sumY = r.sumY / im.COLLATERAL_PRECISION; }
    return r;
}

// get_xy_up — big function, direct port of amm.py::get_xy_up
u256 get_xy_up(const LlammaImmutables& im, const LlammaState& s, const std::string& user, bool use_y) {
    auto it = s.users.find(user);
    if (it == s.users.end()) return 0;
    const UserTicks& ut = it->second;
    auto ticks = read_user_ticks(s, user);
    if (ticks.empty() || ticks[0] == 0) return 0;

    u256 p_o = price_oracle_ro(im, s).p;
    if (p_o == 0) throw std::runtime_error("get_xy_up: p_o=0");

    int64_t n_lo = ut.ns0.convert_to<int64_t>();
    int64_t n_hi = ut.ns1.convert_to<int64_t>();
    int64_t n_active = s.active_band.convert_to<int64_t>();
    u256 p_o_down = p_oracle_up(im, s, i256(n_lo));
    u256 XY_total = 0;
    static const u256 DEAD_SHARES = 1000;

    int64_t idx = 0;
    for (int64_t n = n_lo; n <= n_hi; ++n, ++idx) {
        auto bit = s.bands.find(n);
        u256 x = 0, y = 0;
        if (n >= n_active && bit != s.bands.end()) y = bit->second.y;
        if (n <= n_active && bit != s.bands.end()) x = bit->second.x;
        u256 p_o_up = p_o_down;
        p_o_down = p_o_down * im.Aminus1 / im.A;
        if (x == 0 && y == 0) continue;

        u256 total_share = bit == s.bands.end() ? u256(0) : bit->second.shares;
        u256 user_share = ticks[idx];
        if (total_share == 0 || user_share == 0) continue;
        total_share += DEAD_SHARES;

        u256 p_current_mid = (p_o * p_o) / p_o_down * p_o / p_o_up;

        if (x == 0 || y == 0) {
            if (p_o > p_o_up) {
                u256 y_equiv = y;
                if (y == 0) y_equiv = x * ONE_1E18() / p_current_mid;
                if (use_y) XY_total += y_equiv * user_share / total_share;
                else       XY_total += (y_equiv * p_o_up / im.SQRT_BAND_RATIO) * user_share / total_share;
                continue;
            } else if (p_o < p_o_down) {
                u256 x_equiv = x;
                if (x == 0) x_equiv = y * p_current_mid / ONE_1E18();
                if (use_y) XY_total += (x_equiv * im.SQRT_BAND_RATIO / p_o_up) * user_share / total_share;
                else       XY_total += x_equiv * user_share / total_share;
                continue;
            }
        }

        u256 y0 = get_y0(im, x, y, p_o, p_o_up);
        u256 f = (im.A * y0 * p_o / p_o_up) * p_o / ONE_1E18();
        u256 g = im.Aminus1 * y0 * p_o_up / p_o;
        u256 Inv = (f + x) * (g + y);

        u256 x_o = 0, y_o = 0;
        if (p_o > p_o_up) {
            y_o = std::max<u256>(Inv / f, g) - g;
            if (use_y) XY_total += y_o * user_share / total_share;
            else       XY_total += (y_o * p_o_up / im.SQRT_BAND_RATIO) * user_share / total_share;
        } else if (p_o < p_o_down) {
            x_o = std::max<u256>(Inv / g, f) - f;
            if (use_y) XY_total += (x_o * im.SQRT_BAND_RATIO / p_o_up) * user_share / total_share;
            else       XY_total += x_o * user_share / total_share;
        } else {
            y_o = im.A * y0 * (p_o - p_o_down) / p_o;
            x_o = std::max<u256>(Inv / (g + y_o), f) - f;
            if (use_y) {
                u256 root = sqrt_int(p_o_up * p_o);
                XY_total += (y_o + x_o * ONE_1E18() / root) * user_share / total_share;
            } else {
                u256 root = sqrt_int(p_o_down * p_o);
                XY_total += (x_o + y_o * root / ONE_1E18()) * user_share / total_share;
            }
        }
    }
    return use_y ? XY_total / im.COLLATERAL_PRECISION : XY_total / im.BORROWED_PRECISION;
}

// ---------------------------------------------------------------------------
// calc_swap_out — the band-crossing swap engine
// ---------------------------------------------------------------------------
constexpr int MAX_TICKS_C = 50;
constexpr int MAX_SKIP_TICKS_C = 1024;
constexpr uint64_t MAX_TICKS_UINT_C = 50;

DetailedTrade calc_swap_out(const LlammaImmutables& im, const LlammaState& s,
                            bool pump, u256 in_amount, u256 p_o, u256 p_dfee,
                            u256 in_precision, u256 out_precision) {
    (void)in_precision; (void)out_precision;
    DetailedTrade out;
    out.n2 = s.active_band;
    u256 p_o_up = p_oracle_up(im, s, out.n2);
    auto bit = s.bands.find(out.n2.convert_to<int64_t>());
    u256 x = bit == s.bands.end() ? u256(0) : bit->second.x;
    u256 y = bit == s.bands.end() ? u256(0) : bit->second.y;

    u256 in_amount_left = in_amount;
    u256 fee = std::max(s.fee, p_dfee);
    u256 admin_fee = s.admin_fee;
    uint64_t j = MAX_TICKS_UINT_C;

    static const u256 ONE = ONE_1E18();
    for (int i = 0; i < MAX_TICKS_C + MAX_SKIP_TICKS_C; ++i) {
        u256 y0 = 0, f = 0, g = 0, Inv = 0;
        u256 dyn_fee = fee;

        if (x > 0 || y > 0) {
            if (j == MAX_TICKS_UINT_C) { out.n1 = out.n2; j = 0; }
            y0 = get_y0(im, x, y, p_o, p_o_up);
            f = im.A * y0 * p_o / p_o_up * p_o / ONE;
            g = im.Aminus1 * y0 * p_o_up / p_o;
            Inv = (f + x) * (g + y);
            dyn_fee = std::max(get_dynamic_fee(im, p_o, p_o_up), fee);
        }

        u256 antifee = (ONE * ONE) / (ONE - std::min<u256>(dyn_fee, ONE - 1));

        if (j != MAX_TICKS_UINT_C) {
            u256 _tick = pump ? x : y;
            // Match Vyper `out.ticks_in.append(_tick)` — always add per iter
            out.ticks_in.push_back(_tick);
        }

        u256 p_ratio = p_o_up * ONE / p_o;

        if (pump) {
            if (y != 0 && g != 0) {
                u256 x_dest = (Inv / g - f) - x;
                u256 dx = x_dest * antifee / ONE;
                if (dx >= in_amount_left) {
                    x_dest = in_amount_left * ONE / antifee;
                    out.last_tick_j = std::min<u256>(Inv / (f + (x + x_dest)) - g + 1, y);
                    x_dest = (in_amount_left - x_dest) * admin_fee / ONE;
                    x += in_amount_left;
                    out.out_amount += y - out.last_tick_j;
                    out.ticks_in[j] = x - x_dest;
                    out.in_amount = in_amount;
                    out.admin_fee += x_dest;
                    break;
                } else {
                    dx = std::max<u256>(dx, 1);
                    x_dest = (dx - x_dest) * admin_fee / ONE;
                    in_amount_left -= dx;
                    out.ticks_in[j] = x + dx - x_dest;
                    out.in_amount += dx;
                    out.out_amount += y;
                    out.admin_fee += x_dest;
                }
            }
            if (i != MAX_TICKS_C + MAX_SKIP_TICKS_C - 1) {
                if (out.n2 == s.max_band) break;
                if (j == MAX_TICKS_UINT_C - 1) break;
                if (p_ratio < ONE_1E36() / im.MAX_ORACLE_DN_POW) break;
                out.n2 += 1;
                p_o_up = p_o_up * im.Aminus1 / im.A;
                x = 0;
                auto b2 = s.bands.find(out.n2.convert_to<int64_t>());
                y = b2 == s.bands.end() ? u256(0) : b2->second.y;
            }
        } else {
            if (x != 0 && f != 0) {
                u256 y_dest = (Inv / f - g) - y;
                u256 dy = y_dest * antifee / ONE;
                if (dy >= in_amount_left) {
                    y_dest = in_amount_left * ONE / antifee;
                    out.last_tick_j = std::min<u256>(Inv / (g + (y + y_dest)) - f + 1, x);
                    y_dest = (in_amount_left - y_dest) * admin_fee / ONE;
                    y += in_amount_left;
                    out.out_amount += x - out.last_tick_j;
                    out.ticks_in[j] = y - y_dest;
                    out.in_amount = in_amount;
                    out.admin_fee += y_dest;
                    break;
                } else {
                    dy = std::max<u256>(dy, 1);
                    y_dest = (dy - y_dest) * admin_fee / ONE;
                    in_amount_left -= dy;
                    out.ticks_in[j] = y + dy - y_dest;
                    out.in_amount += dy;
                    out.out_amount += x;
                    out.admin_fee += y_dest;
                }
            }
            if (i != MAX_TICKS_C + MAX_SKIP_TICKS_C - 1) {
                if (out.n2 == s.min_band) break;
                if (j == MAX_TICKS_UINT_C - 1) break;
                if (p_ratio > im.MAX_ORACLE_DN_POW) break;
                out.n2 -= 1;
                p_o_up = p_o_up * im.A / im.Aminus1;
                auto b2 = s.bands.find(out.n2.convert_to<int64_t>());
                x = b2 == s.bands.end() ? u256(0) : b2->second.x;
                y = 0;
            }
        }

        if (j != MAX_TICKS_UINT_C) j += 1;
    }

    // Round up what goes in, round down what goes out
    out.in_amount = (out.in_amount + in_precision - 1) / in_precision * in_precision;
    out.out_amount = out.out_amount / out_precision * out_precision;
    return out;
}

// calc_swap_in — byte-exact port of amm.py:1161 (specified-output form).
DetailedTrade calc_swap_in(const LlammaImmutables& im, const LlammaState& s,
                           bool pump, u256 out_amount, u256 p_o, u256 p_dfee,
                           u256 in_precision, u256 out_precision) {
    (void)in_precision; (void)out_precision;
    DetailedTrade out;
    out.n2 = s.active_band;
    u256 p_o_up = p_oracle_up(im, s, out.n2);
    auto bit = s.bands.find(out.n2.convert_to<int64_t>());
    u256 x = bit == s.bands.end() ? u256(0) : bit->second.x;
    u256 y = bit == s.bands.end() ? u256(0) : bit->second.y;

    u256 out_amount_left = out_amount;
    u256 fee = std::max(s.fee, p_dfee);
    u256 admin_fee = s.admin_fee;
    uint64_t j = MAX_TICKS_UINT_C;

    static const u256 ONE = ONE_1E18();
    for (int i = 0; i < MAX_TICKS_C + MAX_SKIP_TICKS_C; ++i) {
        u256 y0 = 0, f = 0, g = 0, Inv = 0;
        u256 dyn_fee = fee;

        if (x > 0 || y > 0) {
            if (j == MAX_TICKS_UINT_C) { out.n1 = out.n2; j = 0; }
            y0 = get_y0(im, x, y, p_o, p_o_up);
            f = im.A * y0 * p_o / p_o_up * p_o / ONE;
            g = im.Aminus1 * y0 * p_o_up / p_o;
            Inv = (f + x) * (g + y);
            dyn_fee = std::max(get_dynamic_fee(im, p_o, p_o_up), fee);
        }

        u256 antifee = (ONE * ONE) / (ONE - std::min<u256>(dyn_fee, ONE - 1));

        if (j != MAX_TICKS_UINT_C) {
            u256 _tick = pump ? x : y;
            out.ticks_in.push_back(_tick);
        }

        u256 p_ratio = p_o_up * ONE / p_o;

        if (pump) {
            if (y != 0 && g != 0) {
                if (y >= out_amount_left) {
                    out.last_tick_j = y - out_amount_left;
                    u256 x_dest = Inv / (g + out.last_tick_j) - f - x;
                    u256 dx = x_dest * antifee / ONE;
                    out.out_amount = out_amount;
                    out.in_amount += dx;
                    x_dest = (dx - x_dest) * admin_fee / ONE;
                    out.ticks_in[j] = x + dx - x_dest;
                    out.admin_fee += x_dest;
                    break;
                } else {
                    u256 x_dest = (Inv / g - f) - x;
                    u256 dx = std::max<u256>(x_dest * antifee / ONE, u256(1));
                    out_amount_left -= y;
                    out.in_amount += dx;
                    out.out_amount += y;
                    x_dest = (dx - x_dest) * admin_fee / ONE;
                    out.ticks_in[j] = x + dx - x_dest;
                    out.admin_fee += x_dest;
                }
            }
            if (i != MAX_TICKS_C + MAX_SKIP_TICKS_C - 1) {
                if (out.n2 == s.max_band) break;
                if (j == MAX_TICKS_UINT_C - 1) break;
                if (p_ratio < ONE_1E36() / im.MAX_ORACLE_DN_POW) break;
                out.n2 += 1;
                p_o_up = p_o_up * im.Aminus1 / im.A;
                x = 0;
                auto b2 = s.bands.find(out.n2.convert_to<int64_t>());
                y = b2 == s.bands.end() ? u256(0) : b2->second.y;
            }
        } else {
            if (x != 0 && f != 0) {
                if (x >= out_amount_left) {
                    out.last_tick_j = x - out_amount_left;
                    u256 y_dest = Inv / (f + out.last_tick_j) - g - y;
                    u256 dy = y_dest * antifee / ONE;
                    out.out_amount = out_amount;
                    out.in_amount += dy;
                    y_dest = (dy - y_dest) * admin_fee / ONE;
                    out.ticks_in[j] = y + dy - y_dest;
                    out.admin_fee += y_dest;
                    break;
                } else {
                    u256 y_dest = (Inv / f - g) - y;
                    u256 dy = std::max<u256>(y_dest * antifee / ONE, u256(1));
                    out_amount_left -= x;
                    out.in_amount += dy;
                    out.out_amount += x;
                    y_dest = (dy - y_dest) * admin_fee / ONE;
                    out.ticks_in[j] = y + dy - y_dest;
                    out.admin_fee += y_dest;
                }
            }
            if (i != MAX_TICKS_C + MAX_SKIP_TICKS_C - 1) {
                if (out.n2 == s.min_band) break;
                if (j == MAX_TICKS_UINT_C - 1) break;
                if (p_ratio > im.MAX_ORACLE_DN_POW) break;
                out.n2 -= 1;
                p_o_up = p_o_up * im.A / im.Aminus1;
                auto b2 = s.bands.find(out.n2.convert_to<int64_t>());
                x = b2 == s.bands.end() ? u256(0) : b2->second.x;
                y = 0;
            }
        }
        if (j != MAX_TICKS_UINT_C) j += 1;
    }

    out.in_amount  = (out.in_amount + in_precision - 1) / in_precision * in_precision;
    out.out_amount = out.out_amount / out_precision * out_precision;
    return out;
}

// _price_oracle_w equivalent: apply limit_p_o and persist to state.
LimitedP tick_oracle(const LlammaImmutables& im, LlammaState& s) {
    LimitedP r = limit_p_o(im, s, s.external_price);
    s.prev_p_o_time = s.block_timestamp;
    s.old_p_o = r.p;
    s.old_dfee = r.dfee;
    return r;
}

// ---------------------------------------------------------------------------
// Health scan (Controller.health-style)
// ---------------------------------------------------------------------------
// We compute (xDown, sumY, ns0, pUp) on the fly from LLAMMA state — needing to
// look up per-band values from bands[b] and the user's per-band shares.
static u256 compute_xDown_and_sumY(const LlammaImmutables& im, const LlammaState& s,
                                   const UserTicks& ut, u256& sumY_out) {
    // xDown = get_xy_up(user, use_y=false) equivalent — but we need to be
    // careful: get_xy_up divides by BORROWED/COLLATERAL_PRECISION at the end,
    // while the smol TS project's `_get_xy` returns unscaled sums. The health
    // formula in TS uses xDown before precision division: see amm.py get_x_down
    // returns unsafe_div(XY, BORROWED_PRECISION). Since our precisions are 1
    // for this market, division-by-1 is a no-op.
    // For sumY we sum user's per-band collateral shares.
    sumY_out = 0;
    u256 xDown = 0;
    // Reuse get_xy_up in "x direction" (use_y=false) — same as amm.py get_x_down
    // Also sum sumY manually by walking user's bands.
    static const u256 DEAD_SHARES = 1000;
    for (auto& shv : ut.shares) {
        int64_t b = shv.first;
        u256 user_share = shv.second;
        auto bit = s.bands.find(b);
        if (bit == s.bands.end() || bit->second.shares == 0 || user_share == 0) continue;
        u256 total_share = bit->second.shares + DEAD_SHARES;
        u256 by = bit->second.y;
        // sumY entry = (by+1) * user_share / total_share (matches _get_xy)
        sumY_out += (by + 1) * user_share / total_share;
    }
    // Divide sumY by COLLATERAL_PRECISION (=1 for CRV/crvUSD market)
    sumY_out = sumY_out / im.COLLATERAL_PRECISION;
    return xDown;
}

i256 compute_health(const LlammaImmutables& im, const LlammaState& s,
                    const std::string& user, u256 liquidation_discount, bool full,
                    const u256* p_oracle_override) {
    auto uit = s.users.find(user);
    if (uit == s.users.end()) return i256(0);
    const UserTicks& ut = uit->second;
    if (ut.debt == 0) return i256(0);

    // Use the ported get_x_down / get_xy_up
    u256 xDown = get_x_down(im, s, user);
    // sumY via get_sum_xy (already divides by precision)
    auto [sumX, sumY] = get_sum_xy(im, s, user);
    (void)sumX;

    // priceOracle: if caller supplied an override (e.g. chainlink for chart-3
    // counterfactual), use that verbatim; otherwise fall back to the AMM's own
    // price_oracle_ro() = limit_p_o(external_price). Mirrors the TS
    // computeHealthManualForUserArray priceOracleSource logic.
    u256 priceOracle = p_oracle_override ? *p_oracle_override : price_oracle_ro(im, s).p;

    // Health scaled to 1e18
    static const i256 ONE = i256(pow10(18));
    i256 h = ONE - i256(liquidation_discount);
    // Guard: debt shouldn't be zero (caller filters that)
    if (ut.debt == 0) return ONE - ONE;
    h = i256(xDown) * h / i256(ut.debt) - ONE;

    if (full && ut.ns0 > s.active_band) {
        u256 pUp = p_oracle_up(im, s, ut.ns0);
        if (priceOracle > pUp) {
            u256 num = (priceOracle - pUp) * sumY * im.COLLATERAL_PRECISION;
            u256 den = ut.debt * im.BORROWED_PRECISION;
            if (den > 0) h += i256(num / den);
        }
    }
    return h;
}

// apply_token_exchange — replays a swap event onto local state.
// We drive it from the ON-CHAIN (i, j, dx, dy) tuple rather than by re-running
// calc_swap_out(dx) — because the on-chain oracle's external price at each
// block is what actually drove the trade, and it's already baked into dy.
// So instead of re-quoting, we apply the AGGREGATE band mutation: run the
// swap engine with dx and check dy matches; if it does, adopt its band deltas.
void apply_token_exchange(const LlammaImmutables& im, LlammaState& s,
                          uint64_t i, uint64_t j, u256 dx, u256 dy) {
    (void)dy;
    // Update oracle first (mirrors _price_oracle_w call in _exchange)
    LimitedP po = tick_oracle(im, s);

    // pump = (i == 0): borrowed in → collateral out.
    bool pump = (i == 0 && j == 1);
    u256 in_prec  = pump ? im.BORROWED_PRECISION : im.COLLATERAL_PRECISION;
    u256 out_prec = pump ? im.COLLATERAL_PRECISION : im.BORROWED_PRECISION;

    // The TokenExchange event doesn't say whether the tx called `exchange` or
    // `exchange_dy` — but the two go through calc_swap_out vs calc_swap_in
    // (different rounding paths). Try calc_swap_out first; if its outputs
    // match the event, use it. Otherwise the tx was exchange_dy — use
    // calc_swap_in(bought) instead.
    DetailedTrade tr = calc_swap_out(im, s, pump, dx * in_prec, po.p, po.dfee, in_prec, out_prec);
    u256 in_done  = tr.in_amount  / in_prec;
    u256 out_done = tr.out_amount / out_prec;
    if (in_done != dx || out_done != dy) {
        // Event's (dx,dy) didn't match calc_swap_out — the on-chain call was
        // exchange_dy (specified-output). Try calc_swap_in instead.
        DetailedTrade tr2 = calc_swap_in(im, s, pump, dy * out_prec, po.p, po.dfee, in_prec, out_prec);
        u256 in2 = tr2.in_amount / in_prec, out2 = tr2.out_amount / out_prec;
        if (in2 == dx && out2 == dy) {
            tr = tr2; in_done = in2; out_done = out2;
        }
        // Neither exact match → keep calc_swap_out (best-effort).
    }

    // Vyper's _exchange early-exits when the swap moved nothing. Match exactly.
    if (in_done == 0 || out_done == 0) return;

    // Apply out.admin_fee, and write the ticks back to bands.
    u256 admin_fee_out = tr.admin_fee / in_prec;
    if (i == 0) s.admin_fees_x += admin_fee_out;
    else        s.admin_fees_y += admin_fee_out;

    i256 n = std::min(tr.n1, tr.n2);
    i256 n_diff = tr.n2 > tr.n1 ? (tr.n2 - tr.n1) : (tr.n1 - tr.n2);

    int nd_int = n_diff.convert_to<int>();
    for (int k = 0; k <= nd_int && k < MAX_TICKS_C; ++k) {
        int64_t bkey = n.convert_to<int64_t>();
        BandState& bs = s.bands[bkey];
        if (i == 0) {
            size_t idx = (size_t)k;
            if (idx < tr.ticks_in.size()) {
                bs.x = tr.ticks_in[idx];
                bs.y = (n == tr.n2) ? tr.last_tick_j : u256(0);
            }
        } else {
            size_t idx = (size_t)(nd_int - k);
            if (idx < tr.ticks_in.size()) {
                bs.y = tr.ticks_in[idx];
                bs.x = (n == tr.n2) ? tr.last_tick_j : u256(0);
            }
        }
        n += 1;
    }
    s.active_band = tr.n2;
}

// Apply Deposit event — mirrors deposit_range().
void apply_deposit(const LlammaImmutables& im, LlammaState& s,
                   const std::string& user, u256 amount, i256 n1, i256 n2) {
    static const u256 DEAD_SHARES = 1000;
    u256 n_bands_u = u256(n2 - n1) + 1;
    uint64_t n_bands = n_bands_u.convert_to<uint64_t>();
    u256 y_per_band = amount * im.COLLATERAL_PRECISION / n_bands;

    UserTicks& ut = s.users[user];
    ut.ns0 = n1;
    ut.ns1 = n2;

    // autoskip: move active_band down until we hit n1 or find a nonempty band
    i256 n0 = s.active_band;
    for (int i = 0; i <= MAX_SKIP_TICKS_C; ++i) {
        if (n1 > n0) { if (i != 0) s.active_band = n0; break; }
        int64_t bkey = n0.convert_to<int64_t>();
        auto bit = s.bands.find(bkey);
        u256 bx = bit == s.bands.end() ? u256(0) : bit->second.x;
        if (!(bx == 0 && i < MAX_SKIP_TICKS_C)) break;
        n0 -= 1;
    }

    i256 band = n1;
    for (uint64_t i = 0; i < n_bands && i < 50; ++i, band += 1) {
        int64_t bkey = band.convert_to<int64_t>();
        BandState& bs = s.bands[bkey];
        u256 y = y_per_band;
        if (i == 0) y = amount * im.COLLATERAL_PRECISION - y * (n_bands - 1);
        u256 total_y = bs.y;
        u256 sh = bs.shares;
        u256 ds = (sh + DEAD_SHARES) * y / (total_y + 1);
        ut.shares[bkey] = ds;
        sh += ds;
        bs.shares = sh;
        total_y += y;
        bs.y = total_y;
    }
    if (n1 < s.min_band) s.min_band = n1;
    if (n2 > s.max_band) s.max_band = n2;
}

// Apply Withdraw event — mirrors withdraw(). Removes shares proportionally to frac.
std::pair<u256, u256> apply_withdraw(const LlammaImmutables& im, LlammaState& s,
                                     const std::string& user, u256 frac) {
    static const u256 DEAD_SHARES = 1000;
    auto uit = s.users.find(user);
    if (uit == s.users.end()) return {0, 0};
    UserTicks& ut = uit->second;
    i256 n_lo = ut.ns0;
    i256 n_hi = ut.ns1;
    u256 total_x = 0, total_y = 0;
    i256 min_band = s.min_band;
    i256 old_max_band = s.max_band;
    i256 max_band = n_lo - 1;

    for (i256 n = n_lo; ; n += 1) {
        int64_t bkey = n.convert_to<int64_t>();
        BandState& bs = s.bands[bkey];
        u256 x = bs.x, y = bs.y;
        u256 user_share = ut.shares[bkey];
        u256 ds = frac * user_share / ONE_1E18();
        ut.shares[bkey] = user_share - ds;
        u256 sh = bs.shares;
        u256 new_shares = sh - ds;
        bs.shares = new_shares;
        u256 s_plus = sh + DEAD_SHARES;
        u256 dx = (x + 1) * ds / s_plus;
        u256 dy = (y + 1) * ds / s_plus;
        x -= dx;
        y -= dy;
        if (new_shares == 0) {
            if (x > 0) s.admin_fees_x += x / im.BORROWED_PRECISION;
            if (y > 0) s.admin_fees_y += y / im.COLLATERAL_PRECISION;
            x = 0; y = 0;
        }
        if (n == min_band && x == 0 && y == 0) min_band += 1;
        if (x > 0 || y > 0) max_band = n;
        bs.x = x; bs.y = y;
        total_x += dx;
        total_y += dy;
        if (n == n_hi) break;
    }

    if (frac == ONE_1E18()) {
        // clear user's ticks
        ut.shares.clear();
    }
    if (s.min_band != min_band) s.min_band = min_band;
    if (old_max_band <= n_hi) s.max_band = max_band;

    return { total_x / im.BORROWED_PRECISION, total_y / im.COLLATERAL_PRECISION };
}
