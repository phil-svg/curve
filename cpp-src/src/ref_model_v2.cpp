// ref_model_v2.cpp — C++ port of llamma-simulator_v2's LendingAMM
// (simulator/amm/lending_amm.py @ 70367aa) plus the per-window `simulate()`
// of zchf_crvusd/sweep_parameters.py. Double precision, algorithm-identical;
// Python `**` is mirrored with std::pow so results match bit-for-bit.
//
// Division of labour (keeps Python-specific behaviour in Python):
//   driver (sweep_ref_v2.py) builds the oracle series, draws the seeded
//   window starts, and computes the summary statistics with `statistics`;
//   this binary only walks windows and emits one loss per window.
//
// Inputs:  --market  JSON [[t_s, o, h, l, c, v], ...]
//          --oracle  JSON [oracle_i, ...] aligned with market rows
//          --starts  JSON [start_row, ...]
//          --length N  --A a  --fee f  --bands n  --ext-fee e  --dyn-mult m
// Output:  --out path  -> raw little-endian doubles, one per start, in order.

#include <chrono>
#include <cmath>
#include <random>
#include <cstdio>
#include <deque>
#include <filesystem>
#include <fstream>
#include <string>
#include <functional>
#include <thread>
#include <atomic>
#include <mutex>
#include <condition_variable>
#include <vector>
#include <algorithm>
#include <nlohmann/json.hpp>

// wasm fast math: squares/cubes via multiplication and half-powers via the
// native f64.sqrt instruction instead of musl's soft-float pow(). Gated to
// emscripten and verified BIT-IDENTICAL per cell against the native binary
// by the dev/wasm-poc harness (native keeps std::pow: on Apple libm x*x is
// not always bit-equal to pow(x,2)).
#ifdef __EMSCRIPTEN__
static inline double pow2f(double x) { return x * x; }
static inline double pow3f(double x) { return x * x * x; }
static inline double powhf(double x) { return std::sqrt(x); }
#else
static inline double pow2f(double x) { return std::pow(x, 2.0); }
static inline double pow3f(double x) { return std::pow(x, 3.0); }
static inline double powhf(double x) { return std::pow(x, 0.5); }
#endif


using json = nlohmann::json;

struct Candle { double t, o, h, l, c; };

struct AMM {
    static constexpr double PREV_P_O_DELAY = 120.0;
    static constexpr double MAX_P_O_CHANGE = 1.25;
    static constexpr double MIN_PRICE_RATIO = 1.0 / 1.25;
    static constexpr int OFF = 600;   // bands live in (-500, 500)

    double p_base, p_oracle, prev_p_oracle, raw_p_oracle, old_p_oracle;
    double old_dfee = 0.0;
    double prev_p_oracle_time = -1.0;   // -1 == None
    double current_timestamp = -1.0;
    double A, fee, dyn_mult;
    int active_band = 0, min_band = 0, max_band = 0;
    int lo_seen, hi_seen;
    std::vector<double> bx, by;

    AMM(double p_base_, double A_, double fee_, double mult)
        : p_base(p_base_), p_oracle(p_base_), prev_p_oracle(p_base_),
          raw_p_oracle(p_base_), old_p_oracle(p_base_), A(A_), fee(fee_),
          dyn_mult(mult), lo_seen(0), hi_seen(0),
          bx(2 * OFF + 1, 0.0), by(2 * OFF + 1, 0.0),
          pt_c(2 * OFF + 1, 0.0), pb2_c(2 * OFF + 1, 0.0),
          pt_ok(2 * OFF + 1, 0) { br2 = pow2f(A / (A - 1.0)); }

    double &X(int n) { return bx[n + OFF]; }
    double &Y(int n) { return by[n + OFF]; }

    // Re-init for the next loan without reallocating: clear only the band
    // window the previous run touched (lo_seen/hi_seen bound every X/Y
    // write and every pt_cached index, +-4 margin for the p_up/p_down
    // lookahead), then restore every scalar the constructor sets. State is
    // exactly a fresh AMM's, so results are bit-identical — verified per
    // cell against the native binary by the dev/wasm-poc harness.
    void reset(double p_base_, double A_, double fee_, double mult) {
        int lo = std::max(-OFF, lo_seen - 4), hi = std::min(OFF, hi_seen + 4);
        for (int n = lo; n <= hi; n++) {
            bx[n + OFF] = 0.0; by[n + OFF] = 0.0;
            pt_c[n + OFF] = 0.0; pb2_c[n + OFF] = 0.0; pt_ok[n + OFF] = 0;
        }
        p_base = p_base_; p_oracle = p_base_; prev_p_oracle = p_base_;
        raw_p_oracle = p_base_; old_p_oracle = p_base_; old_dfee = 0.0;
        prev_p_oracle_time = -1.0; current_timestamp = -1.0;
        A = A_; fee = fee_; dyn_mult = mult;
        active_band = 0; min_band = 0; max_band = 0; lo_seen = 0; hi_seen = 0;
        br2 = pow2f(A / (A - 1.0));
        po_src = -1.0; po3 = 0.0; po2 = 0.0;
        view_valid = false; view_po3_ok = false;
        view_ts = 0.0; view_p_o = 0.0; view_mem = 0.0; view_po3 = 0.0;
        p_valid = false; p_memo = 0.0;
    }

    // memoised pow() results — identical bits, far fewer libm calls
    mutable std::vector<double> pt_c, pb2_c;   // p_top(n), pow(p_top(n),2)
    mutable std::vector<char> pt_ok;
    mutable double po_src = -1.0, po3 = 0.0, po2 = 0.0;
    // per-candle view cache: between set_p_oracle() writes the state that
    // limit_price_oracle(read) depends on is constant, so the ~10
    // dynamic_fee() calls of one candle all recompute the same limited
    // price / memory ratio. Cache them (and pow(p_o_view,3) for
    // distance_fee); invalidated on every write.
    mutable bool view_valid = false, view_po3_ok = false;
    mutable double view_ts = 0.0, view_p_o = 0.0, view_mem = 0.0, view_po3 = 0.0;
    double br2 = 0.0;                 // pow(A/(A-1), 2), constant per AMM
    // get_p() memo: pure function of (active band, its x/y, p_oracle);
    // invalidated on oracle writes, deposits and trades.
    mutable bool p_valid = false;
    mutable double p_memo = 0.0;
    void oracle_pows() const {
        if (p_oracle != po_src) {
            po_src = p_oracle;
            po3 = pow3f(p_oracle);
            po2 = pow2f(p_oracle);
        }
    }
    double pt_cached(int n) const {
        if (!pt_ok[n + OFF]) {
            pt_c[n + OFF] = p_base * std::pow(k(), (double)n);
            pb2_c[n + OFF] = pow2f(pt_c[n + OFF]);
            pt_ok[n + OFF] = 1;
        }
        return pt_c[n + OFF];
    }

    // ---- oracle memory -------------------------------------------------
    double memory_dt(double ts) const {
        if (prev_p_oracle_time < 0 || ts < 0) return PREV_P_O_DELAY;
        double elapsed = std::max(ts - prev_p_oracle_time, 0.0);
        return PREV_P_O_DELAY - std::min(PREV_P_O_DELAY, elapsed);
    }
    // returns limited_price, sets *ratio_out
    double limit_price_oracle(double price, double ts, bool write,
                              double *ratio_out) {
        if (ts >= 0) current_timestamp = ts;   // _normalize_timestamp
        else ts = prev_p_oracle_time;
        double old_price = old_p_oracle, odf = old_dfee;
        double dt = memory_dt(ts);
        double limited = price, ratio = 0.0;
        if (dt > 0 && old_price > 0) {
            double pr = std::min(old_price, price) / std::max(old_price, price);
            if (price > old_price && pr < MIN_PRICE_RATIO) {
                pr = MIN_PRICE_RATIO; limited = old_price * MAX_P_O_CHANGE;
            } else if (price < old_price && pr < MIN_PRICE_RATIO) {
                pr = MIN_PRICE_RATIO; limited = old_price / MAX_P_O_CHANGE;
            }
            ratio = ((1.0 + odf) - pow3f(pr)) * (dt / PREV_P_O_DELAY);
            ratio = std::min(std::max(ratio, 0.0), 1.0 - 1e-18);
        }
        if (write) {
            raw_p_oracle = price;
            old_p_oracle = limited;
            old_dfee = ratio;
            if (ts >= 0) prev_p_oracle_time = ts;
        }
        *ratio_out = ratio;
        return limited;
    }
    void set_p_oracle(double p, double ts) {
        double r;
        double price = limit_price_oracle(p, ts, true, &r);
        prev_p_oracle = p_oracle;
        p_oracle = price;
        view_valid = false; p_valid = false;
    }
    void invalidate_caches() { view_valid = false; p_valid = false; }
    double distance_fee(double p_o, int n) const {
        double p_o_up = p_top(n);
        if (p_o_up <= 0) return 0.0;
        if (!view_po3_ok) { view_po3 = pow3f(p_o); view_po3_ok = true; }
        double p_c_d = view_po3 / pb2_c[n + OFF];
        double p_c_u = p_c_d * br2;
        if (p_o < p_c_d && p_c_d > 0) return (p_c_d - p_o) / p_c_d * dyn_mult;
        if (p_o > p_c_u && p_o > 0) return (p_o - p_c_u) / p_o * dyn_mult;
        return 0.0;
    }
    double dynamic_fee(int n, double ts) {
        if (!view_valid || ts != view_ts) {
            view_p_o = limit_price_oracle(raw_p_oracle, ts, false, &view_mem);
            view_ts = ts; view_valid = true; view_po3_ok = false;
        }
        double f = std::max(fee, view_mem);
        return std::max(f, distance_fee(view_p_o, n));
    }

    // ---- band geometry -------------------------------------------------
    double k() const { return (A - 1.0) / A; }
    double p_top(int n) const { return pt_cached(n); }
    double p_down(int n) const {
        pt_cached(n); oracle_pows();
        return po3 / pb2_c[n + OFF];
    }
    double p_up(int n) const {
        pt_cached(n + 1); oracle_pows();
        return po3 / pb2_c[n + 1 + OFF];
    }
    int get_band_n(double p) const {
        return (int)std::floor(std::log(p / p_base) / std::log(k()));
    }
    void deposit_nrange(double amount, double p, int dn) {
        int n_top = get_band_n(p_oracle) + 1;
        int n1 = std::max(get_band_n(p), n_top);
        int n2 = n1 + dn - 1;
        double y = amount / dn;
        min_band = n1; max_band = n2; lo_seen = n1; hi_seen = n2;
        for (int i = n1; i <= n2; i++) Y(i) += y;
        p_valid = false;
    }
    double get_y0(int n) const {
        double x = bx[n + OFF], y = by[n + OFF];
        double pt = p_top(n);
        oracle_pows();
        double a = p_oracle * A;
        double b = pt / p_oracle * (A - 1.0) * x
                 + po2 / pt * A * y;
        double D = pow2f(b) + 4.0 * a * x * y;
        return (b + std::sqrt(D)) / (2.0 * a);
    }
    double get_f(double y0, int n) const {
        oracle_pows();
        return y0 * po2 / p_top(n) * A;
    }
    double get_g(double y0, int n) const {
        return y0 * p_top(n) / p_oracle * (A - 1.0);
    }
    double get_p() const {
        if (p_valid) return p_memo;
        int n = active_band;
        double x = bx[n + OFF], y = by[n + OFF];
        if (x == 0.0 && y == 0.0)
            p_memo = powhf(p_up(n) * p_down(n));
        else {
            double y0 = get_y0(n);
            p_memo = (get_f(y0, n) + x) / (get_g(y0, n) + y);
        }
        p_valid = true;
        return p_memo;
    }

    // ---- trading -------------------------------------------------------
    bool trade_to_price(double price) {   // false = band guard tripped
        int bstep;
        if (X(active_band) == 0.0 && Y(active_band) == 0.0) {
            if (price > p_up(active_band)) bstep = 1;
            else if (price < p_down(active_band)) bstep = -1;
            else return true;
        } else {
            double cur = get_p();
            if (price > cur) bstep = 1;
            else if (price < cur) bstep = -1;
            else return true;
        }
        double original_price = price;
        p_valid = false;   // the loop below mutates x/y/active_band
        while (true) {
            int n = active_band;
            if (!(-500 < n && n < 500)) return false;
            if (n < lo_seen) lo_seen = n;
            if (n > hi_seen) hi_seen = n;
            double x = X(n), y = Y(n);
            if (x == 0.0 && y == 0.0) {
                if (p_down(n) <= price && price <= p_up(n)) break;
                active_band += bstep;
                continue;
            }
            double y0 = get_y0(n);
            double g = get_g(y0, n), f = get_f(y0, n);
            double Inv = (f + x) * (g + y);
            price = original_price;
            double fe = dynamic_fee(n, current_timestamp);
            double p_c_d = p_down(n), p_c_u = p_up(n);
            if (bstep == 1) {
                price = price * (1.0 - fe);
                if (price < p_c_d) break;
                double y_dest = powhf(Inv / price) - g;
                double x_old = X(n);
                if (y_dest >= 0.0) {
                    Y(n) = y_dest;
                    X(n) = Inv / (g + y_dest) - f;
                    X(n) += fe * (X(n) - x_old);
                    break;
                }
                Y(n) = 0.0;
                X(n) = Inv / g - f;
                X(n) += fe * (X(n) - x_old);
                active_band += 1;
            } else {
                price = price * (1.0 + fe);
                if (price > p_c_u) break;
                double x_dest = powhf(Inv * price) - f;
                double y_old = Y(n);
                if (x_dest >= 0.0) {
                    X(n) = x_dest;
                    Y(n) = Inv / (f + x_dest) - g;
                    Y(n) += fe * (Y(n) - y_old);
                    break;
                }
                X(n) = 0.0;
                Y(n) = Inv / f - g;
                Y(n) += fe * (Y(n) - y_old);
                active_band -= 1;
            }
        }
        if (active_band < lo_seen) lo_seen = active_band;
        if (active_band > hi_seen) hi_seen = active_band;
        return true;
    }

    // ---- valuation -----------------------------------------------------
    double get_x_down(int n) const {
        double x = bx[n + OFF], y = by[n + OFF];
        if (x == 0.0 && y == 0.0) return 0.0;
        double p_o = p_oracle;
        double p_o_up = p_top(n);
        double p_o_down = p_o_up * (A - 1.0) / A;
        double p_mid = pow3f(p_o) / pow2f(p_o_down) * (A - 1.0) / A;
        double sbr = std::sqrt(A / (A - 1.0));
        if (x == 0.0 || y == 0.0) {
            if (p_o > p_o_up) {
                double y_eq = (y == 0.0) ? x / p_mid : y;
                return y_eq * p_o_up / sbr;
            } else if (p_o < p_o_down) {
                return (x == 0.0) ? y * p_mid : x;
            }
        }
        double y0 = get_y0(n);
        double g = get_g(y0, n), f = get_f(y0, n);
        double Inv = (f + x) * (g + y);
        if (p_o > p_o_up) {
            double y_o = std::max(Inv / f, g) - g;
            return y_o * p_o_up / sbr;
        } else if (p_o < p_o_down) {
            return std::max(Inv / g, f) - f;
        }
        double y_o = A * y0 * (1.0 - p_o_down / p_o);
        double x_o = std::max(Inv / (g + y_o), f) - f;
        return x_o + y_o * std::sqrt(p_o_down * p_o);
    }
    double get_all_x() const {
        // Python sums -500..499 in ascending order; empties contribute 0,
        // so summing the touched range in the same order is identical.
        double s = 0.0;
        for (int n = std::max(-500, lo_seen - 1);
             n <= std::min(499, hi_seen + 1); n++)
            s += get_x_down(n);
        return s;
    }
};

// his simulate(): one window
static double simulate(const std::vector<Candle> &M, const std::vector<double> &O,
                       double A, double fee, size_t start, size_t length,
                       int bands, double ext_fee, double mult) {
    double start_oracle = O[start];
    double p_base = start_oracle * (A / (A - 1.0) + 1e-4);
    static thread_local AMM *pool = nullptr;
    if (!pool) pool = new AMM(p_base, A, fee, mult);
    else pool->reset(p_base, A, fee, mult);
    AMM &amm = *pool;
    amm.deposit_nrange(1.0, start_oracle, bands);
    double initial_value = amm.get_all_x();      // with p_oracle == p_base
    amm.p_oracle = start_oracle;
    amm.prev_p_oracle = start_oracle;
    amm.raw_p_oracle = start_oracle;
    amm.old_p_oracle = start_oracle;
    amm.old_dfee = 0.0;
    amm.prev_p_oracle_time = M[start].t;
    amm.current_timestamp = M[start].t;
    amm.invalidate_caches();

    auto target = [&](double price, double ts, bool up) {
        if (up) {
            for (int b = amm.max_band; b >= amm.min_band; b--) {
                double df = amm.dynamic_fee(b, ts);
                double boundary = amm.p_down(b) * (1.0 + df);
                if (price > boundary) return price * (1.0 - df);
            }
            double df = amm.dynamic_fee(amm.min_band, ts);
            return price * (1.0 - df);
        }
        for (int b = amm.min_band; b <= amm.max_band; b++) {
            double df = amm.dynamic_fee(b, ts);
            double boundary = amm.p_up(b) * (1.0 - df);
            if (price < boundary) return price * (1.0 + df);
        }
        double df = amm.dynamic_fee(amm.max_band, ts);
        return price * (1.0 + df);
    };

    size_t end = std::min(start + length, M.size());
    for (size_t i = start; i < end; i++) {
        double ts = M[i].t;
        amm.set_p_oracle(O[i], ts);
        double high_t = target(M[i].h * (1.0 - ext_fee), ts, true);
        double low_t = target(M[i].l * (1.0 + ext_fee), ts, false);
        if (high_t > amm.get_p())
            if (!amm.trade_to_price(high_t)) return std::nan("");
        if (low_t < amm.get_p())
            if (!amm.trade_to_price(low_t)) return std::nan("");
    }
    return 1.0 - amm.get_all_x() / initial_value;
}

// ---------------------------------------------------------------- realities --
// Perturbed price histories (same scheme as ref_model.cpp): flip the sign
// of a random 10% of the small-half nonzero |log-return| bars per seed,
// bias-correct, reconstruct. Candles scale by the mid ratio; the oracle
// series scales by the EMA ratio (EMA is linear, so the non-EMA leg of the
// oracle is untouched).
struct RealityPrep {
    std::vector<double> r;
    std::vector<int> small_idx;
    int n_flip = 0;
    double bias = 0.0;
};
static RealityPrep prep_reality(const std::vector<double> &mid) {
    RealityPrep p;
    size_t n = mid.size();
    p.r.resize(n - 1);
    for (size_t i = 0; i + 1 < n; i++) p.r[i] = std::log(mid[i + 1] / mid[i]);
    std::vector<double> nz;
    nz.reserve(p.r.size());
    for (double v : p.r) if (v != 0.0) nz.push_back(std::fabs(v));
    if (nz.empty()) { p.n_flip = 0; p.bias = 0.0; return p; }
    std::nth_element(nz.begin(), nz.begin() + nz.size() / 2, nz.end());
    double med = nz[nz.size() / 2];
    for (size_t i = 0; i < p.r.size(); i++)
        if (p.r[i] != 0.0 && std::fabs(p.r[i]) <= med) p.small_idx.push_back(int(i));
    p.n_flip = std::max(1, int(0.1 * p.small_idx.size()));
    double mean_small = 0.0;
    for (int i : p.small_idx) mean_small += p.r[i];
    mean_small /= double(p.small_idx.size());
    p.bias = 2.0 * p.n_flip * mean_small / double(p.r.size());
    return p;
}
static std::vector<double> reality_mids(const RealityPrep &p,
                                        const std::vector<double> &mid,
                                        uint64_t seed) {
    std::vector<double> r = p.r;
    std::mt19937_64 rng(seed);
    std::vector<int> shuf = p.small_idx;
    int N = int(shuf.size());
    for (int i = 0; i < p.n_flip; i++) {
        int j = i + int(rng() % (uint64_t)(N - i));
        std::swap(shuf[i], shuf[j]);
        r[shuf[i]] *= -1.0;
    }
    for (size_t i = 0; i < r.size(); i++) r[i] += p.bias;
    std::vector<double> out(mid.size());
    out[0] = mid[0];
    double log_cum = 0.0;
    for (size_t i = 1; i < mid.size(); i++) {
        log_cum += r[i - 1];
        out[i] = mid[0] * std::exp(log_cum);
    }
    return out;
}
// EMA of (l+h)/2 with half-life hl seconds (v2 timestamps are seconds)
static std::vector<double> ema_of(const std::vector<Candle> &M, double hl) {
    std::vector<double> e(M.size());
    double ema = M[0].o, ema_t = M[0].t;
    for (size_t i = 0; i < M.size(); i++) {
        double mul = std::pow(2.0, -(M[i].t - ema_t) / hl);
        ema = ema * mul + (M[i].l + M[i].h) / 2.0 * (1.0 - mul);
        ema_t = M[i].t;
        e[i] = ema;
    }
    return e;
}

// ---------------------------------------------------------------- search --
// The exhaustive-tail machinery, ported from ref_model.cpp (v1) and
// validated the same way: coarse grid + local refinement + spike starts +
// jump starts + outward walk + anchor transfer, with --auto deriving every
// search length from the loan length L. Start domain matches the author's
// select_windows: rows [warmup, len(market) - L].

// Two parallel schedules, both writing out[k] = fn(k) so results are
// identical regardless of which thread computes what. PAR_STEAL uses an
// atomic grab-16 counter (load-balanced); default is the original static
// per-thread block split.
static void par_map_v2(int threads, size_t n, const std::function<float(size_t)> &fn,
                       std::vector<float> &out) {
    out.assign(n, 0.0f);
#ifdef PAR_STEAL
    std::atomic<size_t> next{0};
    std::vector<std::thread> pool;
    for (int th = 0; th < threads; th++)
        pool.emplace_back([&]() {
            constexpr size_t B = 16;
            for (;;) {
                size_t k0 = next.fetch_add(B);
                if (k0 >= n) break;
                size_t k1 = std::min(n, k0 + B);
                for (size_t k = k0; k < k1; k++) out[k] = fn(k);
            }
        });
#else
    std::vector<std::thread> pool;
    size_t per = (n + threads - 1) / threads;
    for (int th = 0; th < threads; th++)
        pool.emplace_back([&, th]() {
            size_t lo = th * per, hi = std::min(n, (th + 1) * per);
            for (size_t k = lo; k < hi; k++) out[k] = fn(k);
        });
#endif
    for (auto &t : pool) t.join();
}
static void write_f32(const std::string &path, const std::vector<float> &v) {
    std::ofstream f(path, std::ios::binary);
    f.write(reinterpret_cast<const char *>(v.data()),
            std::streamsize(v.size() * sizeof(float)));
}
static std::vector<double> parse_list(const std::string &sv) {
    std::vector<double> out; size_t q, pos = 0;
    while (pos < sv.size()) {
        q = sv.find(',', pos); if (q == std::string::npos) q = sv.size();
        out.push_back(std::stod(sv.substr(pos, q - pos))); pos = q + 1;
    }
    return out;
}

// JSON market/oracle with a packed-double .bin sidecar (like ref_model's
// klines cache): 75 MB of JSON costs ~2.3 s to parse, the sidecar ~50 ms.
static bool bin_fresh(const std::string &json_p, const std::string &bin_p) {
    namespace fs = std::filesystem;
    return fs::exists(bin_p) && fs::exists(json_p)
        && fs::last_write_time(bin_p) >= fs::last_write_time(json_p);
}
static void load_market(const std::string &path, std::vector<Candle> &M) {
    std::string bin = path + ".bin";
    if (bin_fresh(path, bin)) {
        std::ifstream f(bin, std::ios::binary);
        uint64_t n; f.read(reinterpret_cast<char *>(&n), 8);
        M.resize(n);
        f.read(reinterpret_cast<char *>(M.data()), std::streamsize(n * sizeof(Candle)));
        return;
    }
    std::ifstream f(path); json j; f >> j; M.clear(); M.reserve(j.size());
    for (auto &r : j) M.push_back({r[0].get<double>(), r[1].get<double>(),
                                   r[2].get<double>(), r[3].get<double>(),
                                   r[4].get<double>()});
    std::ofstream o(bin, std::ios::binary);
    uint64_t n = M.size(); o.write(reinterpret_cast<const char *>(&n), 8);
    o.write(reinterpret_cast<const char *>(M.data()), std::streamsize(n * sizeof(Candle)));
}
static void load_oracle(const std::string &path, std::vector<double> &O) {
    std::string bin = path + ".bin";
    if (bin_fresh(path, bin)) {
        std::ifstream f(bin, std::ios::binary);
        uint64_t n; f.read(reinterpret_cast<char *>(&n), 8);
        O.resize(n);
        f.read(reinterpret_cast<char *>(O.data()), std::streamsize(n * 8));
        return;
    }
    std::ifstream f(path); json j; f >> j; O = j.get<std::vector<double>>();
    std::ofstream o(bin, std::ios::binary);
    uint64_t n = O.size(); o.write(reinterpret_cast<const char *>(&n), 8);
    o.write(reinterpret_cast<const char *>(O.data()), std::streamsize(n * 8));
}

int main(int argc, char **argv) {
    std::string market_p, oracle_p, starts_p, out_p, dump_all, cand_out;
    std::vector<double> a_list, fee_list;
    double A = 140, fee = 0.002, ext_fee = 5e-4, mult = 0.25;
    long length = 1440; int bands = 4;
    int threads = 4;
    long warmup = 601;          // ceil(10 * 3603 / 60): his select_windows
    long realities = 1;         // --realities N: average the table over N
                                // perturbed histories (N=1: original)
    double oracle_hl = 3603.0;  // --oracle-hl s: EMA half-life used to carry
                                // a perturbation into the oracle series
    double tail_frac = 0.0005;
    long stride = 1;
    long gr_k = 0, gr_c = 0;
    bool auto_mode = false;
    long sp_w = 720, sp_r = 2;
    double sp_bands = 2.0, sp_jump = 0.5;
    long rf_radius = 200, rf_stride = 2;
    double rf_beta = 0.7;
    double tr_gamma = 0.4, tr_gs = 0.5;
    long tr_r = 10;
    bool set_k = false, set_radius = false, set_stride = false,
         set_spikes = false, set_transfer = false;
    for (int i = 1; i + 1 < argc; i++) {
        std::string a = argv[i], v = argv[i + 1];
        if (a == "--market") market_p = v;
        else if (a == "--oracle") oracle_p = v;
        else if (a == "--starts") starts_p = v;
        else if (a == "--out") out_p = v;
        else if (a == "--A") A = std::stod(v);
        else if (a == "--fee") fee = std::stod(v);
        else if (a == "--a-list") a_list = parse_list(v);
        else if (a == "--fee-list") fee_list = parse_list(v);
        else if (a == "--length") length = std::stol(v);
        else if (a == "--bands") bands = std::stoi(v);
        else if (a == "--ext-fee") ext_fee = std::stod(v);
        else if (a == "--dyn-mult") mult = std::stod(v);
        else if (a == "--threads") threads = std::stoi(v);
        else if (a == "--warmup") warmup = std::stol(v);
        else if (a == "--realities") realities = std::stol(v);
        else if (a == "--oracle-hl") oracle_hl = std::stod(v);
        else if (a == "--tail-frac") tail_frac = std::stod(v);
        else if (a == "--stride") stride = std::stol(v);
        else if (a == "--dump-all") dump_all = v;
        else if (a == "--cand-out") cand_out = v;
        else if (a == "--spike-bands") sp_bands = std::stod(v);
        else if (a == "--spike-jump") sp_jump = std::stod(v);
        else if (a == "--refine-stride") { rf_stride = std::stol(v); set_stride = true; }
        else if (a == "--refine-radius") { rf_radius = std::stol(v); set_radius = true; }
        else if (a == "--fill-beta") rf_beta = std::stod(v);
        else if (a == "--spikes") {
            auto kv = parse_list(v);
            if (kv.size() == 2) { sp_w = long(kv[0]); sp_r = long(kv[1]); set_spikes = true; }
        }
        else if (a == "--transfer") {
            auto kv = parse_list(v);
            if (kv.size() == 3) { tr_gamma = kv[0]; tr_r = long(kv[1]); tr_gs = kv[2]; set_transfer = true; }
        }
        else if (a == "--grid-refine") {
            auto kv = parse_list(v);
            if (kv.size() == 2) { gr_k = long(kv[0]); gr_c = long(kv[1]); set_k = true; }
        }
    }
    for (int i = 1; i < argc; i++)
        if (std::string(argv[i]) == "--auto") auto_mode = true;
    if (market_p.empty() || oracle_p.empty()) {
        std::fprintf(stderr, "usage: ref_model_v2 --market m.json --oracle o.json "
                             "(--starts s.json --out f | --dump-all f | --auto) "
                             "[--a-list/--fee-list] ...\n");
        return 2;
    }
    if (a_list.empty()) a_list = {A};
    if (fee_list.empty()) fee_list = {fee};

    std::vector<Candle> M; std::vector<double> O;
    load_market(market_p, M);
    load_oracle(oracle_p, O);
    if (O.size() != M.size()) { std::fprintf(stderr, "oracle/market size mismatch\n"); return 3; }
    size_t L = size_t(length);

    // ---- legacy mode: explicit starts file, one A/fee, raw f64 out ------
    if (!starts_p.empty()) {
        std::vector<long> S;
        { std::ifstream f(starts_p); json j; f >> j; S = j.get<std::vector<long>>(); }
        std::vector<double> losses(S.size());
        std::vector<std::thread> pool;
        size_t per = (S.size() + threads - 1) / threads;
        for (int t = 0; t < threads; t++)
            pool.emplace_back([&, t]() {
                size_t lo = t * per, hi = std::min(S.size(), (t + 1) * per);
                for (size_t i = lo; i < hi; i++)
                    losses[i] = simulate(M, O, a_list[0], fee_list[0], (size_t)S[i],
                                         L, bands, ext_fee, mult);
            });
        for (auto &t : pool) t.join();
        std::ofstream out(out_p, std::ios::binary);
        out.write(reinterpret_cast<const char *>(losses.data()),
                  std::streamsize(losses.size() * sizeof(double)));
        std::printf("%zu windows done\n", losses.size());
        return 0;
    }

    // ---- start domain (his select_windows): rows [warmup, n_rows - L] ---
    if (M.size() < L + size_t(warmup) + 1) { std::fprintf(stderr, "history too short\n"); return 4; }
    size_t n_all = M.size() - L - size_t(warmup) + 1;
    long m = std::max(1L, long(std::ceil(double(n_all) * tail_frac)));
    auto sim_at = [&](double Ax, double feex, size_t di) {
        double v = simulate(M, O, Ax, feex, size_t(warmup) + di, L, bands, ext_fee, mult);
        return std::isnan(v) ? 0.0 : v;   // band guard: score 0 like v1
    };

    if (auto_mode) {
        double Ld = double(L);
        if (!set_k) {
            gr_k = std::max(1L, long(std::llround(Ld / 28.8)));
            gr_c = std::max(200L, long(25.0 * double(m) / double(gr_k)));
        } else if (gr_c <= 0) {
            gr_c = std::max(200L, long(25.0 * double(m) / double(gr_k)));
        }
        if (!set_radius) rf_radius = std::max(2L, 2 * gr_k);
        if (!set_stride) rf_stride = gr_k >= 8 ? 2 : 1;
        if (!set_spikes) { sp_w = std::max(15L, long(std::llround(Ld / 4.0))); sp_r = 2; }
        if (!set_transfer) tr_r = std::max(1L, long(std::llround(Ld / 288.0)));
        std::fprintf(stderr, "auto: L=%zu k=%ld C=%ld radius=%ld stride=%ld "
                             "spikes=%ld,%ld transfer_r=%ld m=%ld n_all=%zu\n",
                     L, gr_k, gr_c, rf_radius, rf_stride, sp_w, sp_r, tr_r, m, n_all);
    }

    // ---- dump-all: exhaustive losses per start (float32, .cN per cell) --
    if (!dump_all.empty()) {
        size_t n_starts = (n_all + stride - 1) / stride;
        int ci = 0;
        for (double Ax : a_list)
            for (double feex : fee_list) {
                std::vector<float> out;
                par_map_v2(threads, n_starts, [&](size_t k) {
                    return float(sim_at(Ax, feex, k * size_t(stride)));
                }, out);
                char suf[32]; std::snprintf(suf, sizeof suf, ".c%d", ci);
                write_f32(dump_all + suf, out);
                std::printf("{\"mode\": \"dump\", \"cell\": %d, \"A\": %g, "
                            "\"fee\": %g, \"n_starts\": %zu, \"stride\": %ld, "
                            "\"L\": %zu, \"warmup\": %ld}\n",
                            ci, Ax, feex, n_starts, stride, L, warmup);
                std::fflush(stdout);
                ci++;
            }
        return 0;
    }

    if (!(gr_k > 0 && gr_c > 0)) {
        std::fprintf(stderr, "nothing to do: pass --starts, --dump-all, "
                             "--grid-refine k,C or --auto\n");
        return 5;
    }

    // --realities R: rerun the whole table on R perturbed histories and
    // average per cell (R = 1: original series, output unchanged)
    const int R = int(std::max(1L, realities));
    const size_t n_cells = a_list.size() * fee_list.size();
    std::vector<double> acc_loss(n_cells, 0.0), acc_max(n_cells, 0.0),
                        acc_secs(n_cells, 0.0), acc_sims(n_cells, 0.0);
    std::vector<double> cellA(n_cells), cellFee(n_cells);
    std::vector<char> cellTransfer(n_cells, 0);
    std::vector<Candle> M0;
    std::vector<double> O0, mid0, emaO;
    RealityPrep rp;
    if (R > 1) {
        M0 = M; O0 = O;
        mid0.resize(M0.size());
        for (size_t i = 0; i < M0.size(); i++)
            mid0[i] = (M0[i].h + M0[i].l) / 2.0;
        rp = prep_reality(mid0);
        emaO = ema_of(M0, oracle_hl);
    }
    for (int rseed = 0; rseed < R; rseed++) {
    if (R > 1) {
        auto midp = reality_mids(rp, mid0, uint64_t(rseed));
        for (size_t i = 0; i < M0.size(); i++) {
            double ratio = midp[i] / mid0[i];
            Candle c = M0[i];
            c.o *= ratio; c.h *= ratio; c.l *= ratio; c.c *= ratio;
            M[i] = c;
        }
        auto emaP = ema_of(M, oracle_hl);
        for (size_t i = 0; i < O.size(); i++)
            O[i] = O0[i] * emaP[i] / emaO[i];
    }

    // ---- shared price-series features (domain-independent of A/fee) ----
    // forward min of the lows over the loan length, per market row
    std::vector<double> fwd_min_row;
    if (sp_w > 0 && sp_bands > 0.0) {
        fwd_min_row.assign(M.size(), 0.0);
        std::deque<size_t> dq;
        for (long i = long(M.size()) - 1; i >= 0; i--) {
            while (!dq.empty() && M[dq.back()].l >= M[i].l) dq.pop_back();
            dq.push_back(size_t(i));
            while (!dq.empty() && dq.front() >= size_t(i) + L) dq.pop_front();
            fwd_min_row[i] = M[dq.front()].l;
        }
    }
    // sliding max of the previous sp_w opens, per market row
    std::vector<double> prevmax_row;
    if (sp_w > 0) {
        prevmax_row.assign(M.size(), -1e300);
        std::deque<size_t> dq;
        for (size_t i = 0; i < M.size(); i++) {
            if (!dq.empty()) prevmax_row[i] = M[dq.front()].o;
            while (!dq.empty() && M[dq.back()].o <= M[i].o) dq.pop_back();
            dq.push_back(i);
            while (!dq.empty() && dq.front() + size_t(sp_w) <= i) dq.pop_front();
        }
    }

    int ci_out = 0;
    std::vector<float> anchor_loss;
    double anchor_thr = 0.0;
    bool have_anchor = false;
    for (double Ax : a_list)
        for (double feex : fee_list) {
            auto t0 = std::chrono::steady_clock::now();
            // the anchor transfer is validated only while the base fee is at
            // most ~one band width (fee*A <= 1): above that, trades need
            // multi-band moves and the cell's worst starts are a different
            // population than the anchor's (validation: fee 2.919% cells,
            // recall 0.17). Such cells run the full search themselves.
            bool transfer = have_anchor && tr_gamma > 0.0 && feex * Ax <= 1.0;
            long s_rf = std::max(1L, rf_stride);
            size_t n_coarse = 0;
            std::vector<float> coarse;
            std::vector<char> mark(n_all, 0);
            if (!transfer) {
                n_coarse = (n_all + gr_k - 1) / gr_k;
                par_map_v2(threads, n_coarse, [&](size_t k) {
                    return float(sim_at(Ax, feex, k * size_t(gr_k)));
                }, coarse);
                std::vector<size_t> idx(n_coarse);
                for (size_t k = 0; k < n_coarse; k++) idx[k] = k;
                size_t C = std::min<size_t>(gr_c, n_coarse);
                std::partial_sort(idx.begin(), idx.begin() + C, idx.end(),
                                  [&](size_t a, size_t b) { return coarse[a] > coarse[b]; });
                long rad = rf_radius > 0 ? rf_radius : gr_k;
                for (size_t j = 0; j < C; j++) {
                    long c = long(idx[j]) * gr_k;
                    for (long i = std::max(0L, c - rad);
                         i <= std::min(long(n_all) - 1, c + rad); i += s_rf)
                        mark[i] = 1;
                }
            } else {
                double hot = tr_gamma * anchor_thr;
                for (size_t i = 0; i < n_all; i++) {
                    if (!(anchor_loss[i] >= hot)) continue;
                    for (long j = std::max(0L, long(i) - tr_r);
                         j <= std::min(long(n_all) - 1, long(i) + tr_r); j += s_rf)
                        mark[j] = 1;
                }
            }
            // spike starts: new high vs previous sp_w opens, +-sp_r, kept
            // only if the lows within the loan fall >= sp_bands bands
            size_t n_spike = 0;
            if (sp_w > 0) {
                double depth = 1.0 - sp_bands / Ax;
                double gs_thr = tr_gs * anchor_thr;
                for (size_t i = 0; i < n_all; i++) {
                    size_t r = size_t(warmup) + i;
                    if (M[r].o > prevmax_row[r]) {
                        for (long j = std::max(0L, long(i) - sp_r);
                             j <= std::min(long(n_all) - 1, long(i) + sp_r); j++) {
                            if (mark[j]) continue;
                            size_t rj = size_t(warmup) + size_t(j);
                            if (!fwd_min_row.empty() && M[rj].o > 0.0
                                && fwd_min_row[rj] / M[rj].o > depth)
                                continue;
                            if (transfer && !std::isnan(anchor_loss[j])
                                && anchor_loss[j] < gs_thr)
                                continue;
                            mark[j] = 1; n_spike++;
                        }
                    }
                }
            }
            // jump starts: +-sp_r around a one-minute open move >= J bands
            size_t n_jump = 0;
            if (sp_jump > 0.0) {
                double jthr = sp_jump / Ax;
                double gs_thr = tr_gs * anchor_thr;
                for (size_t i = 0; i < n_all; i++) {
                    size_t r = size_t(warmup) + i;
                    if (r + 1 >= M.size() || !(M[r].o > 0.0)) continue;
                    double mv = std::fabs(M[r + 1].o / M[r].o - 1.0);
                    if (mv < jthr) continue;
                    for (long j = std::max(0L, long(i) - sp_r);
                         j <= std::min(long(n_all) - 1, long(i) + sp_r); j++) {
                        if (mark[j]) continue;
                        if (transfer && !std::isnan(anchor_loss[j])
                            && anchor_loss[j] < gs_thr)
                            continue;
                        mark[j] = 1; n_jump++;
                    }
                }
            }
            std::vector<long> cand;
            for (size_t i = 0; i < n_all; i++)
                if (mark[i] && (transfer || (long(i) % gr_k) != 0))
                    cand.push_back(long(i));
            std::vector<float> fine;
            par_map_v2(threads, cand.size(), [&](size_t k) {
                return float(sim_at(Ax, feex, size_t(cand[k])));
            }, fine);
            // outward walk from every simulated start >= rf_beta x threshold
            size_t n_fill = 0;
            std::vector<float> fill;
            std::vector<long> fill_idx;
            {
                std::vector<double> seen;
                seen.reserve(n_coarse + fine.size());
                for (float v : coarse) seen.push_back(v);
                for (float v : fine) seen.push_back(v);
                std::vector<double> sorted_seen = seen;
                std::sort(sorted_seen.begin(), sorted_seen.end(), std::greater<double>());
                long mm0 = std::min<long>(m, long(sorted_seen.size()));
                double thr = sorted_seen[mm0 - 1] * rf_beta;
                std::vector<char> done(n_all, 0);
                for (size_t k = 0; k < n_coarse; k++) done[k * gr_k] = 1;
                for (long i : cand) done[i] = 1;
                std::vector<long> frontier;
                auto consider = [&](long i, double v) {
                    if (v < thr) return;
                    for (long j = i - 1; j <= i + 1; j += 2)
                        if (j >= 0 && j < long(n_all) && !done[j]) {
                            done[j] = 1; frontier.push_back(j);
                        }
                };
                for (size_t k = 0; k < n_coarse; k++)
                    consider(long(k * gr_k), coarse[k]);
                for (size_t k = 0; k < cand.size(); k++)
                    consider(cand[k], fine[k]);
                while (!frontier.empty()) {
                    std::vector<long> batch;
                    batch.swap(frontier);
                    std::vector<float> res;
                    par_map_v2(threads, batch.size(), [&](size_t k) {
                        return float(sim_at(Ax, feex, size_t(batch[k])));
                    }, res);
                    for (size_t k = 0; k < batch.size(); k++) {
                        fill_idx.push_back(batch[k]);
                        fill.push_back(res[k]);
                        consider(batch[k], res[k]);
                    }
                }
                n_fill = fill.size();
            }
            if (!cand_out.empty()) {
                char sufc[32];
                std::snprintf(sufc, sizeof sufc, ".c%d", ci_out);
                std::ofstream f(cand_out + sufc);
                for (size_t k = 0; k < n_coarse; k++) f << k * gr_k << '\n';
                for (long i : cand) f << i << '\n';
                for (long i : fill_idx) f << i << '\n';
            }
            std::vector<double> all;
            all.reserve(n_coarse + fine.size() + fill.size());
            for (float v : coarse) all.push_back(v);
            for (float v : fine) all.push_back(v);
            for (float v : fill) all.push_back(v);
            std::sort(all.begin(), all.end(), std::greater<double>());
            double top = 0.0;
            long mm = std::min<long>(m, long(all.size()));
            for (long i = 0; i < mm; i++) top += all[i];
            top /= double(mm);
            if (!have_anchor) {
                anchor_loss.assign(n_all, std::nanf(""));
                for (size_t k = 0; k < n_coarse; k++)
                    anchor_loss[k * gr_k] = coarse[k];
                for (size_t k = 0; k < cand.size(); k++)
                    anchor_loss[cand[k]] = fine[k];
                for (size_t k = 0; k < fill_idx.size(); k++)
                    anchor_loss[fill_idx[k]] = fill[k];
                anchor_thr = all[mm - 1];
                have_anchor = true;
            }
            double secs = std::chrono::duration<double>(
                std::chrono::steady_clock::now() - t0).count();
            if (R > 1) {
                acc_loss[ci_out] += top;
                acc_max[ci_out]  += all[0];
                acc_secs[ci_out] += secs;
                acc_sims[ci_out] += double(n_coarse + fine.size() + fill.size());
                cellA[ci_out] = Ax; cellFee[ci_out] = feex;
                cellTransfer[ci_out] = transfer ? 1 : 0;
                std::fprintf(stderr, "reality %d cell %d done (%.2fs) "
                             "loss %.6f max %.6f\n", rseed, ci_out, secs,
                             top * 100.0, all[0] * 100.0);
                ci_out++;
                continue;
            }
            std::printf("{\"A\": %.10g, \"fee\": %.10g, \"loss_pct\": %.6f, "
                        "\"max_pct\": %.6f, \"mode\": \"grid-refine\", "
                        "\"k\": %ld, \"C\": %ld, \"spike_w\": %ld, "
                        "\"spike_r\": %ld, \"n_spike\": %zu, "
                        "\"spike_jump\": %g, \"n_jump\": %zu, "
                        "\"spike_bands\": %g, \"refine_stride\": %ld, "
                        "\"fill_beta\": %g, \"n_fill\": %zu, "
                        "\"refine_radius\": %ld, \"transfer\": %s, "
                        "\"transfer_r\": %ld, \"auto\": %s, \"L\": %zu, "
                        "\"warmup\": %ld, \"n_all\": %zu, \"m\": %ld, "
                        "\"n_sims\": %zu, \"threshold_pct\": %.6f, "
                        "\"secs\": %.2f}\n",
                        Ax, feex, top * 100.0, all[0] * 100.0, gr_k, gr_c,
                        sp_w, sp_r, n_spike, sp_jump, n_jump, sp_bands, s_rf,
                        rf_beta, n_fill, rf_radius, transfer ? "true" : "false",
                        tr_r, auto_mode ? "true" : "false", L, warmup, n_all,
                        mm, n_coarse + fine.size() + fill.size(),
                        all[mm - 1] * 100.0, secs);
            std::fflush(stdout);
            ci_out++;
        }
    }   // realities loop
    if (int(std::max(1L, realities)) > 1) {
        const int R2 = int(realities);
        long m2 = std::max(1L, long(std::ceil(double(n_all) * tail_frac)));
        for (size_t ci = 0; ci < n_cells; ci++)
            std::printf("{\"A\": %.10g, \"fee\": %.10g, \"loss_pct\": %.6f, "
                        "\"max_pct\": %.6f, \"mode\": \"grid-refine\", "
                        "\"n_realities\": %d, \"transfer\": %s, "
                        "\"auto\": %s, \"L\": %zu, \"warmup\": %ld, "
                        "\"n_all\": %zu, \"m\": %ld, \"n_sims\": %zu, "
                        "\"secs\": %.2f}\n",
                        cellA[ci], cellFee[ci],
                        acc_loss[ci] / R2 * 100.0, acc_max[ci] / R2 * 100.0,
                        R2, cellTransfer[ci] ? "true" : "false",
                        auto_mode ? "true" : "false", L, warmup, n_all, m2,
                        size_t(acc_sims[ci] / R2), acc_secs[ci]);
    }
    return 0;
}
