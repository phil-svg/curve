// venue.hpp — liquidation-venue engines, C++ port of the case study's
// venues.py + cryptoswap_2coin.py (which were themselves line-for-line ports of
// the wei-exact engines validated 484/484 against on-chain get_dy).
//
// Arithmetic: boost cpp_int (arbitrary precision, signed) mirrors Python ints.
// venues.py's own audit: every division that can see a negative operand goes
// through idiv() (truncate toward zero == cpp_int's operator/); all other
// division sites are positive-only, where Python's floor // equals truncation.
//
// External coin convention (all venues): 0 = borrowed (crvUSD), 1 = collateral.
#pragma once
#include <boost/multiprecision/cpp_int.hpp>
#include <algorithm>
#include <memory>
#include <stdexcept>
#include <vector>

namespace venue {

// Fixed-width signed integer on the stack: ~10x faster than dynamic cpp_int,
// and multiplication cost is quadratic in the limb count, so the width is
// kept as small as provably safe. Largest intermediates in the ported math
// are ~2^240 (tri_get_y's discriminant, cbrt's scaled input); 384 bits
// leaves a 2^140 margin. Validated empirically: a `checked` build (throws on
// any overflow) at this width ran the parity gate + full sweeps with zero
// throws and bit-identical output (2026-08-15). Build knobs for re-running
// that validation: -DVENUE_INT_BITS=<n> -DVENUE_INT_CHECKED.
#ifndef VENUE_INT_BITS
#define VENUE_INT_BITS 384
#endif
#ifdef VENUE_INT_CHECKED
#define VENUE_INT_CHECK boost::multiprecision::checked
#else
#define VENUE_INT_CHECK boost::multiprecision::unchecked
#endif
using cpp_int = boost::multiprecision::number<
    boost::multiprecision::cpp_int_backend<
        VENUE_INT_BITS, VENUE_INT_BITS,
        boost::multiprecision::signed_magnitude, VENUE_INT_CHECK, void>>;

inline const cpp_int E18 = cpp_int(10) * 100'000'000 * 1'000'000'000; // 1e18
inline const cpp_int FEE_DENOM = cpp_int(10'000'000'000);             // 1e10

inline cpp_int pow10_i(int n) { cpp_int r = 1; while (n--) r *= 10; return r; }

// Python //: floor division (only used on non-negative paths, where it equals
// truncation); idiv: truncate toward zero — cpp_int's `/` already truncates.
inline cpp_int idiv(const cpp_int& a, const cpp_int& b) { return a / b; }

inline cpp_int iabs(const cpp_int& x) { return x < 0 ? cpp_int(-x) : x; }

// =========================================================================
// StableSwap core
// =========================================================================
inline cpp_int ss_get_D_v1(const std::vector<cpp_int>& xp, const cpp_int& amp, int n) {
    cpp_int S = 0; for (auto& x : xp) S += x;
    if (S == 0) return 0;
    cpp_int D = S, Ann = amp * n;
    for (int it = 0; it < 255; ++it) {
        cpp_int D_P = D;
        for (auto& x : xp) D_P = D_P * D / (x * n);
        cpp_int Dprev = D;
        D = (Ann * S + D_P * n) * D / ((Ann - 1) * D + (n + 1) * D_P);
        if (iabs(D - Dprev) <= 1) return D;
    }
    throw std::runtime_error("ss D no conv");
}

inline cpp_int ss_get_D_ng(const std::vector<cpp_int>& xp, const cpp_int& amp, int n) {
    cpp_int S = 0; for (auto& x : xp) S += x;
    if (S == 0) return 0;
    cpp_int D = S, Ann = amp * n;
    cpp_int nn = 1; for (int k = 0; k < n; ++k) nn *= n;
    for (int it = 0; it < 255; ++it) {
        cpp_int D_P = D;
        for (auto& x : xp) D_P = D_P * D / x;
        D_P /= nn;
        cpp_int Dprev = D;
        D = (Ann * S / 100 + D_P * n) * D / ((Ann - 100) * D / 100 + (n + 1) * D_P);
        if (iabs(D - Dprev) <= 1) return D;
    }
    throw std::runtime_error("ss D no conv");
}

inline cpp_int ss_get_y(int i, int j, const cpp_int& x, const std::vector<cpp_int>& xp,
                        const cpp_int& amp, const cpp_int& D, int n, int a_prec) {
    cpp_int Ann = amp * n;
    cpp_int c = D, S_ = 0;
    for (int k = 0; k < n; ++k) {
        cpp_int xk;
        if (k == i) xk = x;
        else if (k != j) xk = xp[k];
        else continue;
        S_ += xk;
        c = c * D / (xk * n);
    }
    c = c * D * a_prec / (Ann * n);
    cpp_int b = S_ + D * a_prec / Ann;
    cpp_int y = D;
    for (int it = 0; it < 255; ++it) {
        cpp_int y_prev = y;
        y = (y * y + c) / (2 * y + b - D);
        if (iabs(y - y_prev) <= 1) return y;
    }
    throw std::runtime_error("ss y no conv");
}

inline cpp_int ss_dynamic_fee(const cpp_int& xpi, const cpp_int& xpj,
                              const cpp_int& fee, const cpp_int& opm) {
    if (opm <= FEE_DENOM) return fee;
    cpp_int xps2 = (xpi + xpj) * (xpi + xpj);
    return (opm * fee) / ((opm - FEE_DENOM) * 4 * xpi * xpj / xps2 + FEE_DENOM);
}

// =========================================================================
// Tricrypto core
// =========================================================================
inline const cpp_int CBRT_T1 = cpp_int("115792089237316195423570985008687907853269");

inline cpp_int cbrt_u(const cpp_int& x) {
    cpp_int xx; int scale;
    if (x >= CBRT_T1 * E18)      { xx = x;             scale = 0; }
    else if (x >= CBRT_T1)       { xx = x * E18;       scale = 1; }
    else                         { xx = x * E18 * E18; scale = 2; }
    long log2x = static_cast<long>(boost::multiprecision::msb(xx)); // bit_length-1
    long rem = log2x % 3;
    cpp_int p1260 = 1, p1000 = 1;
    for (long k = 0; k < rem; ++k) { p1260 *= 1260; p1000 *= 1000; }
    cpp_int a = (cpp_int(1) << (log2x / 3)) * p1260 / p1000;
    for (int it = 0; it < 7; ++it) a = (2 * a + xx / (a * a)) / 3;
    if (scale == 0) a *= pow10_i(12);
    else if (scale == 1) a *= pow10_i(6);
    return a;
}

inline std::vector<cpp_int> tri_sort_desc(std::vector<cpp_int> x) {
    std::sort(x.begin(), x.end(), [](const cpp_int& a, const cpp_int& b) { return a > b; });
    return x;
}

inline cpp_int tri_geometric_mean(const std::vector<cpp_int>& x) {
    cpp_int prod = x[0] * x[1] / E18 * x[2] / E18;
    return prod != 0 ? cbrt_u(prod) : cpp_int(0);
}

inline cpp_int tri_newton_D(const cpp_int& ANN, const cpp_int& gamma,
                            const std::vector<cpp_int>& x_unsorted,
                            const cpp_int& K0_prev = 0) {
    auto x = tri_sort_desc(x_unsorted);
    if (x[0] <= 0) throw std::runtime_error("empty pool");
    cpp_int S = x[0] + x[1] + x[2];
    cpp_int D;
    if (K0_prev == 0) D = 3 * tri_geometric_mean(x);
    else {
        if (S > pow10_i(36))
            D = cbrt_u(x[0] * x[1] / pow10_i(36) * x[2] / K0_prev * 27 * pow10_i(12));
        else if (S > pow10_i(24))
            D = cbrt_u(x[0] * x[1] / pow10_i(24) * x[2] / K0_prev * 27 * pow10_i(6));
        else
            D = cbrt_u(x[0] * x[1] / E18 * x[2] / K0_prev * 27);
    }
    for (int it = 0; it < 255; ++it) {
        cpp_int D_prev = D;
        cpp_int K0 = E18 * x[0] * 3 / D * x[1] * 3 / D * x[2] * 3 / D;
        cpp_int g1k0 = gamma + E18;
        g1k0 = (g1k0 > K0) ? (g1k0 - K0 + 1) : (K0 - g1k0 + 1);
        cpp_int mul1 = E18 * D / gamma * g1k0 / gamma * g1k0 * 10000 / ANN;
        cpp_int mul2 = (2 * E18 * 3) * K0 / g1k0;
        cpp_int neg_fprime = (S + S * mul2 / E18) + mul1 * 3 / K0 - mul2 * D / E18;
        cpp_int D_plus = D * (neg_fprime + S) / neg_fprime;
        cpp_int D_minus = D * D / neg_fprime;
        if (E18 > K0) D_minus += D * (mul1 / neg_fprime) / E18 * (E18 - K0) / K0;
        else          D_minus -= D * (mul1 / neg_fprime) / E18 * (K0 - E18) / K0;
        D = (D_plus > D_minus) ? cpp_int(D_plus - D_minus) : cpp_int((D_minus - D_plus) / 2);
        cpp_int diff = iabs(D - D_prev);
        cpp_int lim = std::max(D, pow10_i(16));
        if (diff * pow10_i(14) < lim) {
            for (int q = 0; q < 3; ++q) {
                cpp_int frac = x[q] * E18 / D;
                if (!(frac >= pow10_i(16) - 1 && frac < pow10_i(20) + 1))
                    throw std::runtime_error("unsafe frac");
            }
            return D;
        }
    }
    throw std::runtime_error("tri newton_D no conv");
}

inline cpp_int tri_newton_y(const cpp_int& ANN, const cpp_int& gamma,
                            const std::vector<cpp_int>& x, const cpp_int& D, int i,
                            int a_mult = 10000) {
    for (int k = 0; k < 3; ++k) {
        if (k == i) continue;
        cpp_int frac = x[k] * E18 / D;
        if (!(frac > pow10_i(16) - 1 && frac < pow10_i(20) + 1))
            throw std::runtime_error("unsafe x[k]");
    }
    cpp_int y = D / 3;
    cpp_int K0_i = E18;
    cpp_int S_i = 0;
    std::vector<cpp_int> xs = x;
    xs[i] = 0;
    xs = tri_sort_desc(xs);
    cpp_int convergence_limit = std::max(std::max(xs[0] / pow10_i(14), D / pow10_i(14)),
                                         cpp_int(100));
    for (int j = 2; j < 4; ++j) {
        cpp_int _x = xs[3 - j];              // small x first
        y = y * D / (_x * 3);
        S_i += _x;
    }
    for (int j = 0; j < 2; ++j)
        K0_i = K0_i * xs[j] * 3 / D;         // large x first
    for (int it = 0; it < 255; ++it) {
        cpp_int y_prev = y;
        cpp_int K0 = K0_i * y * 3 / D;
        cpp_int S = S_i + y;
        cpp_int g1k0 = gamma + E18;
        g1k0 = (g1k0 > K0) ? (g1k0 - K0 + 1) : (K0 - g1k0 + 1);
        cpp_int mul1 = E18 * D / gamma * g1k0 / gamma * g1k0 * a_mult / ANN;
        cpp_int mul2 = E18 + (2 * E18) * K0 / g1k0;
        cpp_int yfprime = E18 * y + S * mul2 + mul1;
        cpp_int dyfprime = D * mul2;
        if (yfprime < dyfprime) { y = y_prev / 2; continue; }
        yfprime -= dyfprime;
        cpp_int fprime = yfprime / y;
        cpp_int y_minus = mul1 / fprime;
        cpp_int y_plus = (yfprime + E18 * D) / fprime + y_minus * E18 / K0;
        y_minus += E18 * S / fprime;
        y = (y_plus < y_minus) ? cpp_int(y_prev / 2) : cpp_int(y_plus - y_minus);
        cpp_int diff = iabs(y - y_prev);
        if (diff < std::max(convergence_limit, y / pow10_i(14))) {
            cpp_int frac = y * E18 / D;
            if (!(frac > pow10_i(16) - 1 && frac < pow10_i(20) + 1))
                throw std::runtime_error("unsafe y");
            return y;
        }
    }
    throw std::runtime_error("tri newton_y no conv");
}

inline cpp_int tri_get_y(const cpp_int& ANN, const cpp_int& gamma,
                         const std::vector<cpp_int>& x, const cpp_int& D, int i) {
    if (!(D > pow10_i(17) - 1 && D < pow10_i(15) * E18 + 1))
        throw std::runtime_error("unsafe D");
    for (int k = 0; k < 3; ++k) {
        if (k == i) continue;
        cpp_int frac = x[k] * E18 / D;
        if (!(frac > pow10_i(16) - 1 && frac < pow10_i(20) + 1))
            throw std::runtime_error("unsafe x[k]");
    }
    int j, k;
    if (i == 0) { j = 1; k = 2; } else if (i == 1) { j = 0; k = 2; } else { j = 0; k = 1; }
    cpp_int x_j = x[j], x_k = x[k];
    cpp_int gamma2 = gamma * gamma;

    cpp_int a = pow10_i(36) / 27;
    cpp_int b = pow10_i(36) / 9 + 2 * E18 * gamma / 27
        - D * D / x_j * gamma2 * ANN / 729 / 10000 / x_k;
    cpp_int c = pow10_i(36) / 9 + gamma * (gamma + 4 * E18) / 27
        + idiv(idiv(idiv(gamma2 * (x_j + x_k - D), D) * ANN, 27), 10000);
    cpp_int d = (E18 + gamma) * (E18 + gamma) / 27;

    cpp_int d0 = iabs(idiv(3 * a * c, b) - b);
    cpp_int divider = 1;
    {
        struct Th { int t; int dv; };
        static const Th TH[] = {{48, 30}, {44, 26}, {40, 22}, {36, 18},
                                {32, 14}, {28, 10}, {24, 6}, {20, 2}};
        for (auto& th : TH)
            if (d0 > pow10_i(th.t)) { divider = pow10_i(th.dv); break; }
    }

    if (iabs(a) > iabs(b)) {
        cpp_int additional_prec = iabs(idiv(a, b));
        a = idiv(a * additional_prec, divider);
        b = idiv(b * additional_prec, divider);
        c = idiv(c * additional_prec, divider);
        d = idiv(d * additional_prec, divider);
    } else {
        cpp_int additional_prec = iabs(idiv(b, a));
        a = idiv(idiv(a, additional_prec), divider);
        b = idiv(idiv(b, additional_prec), divider);
        c = idiv(idiv(c, additional_prec), divider);
        d = idiv(idiv(d, additional_prec), divider);
    }

    cpp_int _3ac = 3 * a * c;
    cpp_int delta0 = idiv(_3ac, b) - b;
    cpp_int delta1 = idiv(3 * _3ac, b) - 2 * b - idiv(idiv(27 * a * a, b) * d, b);
    cpp_int sqrt_arg = delta1 * delta1 + idiv(4 * delta0 * delta0, b) * delta0;
    if (sqrt_arg <= 0) return tri_newton_y(ANN, gamma, x, D, i);
    cpp_int sqrt_val = boost::multiprecision::sqrt(sqrt_arg);

    cpp_int b_cbrt = (b >= 0) ? cbrt_u(b) : cpp_int(-cbrt_u(-b));
    cpp_int second_cbrt = (delta1 > 0)
        ? cbrt_u((delta1 + sqrt_val) / 2)
        : cpp_int(-cbrt_u((sqrt_val - delta1) / 2));
    cpp_int C1 = idiv(idiv(b_cbrt * b_cbrt, E18) * second_cbrt, E18);
    cpp_int root_K0 = idiv(b + idiv(b * delta0, C1) - C1, 3);
    cpp_int root = idiv(idiv(idiv(idiv(D * D, 27), x_k) * D, x_j) * root_K0, a);

    cpp_int frac = root * E18 / D;
    if (!(frac >= pow10_i(16) - 1 && frac < pow10_i(20) + 1))
        throw std::runtime_error("unsafe y");
    return root;
}

inline cpp_int tri_fee(const std::vector<cpp_int>& xp, const cpp_int& mid_fee,
                       const cpp_int& out_fee, const cpp_int& fee_gamma) {
    cpp_int S = xp[0] + xp[1] + xp[2];
    cpp_int K = E18 * 3 * xp[0] / S;
    K = K * 3 * xp[1] / S;
    K = K * 3 * xp[2] / S;
    if (fee_gamma > 0) K = fee_gamma * E18 / (fee_gamma + E18 - K);
    return (mid_fee * K + out_fee * (E18 - K)) / E18;
}

// =========================================================================
// Cryptoswap N=2 core (cryptoswap_2coin.py — the TS-parity anchor)
// =========================================================================
inline cpp_int c2_geometric_mean(std::vector<cpp_int> x) {
    if (x[0] < x[1]) std::swap(x[0], x[1]);
    cpp_int prod = x[0] * x[1];
    cpp_int z = prod, y = (prod + 1) / 2;
    while (y < z) { z = y; y = (prod / y + y) / 2; }
    return z;
}

inline cpp_int c2_newton_D(const cpp_int& ANN, const cpp_int& gamma,
                           std::vector<cpp_int> x) {
    if (x[0] < x[1]) std::swap(x[0], x[1]);
    cpp_int S = x[0] + x[1];
    cpp_int D = 2 * c2_geometric_mean(x);
    for (int it = 0; it < 255; ++it) {
        cpp_int D_prev = D;
        cpp_int K0 = E18;
        for (auto& xi : x) K0 = K0 * xi * 2 / D;
        cpp_int g1k0 = gamma + E18;
        g1k0 = (g1k0 > K0) ? (g1k0 - K0 + 1) : (K0 - g1k0 + 1);
        cpp_int mul1 = E18 * D / gamma * g1k0 / gamma * g1k0 * 10000 / ANN;
        cpp_int mul2 = 2 * E18 * 2 * K0 / g1k0;
        cpp_int neg_fprime = (S + S * mul2 / E18) + mul1 * 2 / K0 - mul2 * D / E18;
        cpp_int D_plus = D * (neg_fprime + S) / neg_fprime;
        cpp_int D_minus = D * D / neg_fprime;
        if (K0 > E18) D_minus -= D * (mul1 / neg_fprime) / E18 * (K0 - E18) / K0;
        else          D_minus += D * (mul1 / neg_fprime) / E18 * (E18 - K0) / K0;
        D = (D_plus > D_minus) ? cpp_int(D_plus - D_minus) : cpp_int((D_minus - D_plus) / 2);
        cpp_int diff = iabs(D - D_prev);
        if (diff * pow10_i(14) < std::max(pow10_i(16), D)) return D;
    }
    throw std::runtime_error("c2 newton_D no conv");
}

inline cpp_int c2_newton_y(const cpp_int& ANN, const cpp_int& gamma,
                           const std::vector<cpp_int>& x_new, const cpp_int& D, int j) {
    int other = 1 - j;
    cpp_int y = D * D / (4 * x_new[other]);
    for (int it = 0; it < 255; ++it) {
        cpp_int y_prev = y;
        cpp_int xp0 = (other == 0) ? x_new[0] : y;
        cpp_int xp1 = (other == 1) ? x_new[1] : y;
        cpp_int K0 = E18;
        K0 = K0 * xp0 * 2 / D;
        K0 = K0 * xp1 * 2 / D;
        cpp_int g1k0 = gamma + E18;
        g1k0 = (g1k0 > K0) ? (g1k0 - K0 + 1) : (K0 - g1k0 + 1);
        cpp_int mul1 = E18 * D / gamma * g1k0 / gamma * g1k0 * 10000 / ANN;
        cpp_int mul2 = E18 + 2 * E18 * K0 / g1k0;
        cpp_int S = xp0 + xp1;
        cpp_int yfprime = E18 * y + S * mul2 + mul1;
        cpp_int dyfprime = D * mul2;
        if (yfprime < dyfprime) { y = y_prev / 2; continue; }
        yfprime -= dyfprime;
        cpp_int fprime = yfprime / y;
        cpp_int y_minus = mul1 / fprime;
        cpp_int y_plus = (yfprime + E18 * D) / fprime + y_minus * E18 / K0;
        y_minus += E18 * S / fprime;
        y = (y_plus < y_minus) ? cpp_int(y_prev / 2) : cpp_int(y_plus - y_minus);
        cpp_int diff = iabs(y - y_prev);
        if (diff * pow10_i(14) < std::max(pow10_i(16), y)) return y;
    }
    throw std::runtime_error("c2 newton_y no conv");
}

inline cpp_int c2_dynamic_fee(const cpp_int& mid_fee, const cpp_int& out_fee,
                              const cpp_int& fee_gamma, const std::vector<cpp_int>& xp) {
    cpp_int S = xp[0] + xp[1];
    if (S == 0) return mid_fee;
    cpp_int K = 4 * E18 * xp[0] / S * xp[1] / S;
    cpp_int f_denom = fee_gamma * E18 / (fee_gamma + E18 - K);
    return (mid_fee * f_denom + out_fee * (E18 - f_denom)) / E18;
}

// =========================================================================
// Venue interface + classes
// =========================================================================
struct Venue {
    virtual ~Venue() = default;
    virtual cpp_int get_dy(int i, int j, const cpp_int& dx) const = 0;
    virtual cpp_int exec(int i, int j, const cpp_int& dx) = 0;
    virtual std::vector<cpp_int> pair_balances() const = 0;
    virtual std::unique_ptr<Venue> clone() const = 0;
};

struct StableswapVenue : Venue {
    bool ng;
    cpp_int amp, fee, offpeg;
    std::vector<cpp_int> rates, balances;

    StableswapVenue(const cpp_int& tvl, const cpp_int& price, double A_ui,
                    bool ng_, const cpp_int& fee_, const cpp_int& offpeg_)
        : ng(ng_), fee(fee_), offpeg(offpeg_) {
        amp = cpp_int(static_cast<long long>(ng_ ? A_ui * 100 + 0.5 : A_ui + 0.5));
        rates = {E18, price};
        balances = {tvl / 2, tvl * E18 / (2 * price)};
    }

    std::vector<cpp_int> xp() const {
        return {rates[0] * balances[0] / E18, rates[1] * balances[1] / E18};
    }

    cpp_int get_dy(int i, int j, const cpp_int& dx) const override {
        if (dx <= 0) throw std::runtime_error("dx<=0");
        auto xps = xp();
        cpp_int x = xps[i] + dx * rates[i] / E18;
        if (ng) {
            cpp_int D = ss_get_D_ng(xps, amp, 2);
            cpp_int y = ss_get_y(i, j, x, xps, amp, D, 2, 100);
            cpp_int dy = xps[j] - y - 1;
            cpp_int f = ss_dynamic_fee((xps[i] + x) / 2, (xps[j] + y) / 2,
                                       fee, offpeg) * dy / FEE_DENOM;
            return (dy - f) * E18 / rates[j];
        }
        cpp_int D = ss_get_D_v1(xps, amp, 2);
        cpp_int y = ss_get_y(i, j, x, xps, amp, D, 2, 1);
        cpp_int dy = (xps[j] - y - 1) * E18 / rates[j];
        return dy - fee * dy / FEE_DENOM;
    }

    cpp_int exec(int i, int j, const cpp_int& dx) override {
        cpp_int dy = get_dy(i, j, dx);
        if (dy >= balances[j]) throw std::runtime_error("venue side depleted");
        balances[i] += dx;
        balances[j] -= dy;
        return dy;
    }

    std::vector<cpp_int> pair_balances() const override { return balances; }
    std::unique_ptr<Venue> clone() const override {
        return std::make_unique<StableswapVenue>(*this);
    }
};

struct Cryptoswap3Venue : Venue {
    cpp_int ANN, gamma, mid_fee, out_fee, fee_gamma, D;
    std::vector<cpp_int> price_scale, balances;
    // external 0 -> internal 0, external 1 -> internal 2

    Cryptoswap3Venue(const cpp_int& tvl, const cpp_int& price, double A_raw)
        : gamma(1'300'000'000'000LL), mid_fee(3'000'000), out_fee(80'000'000),
          fee_gamma(350'000'000'000'000LL) {
        ANN = cpp_int(static_cast<long long>(A_raw * 27 * 10000 + 0.5));
        price_scale = {E18, price};
        cpp_int half = tvl / 2;
        balances = {half, half, tvl * E18 / (2 * price)};
        D = tri_newton_D(ANN, gamma, xr());
    }

    std::vector<cpp_int> xr() const {
        return {balances[0],
                balances[1] * price_scale[0] / E18,
                balances[2] * price_scale[1] / E18};
    }

    cpp_int get_dy(int i, int j, const cpp_int& dx) const override {
        if (dx <= 0) throw std::runtime_error("dx<=0");
        int ii = (i == 0) ? 0 : 2, jj = (j == 0) ? 0 : 2;
        std::vector<cpp_int> xp = balances;
        xp[ii] += dx;
        for (int k = 0; k < 2; ++k) xp[k + 1] = xp[k + 1] * price_scale[k] / E18;
        cpp_int y = tri_get_y(ANN, gamma, xp, D, jj);
        cpp_int dy = xp[jj] - y - 1;
        xp[jj] = y;
        if (jj > 0) dy = dy * E18 / price_scale[jj - 1];
        cpp_int f = tri_fee(xp, mid_fee, out_fee, fee_gamma) * dy / FEE_DENOM;
        return dy - f;
    }

    cpp_int exec(int i, int j, const cpp_int& dx) override {
        cpp_int dy = get_dy(i, j, dx);
        int ii = (i == 0) ? 0 : 2, jj = (j == 0) ? 0 : 2;
        if (dy >= balances[jj]) throw std::runtime_error("venue side depleted");
        balances[ii] += dx;
        balances[jj] -= dy;
        D = tri_newton_D(ANN, gamma, xr());
        return dy;
    }

    std::vector<cpp_int> pair_balances() const override {
        return {balances[0], balances[2]};
    }
    std::unique_ptr<Venue> clone() const override {
        return std::make_unique<Cryptoswap3Venue>(*this);
    }
};

struct Cryptoswap2Venue : Venue {
    cpp_int ANN, gamma, mid_fee, out_fee, fee_gamma, price_scale, D;
    std::vector<cpp_int> balances;

    Cryptoswap2Venue(const cpp_int& tvl, const cpp_int& price, double A_raw)
        : gamma(1'300'000'000'000LL), mid_fee(3'000'000), out_fee(80'000'000),
          fee_gamma(350'000'000'000'000LL), price_scale(price) {
        ANN = cpp_int(static_cast<long long>(A_raw * 4 * 10000 + 0.5));
        balances = {tvl / 2, tvl * E18 / (2 * price)};
        D = c2_newton_D(ANN, gamma, xp(balances));
    }

    std::vector<cpp_int> xp(const std::vector<cpp_int>& b) const {
        return {b[0], b[1] * price_scale / E18};
    }

    cpp_int get_dy(int i, int j, const cpp_int& dx) const override {
        if (dx <= 0) throw std::runtime_error("dx<=0");
        std::vector<cpp_int> nb = balances;
        nb[i] += dx;
        auto xps = xp(nb);
        cpp_int y_new = c2_newton_y(ANN, gamma, xps, D, j);
        cpp_int after = (j == 1) ? cpp_int(y_new * E18 / price_scale) : y_new;
        cpp_int dy = balances[j] - after;
        std::vector<cpp_int> xp_after = xps;
        xp_after[j] = y_new;
        cpp_int f = c2_dynamic_fee(mid_fee, out_fee, fee_gamma, xp_after);
        return dy - dy * f / FEE_DENOM;
    }

    cpp_int exec(int i, int j, const cpp_int& dx) override {
        cpp_int dy = get_dy(i, j, dx);
        balances[i] += dx;
        balances[j] -= dy;
        D = c2_newton_D(ANN, gamma, xp(balances));
        return dy;
    }

    std::vector<cpp_int> pair_balances() const override { return balances; }
    std::unique_ptr<Venue> clone() const override {
        return std::make_unique<Cryptoswap2Venue>(*this);
    }
};

// Real-pool defaults (verify_venues.py): crvUSD/USDT NG fee/offpeg, 3pool fee.
inline std::unique_ptr<Venue> make_venue(const std::string& pool_type, int n_coins,
                                         const cpp_int& tvl, const cpp_int& price,
                                         double A_ui, long fee_1e10 = 0) {
    // fee_1e10 == 0 keeps each type's verified default (1bp stableswap).
    cpp_int ss_fee = fee_1e10 > 0 ? cpp_int(fee_1e10) : cpp_int(1'000'000);
    if (pool_type == "cryptoswap") {
        if (n_coins == 2) return std::make_unique<Cryptoswap2Venue>(tvl, price, A_ui);
        if (n_coins == 3) return std::make_unique<Cryptoswap3Venue>(tvl, price, A_ui);
        throw std::runtime_error("cryptoswap: 2 or 3 coins only");
    }
    if (pool_type == "stableswap-ng")
        return std::make_unique<StableswapVenue>(tvl, price, A_ui, true,
                                                 ss_fee,
                                                 cpp_int(50'000'000'000LL));
    if (pool_type == "stableswap")
        return std::make_unique<StableswapVenue>(tvl, price, A_ui, false,
                                                 ss_fee, cpp_int(0));
    throw std::runtime_error("unknown pool_type " + pool_type);
}


// =============================================================================
// State-seeded venues — the real pool, not a balanced reconstruction.
// 2-coin math ported from global-sim-ui/engine-cpp/src/crypto_math.hpp
// (two_newton_D / two_get_y / ss_newton_D / ss_get_y), which was validated
// 484/484 against on-chain get_dy. The Yield Basis pools present a twocrypto
// interface but run StableswapMath inside; the family is detected in Python by
// which invariant reproduces the pool's own stored D() and arrives as `math`.
// =============================================================================

inline cpp_int isqrt_u(const cpp_int& n) {
    if (n <= 0) return 0;
    cpp_int x = n, y = (x + 1) / 2;
    while (y < x) { x = y; y = (x + n / x) / 2; }
    return x;
}

inline cpp_int two_newton_D(const cpp_int& ANN, const cpp_int& gamma,
                            const std::vector<cpp_int>& xu) {
    cpp_int x0 = xu[0], x1 = xu[1];
    if (x0 < x1) std::swap(x0, x1);
    if (!(x0 > pow10_i(9) - 1 && x0 < pow10_i(15) * E18 + 1))
        throw std::runtime_error("unsafe x0");
    if (x1 * E18 / x0 <= pow10_i(14) - 1) throw std::runtime_error("unsafe ratio");
    cpp_int S = x0 + x1;
    cpp_int D = 2 * isqrt_u(x0 * x1);
    const cpp_int g1k0_base = gamma + E18;
    for (int it = 0; it < 255; ++it) {
        cpp_int D_prev = D;
        if (D <= 0) throw std::runtime_error("D=0");
        cpp_int K0 = (E18 * 4) * x0 / D * x1 / D;
        cpp_int g1k0 = (g1k0_base > K0) ? cpp_int(g1k0_base - K0 + 1)
                                        : cpp_int(K0 - g1k0_base + 1);
        cpp_int mul1 = E18 * D / gamma * g1k0 / gamma * g1k0 * 10000 / ANN;
        cpp_int mul2 = (2 * E18 * 2) * K0 / g1k0;
        cpp_int neg_fprime = (S + S * mul2 / E18) + mul1 * 2 / K0 - mul2 * D / E18;
        cpp_int D_plus = D * (neg_fprime + S) / neg_fprime;
        cpp_int D_minus = D * D / neg_fprime;
        if (E18 > K0) D_minus += D * (mul1 / neg_fprime) / E18 * (E18 - K0) / K0;
        else          D_minus -= D * (mul1 / neg_fprime) / E18 * (K0 - E18) / K0;
        D = (D_plus > D_minus) ? cpp_int(D_plus - D_minus)
                               : cpp_int((D_minus - D_plus) / 2);
        cpp_int lim = D > pow10_i(16) ? D : pow10_i(16);
        if (iabs(D - D_prev) * pow10_i(14) < lim) return D;
    }
    throw std::runtime_error("two newton_D no conv");
}

inline cpp_int two_newton_y(const cpp_int& ANN, const cpp_int& gamma,
                            const std::vector<cpp_int>& x, const cpp_int& D,
                            int i, const cpp_int& lim_mul) {
    const cpp_int x_j = x[1 - i];
    cpp_int y = D * D / (x_j * 4);
    cpp_int K0_i = (E18 * 2) * x_j / D;
    if (!(K0_i >= E18 * E18 / lim_mul && K0_i <= lim_mul))
        throw std::runtime_error("unsafe x[i]");
    cpp_int conv = x_j / pow10_i(14);
    { cpp_int t = D / pow10_i(14); if (t > conv) conv = t; }
    if (conv < 100) conv = 100;
    for (int it = 0; it < 255; ++it) {
        cpp_int y_prev = y;
        cpp_int K0 = K0_i * y * 2 / D;
        cpp_int S = x_j + y;
        cpp_int g1k0 = gamma + E18;
        g1k0 = (g1k0 > K0) ? cpp_int(g1k0 - K0 + 1) : cpp_int(K0 - g1k0 + 1);
        cpp_int mul1 = E18 * D / gamma * g1k0 / gamma * g1k0 * 10000 / ANN;
        cpp_int mul2 = E18 + (2 * E18) * K0 / g1k0;
        cpp_int yfprime = E18 * y + S * mul2 + mul1;
        cpp_int dyfprime = D * mul2;
        if (yfprime < dyfprime) { y = y_prev / 2; continue; }
        yfprime -= dyfprime;
        cpp_int fprime = yfprime / y;
        cpp_int y_minus = mul1 / fprime;
        cpp_int y_plus = (yfprime + E18 * D) / fprime + y_minus * E18 / K0;
        y_minus += E18 * S / fprime;
        y = (y_plus < y_minus) ? cpp_int(y_prev / 2) : cpp_int(y_plus - y_minus);
        cpp_int lim = y / pow10_i(14); if (lim < conv) lim = conv;
        if (iabs(y - y_prev) < lim) return y;
    }
    throw std::runtime_error("two newton_y no conv");
}

inline cpp_int two_get_y(const cpp_int& ANN, const cpp_int& gamma,
                         const std::vector<cpp_int>& x, const cpp_int& D, int i,
                         const cpp_int& max_gamma_small) {
    if (!(D > pow10_i(17) - 1 && D < pow10_i(15) * E18 + 1))
        throw std::runtime_error("unsafe D");
    cpp_int lim_mul = 100 * E18;
    if (max_gamma_small != 0 && gamma > max_gamma_small)
        lim_mul = lim_mul * max_gamma_small / gamma;
    const cpp_int x_j = x[1 - i];
    const cpp_int gamma2 = gamma * gamma;
    cpp_int K0_i = E18 * 2 * x_j / D;
    if (max_gamma_small != 0) {
        if (!(K0_i >= E18 * E18 / lim_mul && K0_i <= lim_mul))
            throw std::runtime_error("unsafe x[i]");
    } else if (!(K0_i > pow10_i(16) * 2 - 1 && K0_i < pow10_i(20) * 2 + 1)) {
        throw std::runtime_error("unsafe x[i]");
    }
    cpp_int ag2 = ANN * gamma2;
    cpp_int a = pow10_i(32);
    cpp_int b = idiv(D * ag2 / 400000000, x_j) - pow10_i(32) * 3 - 2 * gamma * pow10_i(14);
    cpp_int c = pow10_i(32) * 3 + 4 * gamma * pow10_i(14) + gamma2 / 10000
              + idiv((4 * ag2 / 400000000) * x_j, D) - 4 * ag2 / 400000000;
    cpp_int d = -((E18 + gamma) * (E18 + gamma)) / 10000;
    cpp_int delta0 = idiv(3 * a * c, b) - b;
    cpp_int delta1 = 3 * delta0 + b - idiv(idiv(27 * a * a, b) * d, b);
    cpp_int thr = iabs(delta0);
    { cpp_int t = iabs(delta1); if (t < thr) thr = t; if (a < thr) thr = a; }
    cpp_int divider = 1;
    const int ex[] = {48,46,44,42,40,38,36,34,32,30,28,26,24,20};
    const int dv[] = {30,28,26,24,22,20,18,16,14,12,10, 8, 6, 2};
    for (int k = 0; k < 14; ++k)
        if (thr > pow10_i(ex[k])) { divider = pow10_i(dv[k]); break; }
    a = idiv(a, divider); b = idiv(b, divider);
    c = idiv(c, divider); d = idiv(d, divider);
    delta0 = idiv(3 * a * c, b) - b;
    delta1 = 3 * delta0 + b - idiv(idiv(27 * a * a, b) * d, b);
    cpp_int sqrt_arg = delta1 * delta1 + idiv(4 * delta0 * delta0, b) * delta0;
    if (sqrt_arg <= 0) return two_newton_y(ANN, gamma, x, D, i, lim_mul);
    cpp_int sqrt_val = isqrt_u(sqrt_arg);
    cpp_int b_cbrt = (b > 0) ? cbrt_u(b) : cpp_int(-cbrt_u(cpp_int(-b)));
    cpp_int second = (delta1 > 0) ? cbrt_u(cpp_int((delta1 + sqrt_val) / 2))
                                  : cpp_int(-cbrt_u(cpp_int((sqrt_val - delta1) / 2)));
    cpp_int C1 = idiv(idiv(b_cbrt * b_cbrt, E18) * second, E18);
    cpp_int root = idiv(E18 * C1 - E18 * b - idiv(E18 * b, C1) * delta0, 3 * a);
    return idiv(idiv(idiv(D * D, x_j) * root, 4), E18);
}

inline cpp_int ss2_get_D(const cpp_int& amp, const std::vector<cpp_int>& xp) {
    cpp_int S = xp[0] + xp[1];
    if (S == 0) return 0;
    cpp_int D = S, Ann = amp * 2;
    for (int it = 0; it < 255; ++it) {
        cpp_int D_P = D;
        for (int k = 0; k < 2; ++k) D_P = D_P * D / xp[k];
        D_P /= 4;
        cpp_int Dprev = D;
        D = (Ann * S / 10000 + D_P * 2) * D / ((Ann - 10000) * D / 10000 + 3 * D_P);
        if (iabs(D - Dprev) <= 1) return D;
    }
    throw std::runtime_error("ss2 D no conv");
}

inline cpp_int ss2_get_y(const cpp_int& amp, const std::vector<cpp_int>& xp,
                         const cpp_int& D, int i) {
    cpp_int S_ = 0, c = D, Ann = amp * 2;
    for (int k = 0; k < 2; ++k) {
        if (k == i) continue;
        S_ += xp[k];
        c = c * D / (xp[k] * 2);
    }
    c = c * D * 10000 / (Ann * 2);
    cpp_int b = S_ + D * 10000 / Ann;
    cpp_int y = D;
    for (int it = 0; it < 255; ++it) {
        cpp_int y_prev = y;
        y = (y * y + c) / (2 * y + b - D);
        if (iabs(y - y_prev) <= 1) return y;
    }
    throw std::runtime_error("ss2 y no conv");
}

struct Crypto2StateVenue : Venue {
    std::vector<cpp_int> balances;
    cpp_int price_scale, ANN, gamma, mid_fee, out_fee, fee_gamma, D;
    std::string math;

    std::vector<cpp_int> xpv(const std::vector<cpp_int>& b) const {
        return {b[0], b[1] * price_scale / E18};
    }
    cpp_int solveD(const std::vector<cpp_int>& xp) const {
        return (math == "ss") ? ss2_get_D(ANN, xp) : two_newton_D(ANN, gamma, xp);
    }
    cpp_int solveY(const std::vector<cpp_int>& xp, int i) const {
        if (math == "ss") return ss2_get_y(ANN, xp, D, i);
        return two_get_y(ANN, gamma, xp, D, i,
                         math == "old2" ? cpp_int(0) : cpp_int(2) * pow10_i(16));
    }
    cpp_int feeOf(const std::vector<cpp_int>& xp) const {
        cpp_int S = xp[0] + xp[1];
        if (S == 0) return mid_fee;
        cpp_int K = E18 * 2 * xp[0] / S * 2 * xp[1] / S;
        if (fee_gamma > 0) K = fee_gamma * E18 / (fee_gamma + E18 - K);
        return (mid_fee * K + out_fee * (E18 - K)) / E18;
    }
    cpp_int get_dy(int i, int j, const cpp_int& dx) const override {
        if (dx <= 0) throw std::runtime_error("dx<=0");
        std::vector<cpp_int> b = balances; b[i] += dx;
        std::vector<cpp_int> xp = xpv(b);
        cpp_int y = solveY(xp, j);
        cpp_int dy = xp[j] - y - 1;
        xp[j] = y;
        if (j == 1) dy = dy * E18 / price_scale;
        return dy - feeOf(xp) * dy / FEE_DENOM;
    }
    cpp_int exec(int i, int j, const cpp_int& dx) override {
        cpp_int dy = get_dy(i, j, dx);
        if (dy >= balances[j]) throw std::runtime_error("venue side depleted");
        balances[i] += dx; balances[j] -= dy;
        D = solveD(xpv(balances));
        return dy;
    }
    std::vector<cpp_int> pair_balances() const override { return balances; }
    std::unique_ptr<Venue> clone() const override {
        return std::make_unique<Crypto2StateVenue>(*this);
    }
};

struct Tri3StateVenue : Venue {
    std::vector<cpp_int> balances, price_scale;   // pool order; scale for 1,2
    int i_q = 0, i_b = 2;
    cpp_int q_usd, ANN, gamma, mid_fee, out_fee, fee_gamma, D;

    std::vector<cpp_int> xr(const std::vector<cpp_int>& b) const {
        return {b[0], b[1] * price_scale[0] / E18, b[2] * price_scale[1] / E18};
    }
    cpp_int raw_dy(int ii, int jj, const cpp_int& dx) const {
        std::vector<cpp_int> xp = balances;
        xp[ii] += dx;
        for (int k = 0; k < 2; ++k) xp[k + 1] = xp[k + 1] * price_scale[k] / E18;
        cpp_int y = tri_get_y(ANN, gamma, xp, D, jj);
        cpp_int dy = xp[jj] - y - 1;
        xp[jj] = y;
        if (jj > 0) dy = dy * E18 / price_scale[jj - 1];
        return dy - tri_fee(xp, mid_fee, out_fee, fee_gamma) * dy / FEE_DENOM;
    }
    cpp_int get_dy(int i, int j, const cpp_int& dx) const override {
        if (dx <= 0) throw std::runtime_error("dx<=0");
        if (i == 1) return raw_dy(i_b, i_q, dx) * q_usd / E18;
        (void)j;
        return raw_dy(i_q, i_b, dx * E18 / q_usd);
    }
    cpp_int exec(int i, int j, const cpp_int& dx) override {
        cpp_int dy = get_dy(i, j, dx);
        int ii, jj; cpp_int dxr, dyr;
        if (i == 1) { ii = i_b; jj = i_q; dxr = dx; dyr = dy * E18 / q_usd; }
        else        { ii = i_q; jj = i_b; dxr = dx * E18 / q_usd; dyr = dy; }
        if (dyr >= balances[jj]) throw std::runtime_error("venue side depleted");
        balances[ii] += dxr; balances[jj] -= dyr;
        D = tri_newton_D(ANN, gamma, xr(balances));
        return dy;
    }
    std::vector<cpp_int> pair_balances() const override {
        return {balances[i_q] * q_usd / E18, balances[i_b]};
    }
    std::unique_ptr<Venue> clone() const override {
        return std::make_unique<Tri3StateVenue>(*this);
    }
};

// Venue rebuilt from a REAL pool's live state (venues.venue_from_state). The
// caller resolves the state into these six numbers in Python, so both engines
// start from bit-identical balances instead of each re-deriving them.
inline std::unique_ptr<Venue> stableswap_from_state(
        bool ng, const cpp_int& amp, const cpp_int& fee, const cpp_int& offpeg,
        const cpp_int& bal0, const cpp_int& bal1, const cpp_int& rate1) {
    auto v = std::make_unique<StableswapVenue>(2 * E18, E18, 1.0, ng, fee, offpeg);
    v->amp = amp;                       // already carries A_PRECISION for ng
    v->rates = {E18, rate1};
    v->balances = {bal0, bal1};
    return v;
}

// Marginal price of collateral in crvUSD (synth_bad_debt.artificial_crv_spot).
inline double venue_spot(const Venue& p) {
    static const cpp_int tiny = pow10_i(15); // 0.001 collateral
    cpp_int dy = p.get_dy(1, 0, tiny);
    return dy.convert_to<double>() / tiny.convert_to<double>();
}

// synth_bad_debt.push_pool_to_spot — one-time init snap to the start spot.
inline void push_pool_to_spot(Venue& pool, double target_spot, double tolerance = 5e-4) {
    double cur = venue_spot(pool);
    if (std::abs(cur - target_spot) < tolerance) return;
    int direction = (cur > target_spot) ? +1 : -1;
    int i = (direction == +1) ? 1 : 0, j = 1 - i;
    cpp_int lo = 1, hi = pool.pair_balances()[direction == +1 ? 1 : 0] * 3;
    cpp_int best = 0;
    for (int it = 0; it < 60; ++it) {
        if (hi <= lo) break;
        cpp_int mid = (lo + hi) / 2;
        double spot_mid;
        try {
            auto c = pool.clone();
            c->exec(i, j, mid);
            spot_mid = venue_spot(*c);
        } catch (...) { hi = mid; continue; }
        if (direction == +1) {
            if (spot_mid > target_spot) lo = mid + 1;
            else { hi = mid; best = mid; }
        } else {
            if (spot_mid < target_spot) lo = mid + 1;
            else { hi = mid; best = mid; }
        }
    }
    if (best == 0) return;
    pool.exec(i, j, best);
}

} // namespace venue
