// Exact-integer cryptoswap math: twocrypto-ng (Math v2.0.0 / v2.1.0),
// StableswapMath twocrypto shell (Yield Basis pools), tricrypto-ng.
//
// Ported line-by-line from the verified deployed Vyper sources in
// ../reference/. Signed division truncates toward zero (cpp_int matches EVM
// SDIV); unsigned division floors. Vyper asserts are thrown (mirroring
// reverts) so the engine refuses trades the chain would refuse.
#pragma once
#include <boost/multiprecision/cpp_int.hpp>
#include <stdexcept>
#include <vector>

namespace cm {

using I = boost::multiprecision::cpp_int;   // used for both int256/uint256 roles

inline I E18() { static I v("1000000000000000000"); return v; }
inline I pow_int(I b, int e) { I r = 1; for (int k = 0; k < e; ++k) r *= b; return r; }
inline I absI(const I& x) { return x < 0 ? I(-x) : x; }
inline I isqrt_u(const I& x) { return boost::multiprecision::sqrt(x); }
inline void req(bool ok, const char* msg) { if (!ok) throw std::runtime_error(msg); }

// snekmate log2, round-down variant (roundup never used by _cbrt)
inline unsigned snek_log2(const I& x) {
    I value = x; unsigned result = 0;
    if ((x >> 128) != 0) { value = x >> 128; result = 128; }
    if ((value >> 64) != 0) { value >>= 64; result += 64; }
    if ((value >> 32) != 0) { value >>= 32; result += 32; }
    if ((value >> 16) != 0) { value >>= 16; result += 16; }
    if ((value >> 8) != 0) { value >>= 8; result += 8; }
    if ((value >> 4) != 0) { value >>= 4; result += 4; }
    if ((value >> 2) != 0) { value >>= 2; result += 2; }
    if ((value >> 1) != 0) { result += 1; }
    return result;
}

// identical in twocrypto + tricrypto math
inline I cbrt_u(const I& x) {
    static const I T1("115792089237316195423570985008687907853269");  // 2^256/1e36
    I xx;
    int scale = 0;   // 0: *1e12 at end, 1: *1e6, 2: *1
    if (x >= T1 * E18()) { xx = x; scale = 0; }
    else if (x >= T1)    { xx = x * E18(); scale = 1; }
    else                 { xx = x * E18() * E18(); scale = 2; }

    unsigned log2x = snek_log2(xx);
    unsigned rem = log2x % 3;
    I a = (I(1) << (log2x / 3)) * pow_int(1260, rem) / pow_int(1000, rem);
    for (int k = 0; k < 7; ++k) a = (2 * a + xx / (a * a)) / 3;

    if (scale == 0) a *= I("1000000000000");
    else if (scale == 1) a *= 1000000;
    return a;
}

// ---------------------------------------------------------------- twocrypto
// N_COINS = 2, A_MULTIPLIER = 10000

struct TwoVersion { I MAX_GAMMA_SMALL, MAX_GAMMA; };
// v2.0.0: MAX_GAMMA = 2e15 (no MAX_GAMMA_SMALL concept -> lim behaves like old asserts)
// v2.1.0: MAX_GAMMA_SMALL = 2e16, MAX_GAMMA = 1.99e17

inline I two_newton_y(const I& ANN, const I& gamma, const I x[2], const I& D,
                      int i, const I& lim_mul) {
    const I x_j = x[1 - i];
    I y = D * D / (x_j * 4);
    I K0_i = (E18() * 2) * x_j / D;
    req(K0_i >= (E18() * E18() / lim_mul) && K0_i <= lim_mul, "unsafe x[i]");

    I convergence_limit = x_j / I("100000000000000");
    { I t = D / I("100000000000000"); if (t > convergence_limit) convergence_limit = t; }
    if (convergence_limit < 100) convergence_limit = 100;

    for (int it = 0; it < 255; ++it) {
        I y_prev = y;
        I K0 = K0_i * y * 2 / D;
        I S = x_j + y;
        I g1k0 = gamma + E18();
        g1k0 = (g1k0 > K0) ? (g1k0 - K0 + 1) : (K0 - g1k0 + 1);
        I mul1 = E18() * D / gamma * g1k0 / gamma * g1k0 * 10000 / ANN;
        I mul2 = E18() + (2 * E18()) * K0 / g1k0;
        I yfprime = E18() * y + S * mul2 + mul1;
        I dyfprime = D * mul2;
        if (yfprime < dyfprime) { y = y_prev / 2; continue; }
        yfprime -= dyfprime;
        I fprime = yfprime / y;
        I y_minus = mul1 / fprime;
        I y_plus = (yfprime + E18() * D) / fprime + y_minus * E18() / K0;
        y_minus += E18() * S / fprime;
        if (y_plus < y_minus) y = y_prev / 2;
        else y = y_plus - y_minus;
        I diff = (y > y_prev) ? I(y - y_prev) : I(y_prev - y);
        I lim = y / I("100000000000000"); if (lim < convergence_limit) lim = convergence_limit;
        if (diff < lim) return y;
    }
    throw std::runtime_error("newton_y no conv");
}

// analytic get_y; returns {y, K0_prev}
inline void two_get_y(const I& _ANN, const I& _gamma, const I _x[2], const I& _D,
                      int i, const TwoVersion& V, I out[2]) {
    req(_D > pow_int(10, 17) - 1 && _D < pow_int(10, 15) * E18() + 1, "unsafe D");
    I lim_mul = 100 * E18();
    if (V.MAX_GAMMA_SMALL != 0 && _gamma > V.MAX_GAMMA_SMALL)
        lim_mul = lim_mul * V.MAX_GAMMA_SMALL / _gamma;

    const I ANN = _ANN, gamma = _gamma, D = _D;
    const I x_j = _x[1 - i];
    const I gamma2 = gamma * gamma;

    I y = D * D / (x_j * 4);
    I K0_i = E18() * 2 * x_j / D;   // trunc; positive
    if (V.MAX_GAMMA_SMALL != 0) {
        req(K0_i >= E18() * E18() / lim_mul && K0_i <= lim_mul, "unsafe x[i]");
    } else {
        // v2.0.0 asserts: K0_i in (1e16*2, 1e20*2)
        req(K0_i > pow_int(10, 16) * 2 - 1 && K0_i < pow_int(10, 20) * 2 + 1, "unsafe x[i]");
    }

    I ann_gamma2 = ANN * gamma2;
    I a = pow_int(10, 32);
    I b = D * ann_gamma2 / 400000000 / x_j
          - pow_int(10, 32) * 3
          - 2 * gamma * pow_int(10, 14);
    I c = pow_int(10, 32) * 3
          + 4 * gamma * pow_int(10, 14)
          + gamma2 / 10000
          + (4 * ann_gamma2 / 400000000) * x_j / D
          - 4 * ann_gamma2 / 400000000;
    I d = -((E18() + gamma) * (E18() + gamma)) / 10000;

    I delta0 = 3 * a * c / b - b;
    I delta1 = 3 * delta0 + b - 27 * a * a / b * d / b;

    I divider = 1;
    I threshold = absI(delta0); { I t = absI(delta1); if (t < threshold) threshold = t; }
    if (a < threshold) threshold = a;
    if      (threshold > pow_int(10, 48)) divider = pow_int(10, 30);
    else if (threshold > pow_int(10, 46)) divider = pow_int(10, 28);
    else if (threshold > pow_int(10, 44)) divider = pow_int(10, 26);
    else if (threshold > pow_int(10, 42)) divider = pow_int(10, 24);
    else if (threshold > pow_int(10, 40)) divider = pow_int(10, 22);
    else if (threshold > pow_int(10, 38)) divider = pow_int(10, 20);
    else if (threshold > pow_int(10, 36)) divider = pow_int(10, 18);
    else if (threshold > pow_int(10, 34)) divider = pow_int(10, 16);
    else if (threshold > pow_int(10, 32)) divider = pow_int(10, 14);
    else if (threshold > pow_int(10, 30)) divider = pow_int(10, 12);
    else if (threshold > pow_int(10, 28)) divider = pow_int(10, 10);
    else if (threshold > pow_int(10, 26)) divider = pow_int(10, 8);
    else if (threshold > pow_int(10, 24)) divider = pow_int(10, 6);
    else if (threshold > pow_int(10, 20)) divider = pow_int(10, 2);

    // signed truncating division toward zero (cpp_int default) matches Vyper
    a /= divider; b /= divider; c /= divider; d /= divider;

    delta0 = 3 * a * c / b - b;
    delta1 = 3 * delta0 + b - 27 * a * a / b * d / b;

    I sqrt_arg = delta1 * delta1 + 4 * delta0 * delta0 / b * delta0;
    if (sqrt_arg <= 0) {
        out[0] = two_newton_y(_ANN, _gamma, _x, _D, i, lim_mul);
        out[1] = 0;
        return;
    }
    I sqrt_val = isqrt_u(sqrt_arg);

    I b_cbrt = (b > 0) ? cbrt_u(b) : I(-cbrt_u(-b));
    I second_cbrt;
    if (delta1 > 0) second_cbrt = cbrt_u((delta1 + sqrt_val) / 2);
    else            second_cbrt = -cbrt_u((sqrt_val - delta1) / 2);

    I C1 = (b_cbrt * b_cbrt / E18()) * second_cbrt / E18();
    I root = (E18() * C1 - E18() * b - E18() * b / C1 * delta0) / (3 * a);

    I y_out0 = (D * D / x_j) * root / 4 / E18();
    out[0] = y_out0;
    out[1] = root;

    I frac = y_out0 * E18() / _D;
    if (V.MAX_GAMMA_SMALL != 0) {
        req(frac >= (E18() * E18() / 2) / lim_mul && frac <= lim_mul / 2, "unsafe y");
    } else {
        req(frac >= pow_int(10, 16) - 1 && frac < pow_int(10, 20) + 1, "unsafe y");
    }
}

inline I two_newton_D(const I& ANN, const I& gamma, const I x_unsorted[2],
                      const I& K0_prev) {
    I x[2] = {x_unsorted[0], x_unsorted[1]};
    if (x[0] < x[1]) { x[0] = x_unsorted[1]; x[1] = x_unsorted[0]; }
    req(x[0] > pow_int(10, 9) - 1 && x[0] < pow_int(10, 15) * E18() + 1, "unsafe x0");
    req(x[1] * E18() / x[0] > pow_int(10, 14) - 1, "unsafe ratio");

    I S = x[0] + x[1];
    I D;
    if (K0_prev == 0) {
        D = 2 * isqrt_u(x[0] * x[1]);
    } else {
        D = isqrt_u(4 * x[0] * x[1] / K0_prev * E18());
        if (S < D) D = S;
    }
    I g1k0_base = gamma + E18();

    for (int it = 0; it < 255; ++it) {
        I D_prev = D;
        req(D > 0, "D=0");
        I K0 = (E18() * 4) * x[0] / D * x[1] / D;
        I g1k0 = g1k0_base;
        g1k0 = (g1k0 > K0) ? (g1k0 - K0 + 1) : (K0 - g1k0 + 1);
        I mul1 = E18() * D / gamma * g1k0 / gamma * g1k0 * 10000 / ANN;
        I mul2 = (2 * E18() * 2) * K0 / g1k0;
        I neg_fprime = (S + S * mul2 / E18()) + mul1 * 2 / K0 - mul2 * D / E18();
        I D_plus = D * (neg_fprime + S) / neg_fprime;
        I D_minus = D * D / neg_fprime;
        if (E18() > K0)
            D_minus += D * (mul1 / neg_fprime) / E18() * (E18() - K0) / K0;
        else
            D_minus -= D * (mul1 / neg_fprime) / E18() * (K0 - E18()) / K0;
        if (D_plus > D_minus) D = D_plus - D_minus;
        else D = (D_minus - D_plus) / 2;
        I diff = (D > D_prev) ? I(D - D_prev) : I(D_prev - D);
        I lim = D; if (lim < pow_int(10, 16)) lim = pow_int(10, 16);
        if (diff * pow_int(10, 14) < lim) {
            for (int k = 0; k < 2; ++k) {
                I frac = x[k] * E18() / D;
                req(frac > pow_int(10, 16) / 2 - 1 && frac < pow_int(10, 20) / 2 + 1,
                    "unsafe x frac");
            }
            return D;
        }
    }
    throw std::runtime_error("newton_D no conv");
}

// --------------------------------------------- StableswapMath (YB twocrypto)
// _amp already contains A_MULTIPLIER(10000); Ann = _amp * 2

inline I ss_get_y(const I& amp, const I xp[2], const I& D, int i) {
    req(i < 2, "i range");
    I S_ = 0, c = D;
    I Ann = amp * 2;
    for (int k = 0; k < 2; ++k) {
        if (k == i) continue;
        S_ += xp[k];
        c = c * D / (xp[k] * 2);
    }
    c = c * D * 10000 / (Ann * 2);
    I b = S_ + D * 10000 / Ann;
    I y = D;
    for (int it = 0; it < 255; ++it) {
        I y_prev = y;
        y = (y * y + c) / (2 * y + b - D);
        if (y > y_prev ? y - y_prev <= 1 : y_prev - y <= 1) return y;
    }
    throw std::runtime_error("ss y no conv");
}

inline I ss_newton_D(const I& amp, const I xp[2], bool check_balance) {
    if (check_balance) {   // v0.1.1 only
        I mx = xp[0] > xp[1] ? xp[0] : xp[1];
        I mn = xp[0] > xp[1] ? xp[1] : xp[0];
        req(xp[0] > 0 && xp[1] > 0 && mx / mn < 10000, "!balance");
    }
    I S = xp[0] + xp[1];
    if (S == 0) return 0;
    I D = S, Ann = amp * 2;
    for (int it = 0; it < 255; ++it) {
        I D_P = D;
        for (int k = 0; k < 2; ++k) D_P = D_P * D / xp[k];
        D_P /= 4;
        I Dprev = D;
        D = (Ann * S / 10000 + D_P * 2) * D
            / ((Ann - 10000) * D / 10000 + 3 * D_P);
        if (D > Dprev ? D - Dprev <= 1 : Dprev - D <= 1) return D;
    }
    throw std::runtime_error("ss D no conv");
}

// ---------------------------------------------------------------- tricrypto
// N_COINS = 3, A_MULTIPLIER = 10000

inline void tri_sort_desc(I x[3]) {
    I t;
    if (x[0] < x[1]) { t = x[0]; x[0] = x[1]; x[1] = t; }
    if (x[0] < x[2]) { t = x[0]; x[0] = x[2]; x[2] = t; }
    if (x[1] < x[2]) { t = x[1]; x[1] = x[2]; x[2] = t; }
}

inline I tri_newton_y(const I& ANN, const I& gamma, const I x[3], const I& D, int i,
                      int a_mult = 10000) {
    for (int k = 0; k < 3; ++k) {
        if (k == i) continue;
        I frac = x[k] * E18() / D;
        req(frac > pow_int(10, 16) - 1 && frac < pow_int(10, 20) + 1, "unsafe x[k]");
    }
    I y = D / 3;
    I K0_i = E18();
    I S_i = 0;
    I xs[3] = {x[0], x[1], x[2]};
    xs[i] = 0;
    tri_sort_desc(xs);
    I convergence_limit = xs[0] / I("100000000000000");
    { I t = D / I("100000000000000"); if (t > convergence_limit) convergence_limit = t; }
    if (convergence_limit < 100) convergence_limit = 100;

    for (int j = 2; j <= 3; ++j) {
        I _x = xs[3 - j];             // small x first
        y = y * D / (_x * 3);
        S_i += _x;
    }
    for (int j = 0; j < 2; ++j)
        K0_i = K0_i * xs[j] * 3 / D;  // large x first

    for (int it = 0; it < 255; ++it) {
        I y_prev = y;
        I K0 = K0_i * y * 3 / D;
        I S = S_i + y;
        I g1k0 = gamma + E18();
        g1k0 = (g1k0 > K0) ? (g1k0 - K0 + 1) : (K0 - g1k0 + 1);
        I mul1 = E18() * D / gamma * g1k0 / gamma * g1k0 * a_mult / ANN;
        I mul2 = E18() + (2 * E18()) * K0 / g1k0;
        I yfprime = E18() * y + S * mul2 + mul1;
        I dyfprime = D * mul2;
        if (yfprime < dyfprime) { y = y_prev / 2; continue; }
        yfprime -= dyfprime;
        I fprime = yfprime / y;
        I y_minus = mul1 / fprime;
        I y_plus = (yfprime + E18() * D) / fprime + y_minus * E18() / K0;
        y_minus += E18() * S / fprime;
        if (y_plus < y_minus) y = y_prev / 2;
        else y = y_plus - y_minus;
        I diff = (y > y_prev) ? I(y - y_prev) : I(y_prev - y);
        I lim = y / I("100000000000000"); if (lim < convergence_limit) lim = convergence_limit;
        if (diff < lim) {
            I frac = y * E18() / D;
            req(frac > pow_int(10, 16) - 1 && frac < pow_int(10, 20) + 1, "unsafe y");
            return y;
        }
    }
    throw std::runtime_error("tri newton_y no conv");
}

inline void tri_get_y(const I& _ANN, const I& _gamma, const I x[3], const I& _D,
                      int i, I out[2]) {
    req(_D > pow_int(10, 17) - 1 && _D < pow_int(10, 15) * E18() + 1, "unsafe D");
    for (int k = 0; k < 3; ++k) {
        if (k == i) continue;
        I frac = x[k] * E18() / _D;
        req(frac > pow_int(10, 16) - 1 && frac < pow_int(10, 20) + 1, "unsafe x[k]");
    }
    int j = 0, k = 0;
    if (i == 0) { j = 1; k = 2; }
    else if (i == 1) { j = 0; k = 2; }
    else { j = 0; k = 1; }

    const I ANN = _ANN, gamma = _gamma, D = _D;
    const I x_j = x[j], x_k = x[k];
    const I gamma2 = gamma * gamma;

    I a = pow_int(10, 36) / 27;
    I b = pow_int(10, 36) / 9 + 2 * E18() * gamma / 27
          - D * D / x_j * gamma2 * ANN / 729 / 10000 / x_k;
    I c = pow_int(10, 36) / 9 + gamma * (gamma + 4 * E18()) / 27
          + gamma2 * (x_j + x_k - D) / D * ANN / 27 / 10000;
    I d = (E18() + gamma) * (E18() + gamma) / 27;

    I d0 = absI(3 * a * c / b - b);

    I divider = 1;
    if      (d0 > pow_int(10, 48)) divider = pow_int(10, 30);
    else if (d0 > pow_int(10, 44)) divider = pow_int(10, 26);
    else if (d0 > pow_int(10, 40)) divider = pow_int(10, 22);
    else if (d0 > pow_int(10, 36)) divider = pow_int(10, 18);
    else if (d0 > pow_int(10, 32)) divider = pow_int(10, 14);
    else if (d0 > pow_int(10, 28)) divider = pow_int(10, 10);
    else if (d0 > pow_int(10, 24)) divider = pow_int(10, 6);
    else if (d0 > pow_int(10, 20)) divider = pow_int(10, 2);

    I additional_prec;
    if (absI(a) > absI(b)) {
        additional_prec = absI(a / b);
        a = a * additional_prec / divider;
        b = b * additional_prec / divider;
        c = c * additional_prec / divider;
        d = d * additional_prec / divider;
    } else {
        additional_prec = absI(b / a);
        a = a / additional_prec / divider;
        b = b / additional_prec / divider;
        c = c / additional_prec / divider;
        d = d / additional_prec / divider;
    }

    I _3ac = 3 * a * c;
    I delta0 = _3ac / b - b;
    I delta1 = 3 * _3ac / b - 2 * b - 27 * a * a / b * d / b;

    I sqrt_arg = delta1 * delta1 + 4 * delta0 * delta0 / b * delta0;
    if (sqrt_arg <= 0) {
        out[0] = tri_newton_y(_ANN, _gamma, x, _D, i);
        out[1] = 0;
        return;
    }
    I sqrt_val = isqrt_u(sqrt_arg);

    I b_cbrt = (b >= 0) ? cbrt_u(b) : I(-cbrt_u(-b));
    I second_cbrt;
    if (delta1 > 0) second_cbrt = cbrt_u((delta1 + sqrt_val) / 2);
    else            second_cbrt = -cbrt_u(I(-(delta1 - sqrt_val)) / 2);

    I C1 = b_cbrt * b_cbrt / E18() * second_cbrt / E18();
    I root_K0 = (b + b * delta0 / C1 - C1) / 3;
    I root = D * D / 27 / x_k * D / x_j * root_K0 / a;

    out[0] = root;
    out[1] = E18() * root_K0 / a;

    I frac = out[0] * E18() / _D;
    req(frac >= pow_int(10, 16) - 1 && frac < pow_int(10, 20) + 1, "unsafe y");
}

inline I tri_geometric_mean(const I x[3]) {
    I prod = x[0] * x[1] / E18() * x[2] / E18();
    if (prod == 0) return 0;
    return cbrt_u(prod);
}

inline I tri_newton_D(const I& ANN, const I& gamma, const I x_unsorted[3],
                      const I& K0_prev) {
    I x[3] = {x_unsorted[0], x_unsorted[1], x_unsorted[2]};
    tri_sort_desc(x);
    req(x[0] > 0, "empty pool");

    I S = x[0] + x[1] + x[2];
    I D;
    if (K0_prev == 0) {
        D = 3 * tri_geometric_mean(x);
    } else {
        if (S > pow_int(10, 36))
            D = cbrt_u(x[0] * x[1] / pow_int(10, 36) * x[2] / K0_prev * 27 * pow_int(10, 12));
        else if (S > pow_int(10, 24))
            D = cbrt_u(x[0] * x[1] / pow_int(10, 24) * x[2] / K0_prev * 27 * pow_int(10, 6));
        else
            D = cbrt_u(x[0] * x[1] / E18() * x[2] / K0_prev * 27);
    }

    for (int it = 0; it < 255; ++it) {
        I D_prev = D;
        I K0 = E18() * x[0] * 3 / D * x[1] * 3 / D * x[2] * 3 / D;
        I g1k0 = gamma + E18();
        g1k0 = (g1k0 > K0) ? (g1k0 - K0 + 1) : (K0 - g1k0 + 1);
        I mul1 = E18() * D / gamma * g1k0 / gamma * g1k0 * 10000 / ANN;
        I mul2 = (2 * E18() * 3) * K0 / g1k0;
        I neg_fprime = (S + S * mul2 / E18()) + mul1 * 3 / K0 - mul2 * D / E18();
        I D_plus = D * (neg_fprime + S) / neg_fprime;
        I D_minus = D * D / neg_fprime;
        if (E18() > K0)
            D_minus += D * (mul1 / neg_fprime) / E18() * (E18() - K0) / K0;
        else
            D_minus -= D * (mul1 / neg_fprime) / E18() * (K0 - E18()) / K0;
        if (D_plus > D_minus) D = D_plus - D_minus;
        else D = (D_minus - D_plus) / 2;
        I diff = (D > D_prev) ? I(D - D_prev) : I(D_prev - D);
        I lim = D; if (lim < pow_int(10, 16)) lim = pow_int(10, 16);
        if (diff * pow_int(10, 14) < lim) {
            for (int q = 0; q < 3; ++q) {
                I frac = x[q] * E18() / D;
                req(frac >= pow_int(10, 16) - 1 && frac < pow_int(10, 20) + 1, "unsafe frac");
            }
            return D;
        }
    }
    throw std::runtime_error("tri newton_D no conv");
}

// ------------------------------------------- old cryptoswap (2021 vintage)

// iterative geometric mean, 2 coins (cvxeth/eursusdc/teth newton_D seed)
inline I old_geometric_mean2(const I x_in[2]) {
    I x[2] = {x_in[0], x_in[1]};
    I D = x[0];
    for (int it = 0; it < 255; ++it) {
        I D_prev = D;
        D = (D + x[0] * x[1] / D) / 2;
        I diff = (D > D_prev) ? I(D - D_prev) : I(D_prev - D);
        if (diff <= 1 || diff * E18() < D) return D;
    }
    throw std::runtime_error("geomean no conv");
}

// old 2-coin newton_y: same iteration as twocrypto, old fixed bounds + final frac assert
inline I old2_newton_y(const I& ANN, const I& gamma, const I x[2], const I& D, int i) {
    req(D > pow_int(10, 17) - 1 && D < pow_int(10, 15) * E18() + 1, "unsafe D");
    const I x_j = x[1 - i];
    I y = D * D / (x_j * 4);
    I K0_i = (E18() * 2) * x_j / D;
    req(K0_i > pow_int(10, 16) * 2 - 1 && K0_i < pow_int(10, 20) * 2 + 1, "unsafe x[i]");
    I convergence_limit = x_j / I("100000000000000");
    { I t = D / I("100000000000000"); if (t > convergence_limit) convergence_limit = t; }
    if (convergence_limit < 100) convergence_limit = 100;
    for (int it = 0; it < 255; ++it) {
        I y_prev = y;
        I K0 = K0_i * y * 2 / D;
        I S = x_j + y;
        I g1k0 = gamma + E18();
        g1k0 = (g1k0 > K0) ? (g1k0 - K0 + 1) : (K0 - g1k0 + 1);
        I mul1 = E18() * D / gamma * g1k0 / gamma * g1k0 * 10000 / ANN;
        I mul2 = E18() + (2 * E18()) * K0 / g1k0;
        I yfprime = E18() * y + S * mul2 + mul1;
        I dyfprime = D * mul2;
        if (yfprime < dyfprime) { y = y_prev / 2; continue; }
        yfprime -= dyfprime;
        I fprime = yfprime / y;
        I y_minus = mul1 / fprime;
        I y_plus = (yfprime + E18() * D) / fprime + y_minus * E18() / K0;
        y_minus += E18() * S / fprime;
        if (y_plus < y_minus) y = y_prev / 2;
        else y = y_plus - y_minus;
        I diff = (y > y_prev) ? I(y - y_prev) : I(y_prev - y);
        I lim = y / I("100000000000000"); if (lim < convergence_limit) lim = convergence_limit;
        if (diff < lim) {
            I frac = y * E18() / D;
            req(frac > pow_int(10, 16) - 1 && frac < pow_int(10, 20) + 1, "unsafe y");
            return y;
        }
    }
    throw std::runtime_error("old2 y no conv");
}

// old 2-coin newton_D: geometric-mean seed, old frac bounds
inline I old2_newton_D(const I& ANN, const I& gamma, const I x_unsorted[2]) {
    I x[2] = {x_unsorted[0], x_unsorted[1]};
    if (x[0] < x[1]) { x[0] = x_unsorted[1]; x[1] = x_unsorted[0]; }
    req(x[0] > pow_int(10, 9) - 1 && x[0] < pow_int(10, 15) * E18() + 1, "unsafe x0");
    req(x[1] * E18() / x[0] > pow_int(10, 14) - 1, "unsafe ratio");
    I D = 2 * old_geometric_mean2(x);
    I S = x[0] + x[1];
    for (int it = 0; it < 255; ++it) {
        I D_prev = D;
        I K0 = (E18() * 4) * x[0] / D * x[1] / D;
        I g1k0 = gamma + E18();
        g1k0 = (g1k0 > K0) ? (g1k0 - K0 + 1) : (K0 - g1k0 + 1);
        I mul1 = E18() * D / gamma * g1k0 / gamma * g1k0 * 10000 / ANN;
        I mul2 = (2 * E18()) * 2 * K0 / g1k0;
        I neg_fprime = (S + S * mul2 / E18()) + mul1 * 2 / K0 - mul2 * D / E18();
        I D_plus = D * (neg_fprime + S) / neg_fprime;
        I D_minus = D * D / neg_fprime;
        if (E18() > K0)
            D_minus += D * (mul1 / neg_fprime) / E18() * (E18() - K0) / K0;
        else
            D_minus -= D * (mul1 / neg_fprime) / E18() * (K0 - E18()) / K0;
        if (D_plus > D_minus) D = D_plus - D_minus;
        else D = (D_minus - D_plus) / 2;
        I diff = (D > D_prev) ? I(D - D_prev) : I(D_prev - D);
        I lim = D; if (lim < pow_int(10, 16)) lim = pow_int(10, 16);
        if (diff * pow_int(10, 14) < lim) {
            for (int k = 0; k < 2; ++k) {
                I frac = x[k] * E18() / D;
                req(frac > pow_int(10, 16) - 1 && frac < pow_int(10, 20) + 1, "unsafe frac");
            }
            return D;
        }
    }
    throw std::runtime_error("old2 D no conv");
}

// ------------------------------------------------------------------- fees

// NEW Twocrypto (vyper 0.4.3, 2025) _fee: corrected reduction formula.
// clamp_min == 0 -> no clamp (older new-impl); else min/max clamp applied.
inline I two_fee_new(const I xp[2], const I& mid_fee, const I& out_fee,
                     const I& fee_gamma, const I& clamp_min) {
    I S = xp[0] + xp[1];
    I B = E18() * 4 * xp[0] / S * xp[1] / S;
    B = fee_gamma * B / (fee_gamma * B / E18() + E18() - B);
    I fee = (mid_fee * B + out_fee * (E18() - B)) / E18();
    if (clamp_min != 0) {
        if (fee < clamp_min) fee = clamp_min;
        I mx = pow_int(10, 10);
        if (fee > mx) fee = mx;
    }
    return fee;
}

// twocrypto pool fee_calc(xp): xp scaled, post-trade
inline I two_fee(const I xp[2], const I& mid_fee, const I& out_fee, const I& fee_gamma) {
    I f = xp[0] + xp[1];
    f = fee_gamma * E18() / (fee_gamma + E18() - (E18() * 4) * xp[0] / f * xp[1] / f);
    return (mid_fee * f + out_fee * (E18() - f)) / E18();
}

// tricrypto pool _fee(xp) via reduction_coefficient
inline I tri_fee(const I xp[3], const I& mid_fee, const I& out_fee, const I& fee_gamma) {
    I S = xp[0] + xp[1] + xp[2];
    I K = E18() * 3 * xp[0] / S;
    K = K * 3 * xp[1] / S;
    K = K * 3 * xp[2] / S;
    if (fee_gamma > 0)
        K = fee_gamma * E18() / (fee_gamma + E18() - K);
    return (mid_fee * K + out_fee * (E18() - K)) / E18();
}

}  // namespace cm
