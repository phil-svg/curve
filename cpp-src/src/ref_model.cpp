// ref_model.cpp — C++ port of the llamma-simulator reference model
// (refsim/libmodel.py + the single_run/get_loss_rate path of
// libsimulate.py), double precision, algorithm-identical.
//
// Purpose: the S.L./D.L. "reference table" mode. The Python original costs
// ~150ms per sampled loan; this port runs the same loan in ~50us, so the
// published-table statistic (deep tail of 100k+ loans per cell) fits in an
// interactive run.
//
// Fidelity notes (quirks preserved on purpose — they shape the numbers):
//  * dynamic_fee(new=True) COMPOUNDS old_dfee once per band visited in
//    find_target_price's scan, not once per candle.
//  * find_target_price scans the DEPOSIT band range (min_band..max_band
//    fixed at deposit time), falling through to a flat-fee target.
//  * high is computed before low, both before either trade executes.
//  * the |band| > 1000 guard aborts the sample and scores it 0.0 (their
//    `f` wrapper swallows exceptions and returns 0).
// Input klines: JSON [[t_ms, open, high, low, close, vol], ...].

#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <filesystem>
#include <fstream>
#include <random>
#include <string>
#include <thread>
#include <vector>
#include <algorithm>
#include <nlohmann/json.hpp>

using json = nlohmann::json;

struct Candle { double t, o, h, l, c; };

// ---------------------------------------------------------------- RefAMM --
struct RefAMM {
    static constexpr int OFF = 1200;      // band index offset; guard at 1000
    double p_base, p_oracle, prev_p_oracle, old_dfee = 0.0;
    double A, fee;
    int po_fee_delay = 2;                 // their scan_param "other" default
    bool has_history = false;
    int active_band = 0;
    int min_band = 0, max_band = 0;       // deposit range (never updated)
    int lo_seen = 0, hi_seen = 0;         // bands that may hold funds
    std::vector<double> bx, by;
    bool failed = false;                  // |band|>1000 guard tripped
    // k^n for n in [-OFF, OFF], shared per cell (filled with the same
    // std::pow(k, n) values the per-step code used to compute, so every
    // band price is bit-identical; just computed once instead of ~20x per
    // candle step)
    const double *kpow = nullptr;
    double po3 = 0.0;                     // p_oracle^3, refreshed on set
    // get_p() memo: a pure function of (active band, its x/y, p_oracle);
    // invalidated whenever any of those changes. Same arithmetic, computed
    // once per step instead of three times on no-trade steps.
    double p_memo = 0.0;
    bool p_valid = false;
    // dynamic-fee ingredients that only change when the oracle moves:
    // 1 - r^3 with r = min/max(prev, cur) oracle, and (delay-1)/delay
    double po_move = 0.0;
    double delay_f = 0.5;

    RefAMM(double p_base_, double A_, double fee_, const double *kpow_)
        : p_base(p_base_), p_oracle(p_base_), prev_p_oracle(p_base_),
          A(A_), fee(fee_), bx(2 * OFF + 1, 0.0), by(2 * OFF + 1, 0.0),
          kpow(kpow_), po3(p_base_ * p_base_ * p_base_) {}

    double &X(int n) { return bx[n + OFF]; }
    double &Y(int n) { return by[n + OFF]; }

    void set_p_oracle(double p) {
        // use_po_fee: prev <- last SET value (initial: p_base)
        if (has_history) prev_p_oracle = p_oracle;
        has_history = true;
        p_oracle = p;
        po3 = p * p * p;
        p_valid = false;
        double r = std::min(prev_p_oracle, p_oracle)
                 / std::max(prev_p_oracle, p_oracle);
        po_move = 1.0 - r * r * r;
        delay_f = double(po_fee_delay - 1) / double(po_fee_delay);
    }

    double dynamic_fee(bool fresh) {
        double f = fee;
        if (fresh) {
            // same expression as the original, with the per-step constants
            // hoisted: (old + (1 - r^3)) * (delay-1) / delay
            f = (old_dfee + po_move) * double(po_fee_delay - 1)
                / double(po_fee_delay);
            old_dfee = f;
            f = std::max(f, fee);
        } else {
            f = std::max(old_dfee, fee);
        }
        return f;   // dynamic_fee_multiplier = 0 in the table recipe
    }

    double k() const { return (A - 1.0) / A; }
    double kp(int n) const { return kpow[n + OFF]; }
    double p_top(int n) const { return p_base * kp(n); }
    double p_down(int n) const {
        double pb = p_base * kp(n);
        return po3 / (pb * pb);
    }
    double p_up(int n) const {
        double pb = p_base * kp(n + 1);
        return po3 / (pb * pb);
    }
    int get_band_n(double p) const {
        return int(std::floor(std::log(p / p_base) / std::log(k())));
    }

    void deposit_nrange(double amount, double p, int dn) {
        int n_top = get_band_n(p_oracle) + 1;
        int n1 = std::max(get_band_n(p), n_top);
        int n2 = n1 + dn - 1;
        double y = amount / dn;
        min_band = n1; max_band = n2;
        lo_seen = n1; hi_seen = n2;
        for (int i = n1; i <= n2; i++) Y(i) += y;
        p_valid = false;
    }

    double get_y0(int n) {
        double x = X(n), y = Y(n);
        double pt = p_top(n);
        double b = pt / p_oracle * (A - 1.0) * x
                 + p_oracle * p_oracle / pt * A * y;
        double a = p_oracle * A;
        double D = b * b + 4.0 * a * x * y;
        return (b + std::sqrt(D)) / (2.0 * a);
    }
    double get_f(double y0, int n) {
        double pt = p_top(n);
        return y0 * p_oracle * p_oracle / pt * A;
    }
    double get_g(double y0, int n) {
        double pt = p_top(n);
        return y0 * pt / p_oracle * (A - 1.0);
    }
    double get_p() {
        if (p_valid) return p_memo;
        int n = active_band;
        double x = X(n), y = Y(n);
        if (x == 0.0 && y == 0.0)
            p_memo = std::sqrt(p_up(n) * p_down(n));
        else {
            double y0 = get_y0(n);
            p_memo = (get_f(y0, n) + x) / (get_g(y0, n) + y);
        }
        p_valid = true;
        return p_memo;
    }

    void trade_to_price(double price) {
        int bstep;
        if (X(active_band) == 0.0 && Y(active_band) == 0.0) {
            if (price > p_up(active_band)) bstep = 1;
            else if (price < p_down(active_band)) bstep = -1;
            else return;
        } else {
            double cur = get_p();
            if (price > cur) bstep = 1;
            else if (price < cur) bstep = -1;
            else return;
        }
        double original_price = price;
        p_valid = false;   // the loop below mutates x/y/active_band
        while (true) {
            int n = active_band;
            if (n < lo_seen) lo_seen = n;
            if (n > hi_seen) hi_seen = n;
            double y0 = get_y0(n);
            double g = get_g(y0, n), f = get_f(y0, n);
            double x = X(n), y = Y(n);
            double Inv = (f + x) * (g + y);
            price = original_price;

            if (x == 0.0 && y == 0.0) {
                if (price >= p_down(n) && price <= p_up(n)) break;
                active_band += bstep;
                if (std::abs(active_band) > 1000) { failed = true; return; }
                continue;
            }

            double fe = dynamic_fee(false);
            double p_c_d = p_down(n), p_c_u = p_up(n);

            if (bstep == 1) {
                price = price * (1.0 - fe);
                if (price < p_c_d) break;
                double y_dest = std::sqrt(Inv / price) - g;
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
                double x_dest = std::sqrt(Inv * price) - f;
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
            if (std::abs(active_band) > 1000) { failed = true; return; }
        }
        if (active_band < lo_seen) lo_seen = active_band;
        if (active_band > hi_seen) hi_seen = active_band;
    }

    double get_x_down(int n) {
        double x = X(n), y = Y(n);
        if (x == 0.0 && y == 0.0) return 0.0;
        double p_o = p_oracle;
        double p_o_up = p_top(n);
        double p_o_down = p_o_up * k();
        double p_mid = p_o * p_o * p_o / (p_o_down * p_o_down) * k();
        double sbr = std::sqrt(A / (A - 1.0));

        if (x == 0.0 || y == 0.0) {
            if (p_o > p_o_up) {
                double y_eq = (y == 0.0) ? x / p_mid : y;
                return y_eq * p_o_up / sbr;
            } else if (p_o < p_o_down) {
                double x_eq = (x == 0.0) ? y * p_mid : x;
                return x_eq;
            }
        }
        double y0 = get_y0(n);
        double g = get_g(y0, n), f = get_f(y0, n);
        double Inv = (f + x) * (g + y);
        if (p_o > p_o_up) {
            double y_o = std::max(Inv / f, g) - g;
            return y_o * p_o_up / sbr;
        } else if (p_o < p_o_down) {
            double x_o = std::max(Inv / g, f) - f;
            return x_o;
        }
        double y_o = A * y0 * (1.0 - p_o_down / p_o);
        double x_o = std::max(Inv / (g + y_o), f) - f;
        return x_o + y_o * std::sqrt(p_o_down * p_o);
    }
    double get_all_x() {
        double s = 0.0;
        for (int n = lo_seen - 1; n <= hi_seen + 1; n++) s += get_x_down(n);
        return s;
    }
};

// ------------------------------------------------------------ single_run --
// find_target_price scans the deposit range with the compounding po-fee.
static double find_target_price(RefAMM &amm, double p, bool is_up, bool fresh) {
    if (is_up) {
        for (int n = amm.max_band; n >= amm.min_band; n--) {
            double pd = amm.p_down(n);
            double dfee = amm.dynamic_fee(fresh);
            double pd_ = pd * (1.0 + dfee);
            if (p > pd_) {
                double pu = amm.p_up(n);
                double pu_ = pu * (1.0 + dfee);
                return (p - pd_) / (pu_ - pd_) * (pu - pd) + pd;
            }
        }
        return p * (1.0 - amm.dynamic_fee(false));
    }
    for (int n = amm.min_band; n <= amm.max_band; n++) {
        double pu = amm.p_up(n);
        double dfee = amm.dynamic_fee(fresh);
        double pu_ = pu * (1.0 - dfee);
        if (p < pu_) {
            double pd = amm.p_down(n);
            double pd_ = pd * (1.0 - dfee);
            return pu - (pu_ - p) / (pu_ - pd_) * (pu - pd);
        }
    }
    return p * (1.0 + amm.dynamic_fee(false));
}

// rescale < 0: raw window (classic). rescale >= 0: the v2
// VolatilityPriceHistoryLoader convention — stretch the window from its own
// high so its drawdown equals the span's max drawdown (passed as maxdd):
// p' = w_high - (w_high - p) * r, r = maxdd / max(window_dd, 0.001).
// The oracle EMA is then seeded from the (rescaled) global EMA at the
// window start and evolved over the rescaled mids.
static double single_run_idx(const std::vector<Candle> &data,
                             const std::vector<double> &emas,
                             double A, int range_size, double fee,
                             double ext_fee, size_t i0, size_t i1,
                             double texp, double maxdd,
                             const double *kpow) {
    size_t n_all = data.size();
    if (i0 >= n_all) return 0.0;
    i1 = std::min(i1, n_all);

    double w_high = 0.0, r = 1.0;
    bool rescale = maxdd >= 0.0;
    if (rescale) {
        double w_low = 1e300;
        for (size_t i = i0; i < i1; i++) {
            w_high = std::max(w_high, data[i].h);
            w_low = std::min(w_low, data[i].l);
        }
        if (!(w_high > 0.0)) return 0.0;
        double dd = std::max((w_high - w_low) / w_high, 0.001);
        r = maxdd / dd;
    }
    auto resc = [&](double v) {
        return rescale ? w_high - (w_high - v) * r : v;
    };

    double p0 = resc(data[i0].o);
    if (!(p0 > 0.0)) return 0.0;   // a violent stretch can cross zero
    double p_base = p0 * (A / (A - 1.0) + 1e-4);
    RefAMM amm(p_base, A, fee, kpow);
    amm.deposit_nrange(1.0, p0, range_size);
    double initial_all_x = amm.get_all_x();
    if (!(initial_all_x > 0.0)) return 0.0;

    double ema = resc(emas[i0]), ema_t = data[i0].t;
    for (size_t i = i0; i < i1; i++) {
        double h = resc(data[i].h), l = resc(data[i].l);
        if (!(l > 0.0)) return 0.0;
        if (rescale) {
            if (i > i0) {
                double mul = std::pow(2.0, -(data[i].t - ema_t)
                                             / (1000.0 * texp));
                ema = ema * mul + (l + h) / 2.0 * (1.0 - mul);
            }
            ema_t = data[i].t;
            amm.set_p_oracle(ema);
        } else {
            amm.set_p_oracle(emas[i]);
        }
        double high = find_target_price(amm, h * (1.0 - ext_fee),
                                        true, true);
        double low = find_target_price(amm, l * (1.0 + ext_fee),
                                       false, false);
        if (high > amm.get_p()) amm.trade_to_price(high);
        if (amm.failed) return 0.0;
        if (low < amm.get_p()) amm.trade_to_price(low);
        if (amm.failed) return 0.0;
    }
    double loss = 1.0 - amm.get_all_x() / initial_all_x;
    if (!std::isfinite(loss)) return 0.0;
    return loss;
}

// classic entry: start = fraction of the (mirrored) series, as in the
// Python original (kept bit-identical)
// k^n table for one A: exactly the values std::pow(k, n) returns
static std::vector<double> make_kpow(double A) {
    double k = (A - 1.0) / A;
    std::vector<double> t(2 * RefAMM::OFF + 1);
    for (int n = -RefAMM::OFF; n <= RefAMM::OFF; n++)
        t[n + RefAMM::OFF] = std::pow(k, n);
    return t;
}

static double single_run(const std::vector<Candle> &data,
                         const std::vector<double> &emas,
                         double A, int range_size, double fee,
                         double ext_fee, double position, double size,
                         double texp, double maxdd, const double *kpow) {
    size_t n_all = data.size();
    size_t i0 = size_t(position * n_all);
    size_t i1 = size_t((position + size) * n_all);
    return single_run_idx(data, emas, A, range_size, fee, ext_fee, i0, i1,
                          texp, maxdd, kpow);
}

// ------------------------------------------------------- screening proxies --
// Two cheap path features per start offset, no AMM math:
//   P1 oscillation: zig-zag total variation (log) of the high/low sequence
//      clipped to the band corridor [p0*((A-1)/A)^(N+1), p0*A/(A-1)], with a
//      reversal threshold of base fee + external fee (smaller wiggles don't
//      trigger the arb);
//   P2 oracle lag: sum of |log(mid/ema)| over candles touching the corridor
//      (a fast move against a lagging EMA is what the arb exploits).
struct Proxies { double p1, p2; };
static Proxies proxies_for(const std::vector<Candle> &data,
                           const std::vector<double> &emas,
                           double A, int range_size, double theta,
                           size_t i0, size_t i1) {
    size_t n_all = data.size();
    if (i0 >= n_all) return {0.0, 0.0};
    i1 = std::min(i1, n_all);
    double p0 = data[i0].o;
    if (!(p0 > 0.0)) return {0.0, 0.0};
    double g = (A - 1.0) / A;
    double lo_c = p0 * std::pow(g, range_size + 1), hi_c = p0 / g;
    auto clip = [&](double v) { return std::min(hi_c, std::max(lo_c, v)); };
    double pivot = clip(p0), ext = pivot, tv = 0.0, lag = 0.0;
    int dir = 0;
    auto feed = [&](double v) {
        if (dir == 0) {
            if (v >= pivot * (1.0 + theta)) { dir = 1; ext = v; }
            else if (v <= pivot * (1.0 - theta)) { dir = -1; ext = v; }
        } else if (dir == 1) {
            if (v > ext) ext = v;
            else if (v <= ext * (1.0 - theta)) {
                tv += std::log(ext / pivot); pivot = ext; dir = -1; ext = v;
            }
        } else {
            if (v < ext) ext = v;
            else if (v >= ext * (1.0 + theta)) {
                tv += std::log(pivot / ext); pivot = ext; dir = 1; ext = v;
            }
        }
    };
    for (size_t i = i0; i < i1; i++) {
        double h = data[i].h, l = data[i].l;
        if (!(l > 0.0)) break;
        feed(clip(h));
        feed(clip(l));
        if (l <= hi_c && h >= lo_c && emas[i] > 0.0)
            lag += std::fabs(std::log(((h + l) / 2.0) / emas[i]));
    }
    tv += std::fabs(std::log(ext / pivot));
    return {tv, lag};
}

// worst (high-low)/high over every horizon-length window (monotonic deques)
static double max_window_drawdown(const std::vector<Candle> &data,
                                  double horizon_ms) {
    std::deque<std::pair<double, double>> hi, lo;
    double maxdd = 0.0;
    for (const auto &c : data) {
        double t0 = c.t - horizon_ms;
        while (!hi.empty() && hi.front().first < t0) hi.pop_front();
        while (!lo.empty() && lo.front().first < t0) lo.pop_front();
        while (!hi.empty() && hi.back().second <= c.h) hi.pop_back();
        hi.push_back({c.t, c.h});
        while (!lo.empty() && lo.back().second >= c.l) lo.pop_back();
        lo.push_back({c.t, c.l});
        double H = hi.front().second, L = lo.front().second;
        if (H > 0.0) maxdd = std::max(maxdd, (H - L) / H);
    }
    return maxdd;
}

// ---------------------------------------------------------------- realities --
// Perturbed price histories (the arb_sim scheme): flip the sign of a random
// 10% of the small-half |log-return| bars per seed, add a constant bias so
// the cumulative drift is preserved, reconstruct prices from bar 0. Applied
// to the candle mids; each candle's o/h/l/c is scaled by the mid ratio.
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
    // small half of the NONZERO returns: on dense series (BTC/ETH minute
    // data, ~no flat bars) this is identical to the plain bottom-50% rule;
    // on sparse ones (ZCHF: ~94% flat minutes) the plain rule degenerates
    // to flipping zero bars, i.e. no perturbation at all
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
// EMA of (l+h)/2 with half-life texp seconds (ms timestamps) — the same
// recipe as the internal oracle; used to carry a perturbation into an
// external oracle series: EMA is linear, so oracle' = oracle x EMA'/EMA.
static std::vector<double> ema_of(const std::vector<Candle> &data, double texp) {
    std::vector<double> e(data.size());
    double ema = data[0].o, ema_t = data[0].t;
    for (size_t i = 0; i < data.size(); i++) {
        double mul = std::pow(2.0, -(data[i].t - ema_t) / (1000.0 * texp));
        ema = ema * mul + (data[i].l + data[i].h) / 2.0 * (1.0 - mul);
        ema_t = data[i].t;
        e[i] = ema;
    }
    return e;
}

// ------------------------------------------------------------------ main --
int main(int argc, char **argv) {
    std::string klines_path;
    std::vector<double> a_list, fee_list;   // fee in percent
    long samples = 10000, n_top = 0, seed = 1234;
    int range_size = 1;
    double texp = 600.0, ext_fee = 5e-4;
    double loan_days = 0.15;
    double probe_pos = -1.0;   // --probe p: one deterministic sample, exit
    std::string oracle_path;   // --oracle f.json: external oracle per row
    std::string dump_all;      // --dump-all f: exhaustive losses (f32) per start
    std::string scan_out;      // --scan-out f: proxies P1,P2 (f32) per start
    std::string starts_file;   // --starts-file f: exact runs for listed starts
    long stride = 1;           // --stride k: every k-th start (dump/scan)
    double tail_frac = 0.0005; // --tail-frac f: grid-refine statistic = mean
                               // of the worst f of ALL start offsets
    long sp_w = 720, sp_r = 2; // --spikes W,R: also simulate every start whose
                               // open is a new high vs the previous W opens,
                               // +-R minutes; W=0 off
    double sp_jump = 0.5;      // --spike-jump J: also simulate every start
                               // within +-R minutes of a one-minute open move
                               // of >= J bands (J/A), either direction, no
                               // pre-check; 0 = off
    double sp_bands = 2.0;     // --spike-bands d: keep a spike start only if
                               // the lows within the loan fall >= d bands
                               // (d/A) below its open — a knife-edge loss
                               // needs a reversal through the bands; 0 = off
    long rf_radius = 200;      // --refine-radius r: refine +-r minutes around
                               // each top-C coarse hit (validation: +-100
                               // missed a 65-min plateau sitting between two
                               // grid points that both fell in a dip)
    long rf_stride = 2;        // --refine-stride s: refine the +-k
                               // neighbourhoods every s-th minute, then
    double tr_gamma = 0.4, tr_gs = 0.5;  // --transfer gamma,r,gs: cells after
    long tr_r = 10;            // the first (anchor) simulate only the anchor's
                               // starts with loss >= gamma x anchor tail
                               // threshold, +-r minutes (stride s), plus their
                               // own spike starts (those the anchor already
                               // simulated are kept only if anchor loss >=
                               // gs x threshold). gamma <= 0 disables.
    std::string cand_out;      // --cand-out f: write the simulated start
                               // indices (grid-refine) for recall checks
    double rf_beta = 0.7;      // --fill-beta b: fill in the +-(s-1) gaps
                               // around every simulated start whose loss is
                               // >= b x the tail threshold seen so far
    long gr_k = 0, gr_c = 0;   // --grid-refine k,C: exhaustive tail via
                               // coarse grid (every k-th start) + stride-1
                               // refinement within +-k of the top C hits
    long realities = 1;        // --realities N: average the table over N
                               // perturbed price histories (N=1: original)
    bool auto_mode = false;    // --auto: grid-refine with every search
                               // parameter derived from the loan length L
                               // (candles); explicit flags still override.
                               // Calibrated so L = 2880 (2-day loans, 1-min
                               // candles) reproduces the validated settings
                               // k=100 C=200 radius=200 stride=2 W=720 r=10.
    bool set_k = false, set_radius = false, set_stride = false,
         set_spikes = false, set_transfer = false;
    bool add_reverse = true;
    bool do_rescale = false;   // --rescale: v2 drawdown-stretch per window
    int threads = int(std::thread::hardware_concurrency());

    auto parse_list = [](const std::string &s) {
        std::vector<double> out;
        size_t p = 0;
        while (p < s.size()) {
            size_t q = s.find(',', p);
            if (q == std::string::npos) q = s.size();
            out.push_back(std::stod(s.substr(p, q - p)));
            p = q + 1;
        }
        return out;
    };
    for (int i = 1; i < argc - 1; i++) {
        std::string a = argv[i], v = argv[i + 1];
        if (a == "--klines") klines_path = v;
        else if (a == "--a-list") a_list = parse_list(v);
        else if (a == "--fee-list") fee_list = parse_list(v);
        else if (a == "--samples") samples = std::stol(v);
        else if (a == "--n-top") n_top = std::stol(v);
        else if (a == "--range-size") range_size = std::stoi(v);
        else if (a == "--loan-days") loan_days = std::stod(v);
        else if (a == "--texp") texp = std::stod(v);
        else if (a == "--ext-fee") ext_fee = std::stod(v);
        else if (a == "--seed") seed = std::stol(v);
        else if (a == "--threads") threads = std::stoi(v);
        else if (a == "--no-reverse") add_reverse = false;
        else if (a == "--probe") probe_pos = std::stod(v);
        else if (a == "--oracle") oracle_path = v;
        else if (a == "--dump-all") dump_all = v;
        else if (a == "--scan-out") scan_out = v;
        else if (a == "--starts-file") starts_file = v;
        else if (a == "--stride") stride = std::stol(v);
        else if (a == "--tail-frac") tail_frac = std::stod(v);
        else if (a == "--spikes") {
            auto kv = parse_list(v);
            if (kv.size() == 2) { sp_w = long(kv[0]); sp_r = long(kv[1]); set_spikes = true; }
        }
        else if (a == "--spike-bands") sp_bands = std::stod(v);
        else if (a == "--spike-jump") sp_jump = std::stod(v);
        else if (a == "--refine-stride") { rf_stride = std::stol(v); set_stride = true; }
        else if (a == "--refine-radius") { rf_radius = std::stol(v); set_radius = true; }
        else if (a == "--fill-beta") rf_beta = std::stod(v);
        else if (a == "--cand-out") cand_out = v;
        else if (a == "--transfer") {
            auto kv = parse_list(v);
            if (kv.size() == 3) { tr_gamma = kv[0]; tr_r = long(kv[1]); tr_gs = kv[2]; set_transfer = true; }
        }
        else if (a == "--grid-refine") {
            auto kv = parse_list(v);
            if (kv.size() == 2) { gr_k = long(kv[0]); gr_c = long(kv[1]); set_k = true; }
        }
        else if (a == "--realities") realities = std::stol(v);
    }
    for (int i = 1; i < argc; i++) {  // bare flags, no value
        if (std::string(argv[i]) == "--rescale") do_rescale = true;
        if (std::string(argv[i]) == "--no-reverse") add_reverse = false;
        if (std::string(argv[i]) == "--auto") auto_mode = true;
    }
    if (klines_path.empty() || a_list.empty() || fee_list.empty()) {
        std::fprintf(stderr, "usage: ref_model --klines f.json --a-list "
                             "100,140 --fee-list 0.1,0.2 [--samples N] ...\n");
        return 2;
    }
    if (n_top <= 0) n_top = std::max(1L, samples / 10000);

    // load klines, monotonic-time filter (their loader), optional mirror.
    // Parsing 80-180 MB of JSON costs ~1-2 s per run, so the filtered
    // candles are cached next to the file as <klines>.bin (raw doubles,
    // rewritten whenever the JSON is newer); same for the oracle file.
    std::vector<Candle> data;
    {
        std::string bin = klines_path + ".bin";
        bool fresh = false;
        if (auto bf = std::ifstream(bin, std::ios::binary); bf) {
            std::error_code ec;
            auto tj = std::filesystem::last_write_time(klines_path, ec);
            auto tb = std::filesystem::last_write_time(bin, ec);
            if (!ec && tb >= tj) {
                bf.seekg(0, std::ios::end);
                size_t n = size_t(bf.tellg()) / sizeof(Candle);
                bf.seekg(0);
                data.resize(n);
                bf.read(reinterpret_cast<char *>(data.data()),
                        std::streamsize(n * sizeof(Candle)));
                fresh = bool(bf);
            }
        }
        if (!fresh) {
            data.clear();
            std::ifstream f(klines_path);
            json j; f >> j;
            double prev_t = -1e30;
            data.reserve(j.size());
            for (auto &r : j) {
                double t = r[0].get<double>();
                if (t < prev_t) continue;
                prev_t = t;
                data.push_back({t, r[1].get<double>(), r[2].get<double>(),
                                r[3].get<double>(), r[4].get<double>()});
            }
            std::ofstream of(bin, std::ios::binary);   // best effort
            if (of) of.write(reinterpret_cast<const char *>(data.data()),
                             std::streamsize(data.size() * sizeof(Candle)));
        }
        data.reserve(data.size() * (add_reverse ? 2 : 1));
        if (add_reverse) {
            double t0 = data.back().t;
            size_t n = data.size();
            for (size_t i = 0; i < n; i++) {
                Candle c = data[n - 1 - i];
                c.t = t0 + (t0 - c.t);
                data.push_back(c);
            }
        }
    }
    // EMA on (high+low)/2, half-life texp seconds, seeded from first open
    std::vector<double> emas(data.size());
    if (!oracle_path.empty()) {
        std::vector<double> o;
        std::string obin = oracle_path + ".bin";
        bool ofresh = false;
        if (auto bf = std::ifstream(obin, std::ios::binary); bf) {
            std::error_code ec;
            auto tj = std::filesystem::last_write_time(oracle_path, ec);
            auto tb = std::filesystem::last_write_time(obin, ec);
            if (!ec && tb >= tj) {
                bf.seekg(0, std::ios::end);
                size_t n = size_t(bf.tellg()) / sizeof(double);
                bf.seekg(0);
                o.resize(n);
                bf.read(reinterpret_cast<char *>(o.data()),
                        std::streamsize(n * sizeof(double)));
                ofresh = bool(bf);
            }
        }
        if (!ofresh) {
            std::ifstream f(oracle_path); json j; f >> j;
            o = j.get<std::vector<double>>();
            std::ofstream of(obin, std::ios::binary);
            if (of) of.write(reinterpret_cast<const char *>(o.data()),
                             std::streamsize(o.size() * sizeof(double)));
        }
        if (add_reverse) { size_t n = o.size(); for (size_t i = 0; i < n; i++) o.push_back(o[n - 1 - i]); }
        if (o.size() != data.size()) { std::fprintf(stderr, "oracle size mismatch\n"); return 3; }
        emas = o;
    } else {
        double ema = data[0].o, ema_t = data[0].t;
        for (size_t i = 0; i < data.size(); i++) {
            double mul = std::pow(2.0, -(data[i].t - ema_t) / (1000.0 * texp));
            ema = ema * mul + (data[i].l + data[i].h) / 2.0 * (1.0 - mul);
            ema_t = data[i].t;
            emas[i] = ema;
        }
    }
    double day_frac = 86400.0 * 1000.0 / (data.back().t - data.front().t);
    double size_frac = loan_days * day_frac;
    double maxdd = -1.0;
    if (do_rescale) {
        maxdd = max_window_drawdown(data, loan_days * 86400.0 * 1000.0);
        std::fprintf(stderr, "maxdd %.5f over %.2f-day windows\n",
                     maxdd, loan_days);
    }

    if (probe_pos >= 0.0) {
        for (double A : a_list)
            for (double fee_pct : fee_list)
                std::printf("probe A=%g fee=%g pos=%.10f loss=%.12f\n",
                            A, fee_pct, probe_pos,
                            single_run(data, emas, A, range_size,
                                       fee_pct / 100.0, ext_fee,
                                       probe_pos, size_frac, texp, maxdd,
                                       make_kpow(A).data()));
        return 0;
    }

    size_t n_all = data.size();
    size_t L = size_t(std::llround(size_frac * double(n_all)));

    if (auto_mode) {
        // Adaptive v3: the worst-start clusters scale with the loan length
        // (two starts see the same path when their offset << L), so every
        // search length scales with L. Anchored at L = 2880 -> the validated
        // 2-day settings. Explicit flags win.
        long m_auto = std::max(1L, long(std::ceil(double(n_all) * tail_frac)));
        double Ld = double(L);
        if (!set_k) {
            gr_k = std::max(1L, long(std::llround(Ld / 28.8)));
            // top-C: ~m/k coarse points fall inside the worst-m clusters;
            // keep the 2-day margin (200 over 801/100 = 8) -> 25 x m/k, min 200
            gr_c = std::max(200L, long(25.0 * double(m_auto) / double(gr_k)));
        } else if (gr_c <= 0) {
            gr_c = std::max(200L, long(25.0 * double(m_auto) / double(gr_k)));
        }
        if (!set_radius) rf_radius = std::max(2L, 2 * gr_k);
        if (!set_stride) rf_stride = gr_k >= 8 ? 2 : 1;
        if (!set_spikes) { sp_w = std::max(15L, long(std::llround(Ld / 4.0))); sp_r = 2; }
        if (!set_transfer) tr_r = std::max(1L, long(std::llround(Ld / 288.0)));
        std::fprintf(stderr, "auto: L=%zu k=%ld C=%ld radius=%ld stride=%ld "
                             "spikes=%ld,%ld transfer_r=%ld m=%ld\n",
                     L, gr_k, gr_c, rf_radius, rf_stride, sp_w, sp_r, tr_r, m_auto);
    }

    // parallel map over start offsets: fn(i0) -> float, written to out
    auto par_map = [&](size_t n_starts, auto fn, std::vector<float> &out) {
        out.assign(n_starts, 0.0f);
        std::vector<std::thread> pool;
        size_t per = (n_starts + threads - 1) / threads;
        for (int th = 0; th < threads; th++)
            pool.emplace_back([&, th]() {
                size_t lo = th * per, hi = std::min(n_starts, (th + 1) * per);
                for (size_t k = lo; k < hi; k++) out[k] = float(fn(k));
            });
        for (auto &t : pool) t.join();
    };
    auto write_f32 = [](const std::string &path, const std::vector<float> &v) {
        std::ofstream f(path, std::ios::binary);
        f.write(reinterpret_cast<const char *>(v.data()),
                std::streamsize(v.size() * sizeof(float)));
    };

    if (gr_k > 0 && gr_c > 0) {
        // Exhaustive-tail mode. The worst 0.05% of ALL start offsets lie in
        // a few contiguous runs (loss is locally smooth in the start), so:
        // 1) simulate every k-th start, 2) take the top C coarse hits,
        // 3) simulate every start within +-k of them, 4) the statistic is
        // the mean of the worst m = ceil(0.0005 * n_all) losses among the
        // simulated starts (n_top fraction from --n-top/--samples if given).
        // Deterministic: no seed, no sampling noise.
        long m = std::max(1L, long(std::ceil(double(n_all) * tail_frac)));
        // --realities R: rerun the whole table on R perturbed histories and
        // average per cell. R = 1 keeps the original series and per-cell
        // output byte-identical to before.
        const int R = int(std::max(1L, realities));
        const size_t n_cells = a_list.size() * fee_list.size();
        std::vector<double> acc_loss(n_cells, 0.0), acc_max(n_cells, 0.0),
                            acc_secs(n_cells, 0.0), acc_sims(n_cells, 0.0);
        std::vector<double> cellA(n_cells), cellFee(n_cells);
        std::vector<char> cellTransfer(n_cells, 0);
        std::vector<Candle> data0;
        std::vector<double> emas0, mid0, emaO;
        RealityPrep rp;
        const size_t n_base = add_reverse ? data.size() / 2 : data.size();
        if (R > 1) {
            data0 = data; emas0 = emas;
            mid0.resize(n_base);
            for (size_t i = 0; i < n_base; i++)
                mid0[i] = (data0[i].h + data0[i].l) / 2.0;
            rp = prep_reality(mid0);
            if (!oracle_path.empty()) emaO = ema_of(data0, texp);
        }
        for (int rseed = 0; rseed < R; rseed++) {
        if (R > 1) {
            auto midp = reality_mids(rp, mid0, uint64_t(rseed));
            for (size_t i = 0; i < n_base; i++) {
                double ratio = midp[i] / mid0[i];
                Candle c = data0[i];
                c.o *= ratio; c.h *= ratio; c.l *= ratio; c.c *= ratio;
                data[i] = c;
            }
            if (add_reverse)
                for (size_t i = 0; i < n_base; i++) {
                    Candle c = data[n_base - 1 - i];
                    c.t = data0[n_base + i].t;
                    data[n_base + i] = c;
                }
            if (!oracle_path.empty()) {
                // EMA is linear: perturbed oracle = oracle x EMA'/EMA, the
                // aggregate leg (oracle / EMA) is untouched
                auto emaP = ema_of(data, texp);
                for (size_t i = 0; i < data.size(); i++)
                    emas[i] = emas0[i] * emaP[i] / emaO[i];
            } else {
                emas = ema_of(data, texp);
            }
        }
        int ci_out = 0;
        // anchor (first cell) knowledge for the transfer: loss per simulated
        // start (NaN = not simulated) and its tail threshold
        std::vector<float> anchor_loss;
        double anchor_thr = 0.0;
        bool have_anchor = false;
        for (double A : a_list) {
            std::vector<double> kpow_tab = make_kpow(A);
            const double *kpow = kpow_tab.data();
            for (double fee_pct : fee_list) {
                double fee = fee_pct / 100.0;
                auto t0 = std::chrono::steady_clock::now();
                // transfer only while the base fee is at most ~one band
                // width (fee*A <= 1); beyond that the cell's worst starts
                // are a different population than the anchor's (see the v2
                // duration validation, fee 2.919%) — full search instead.
                bool transfer = have_anchor && tr_gamma > 0.0 && fee * A <= 1.0;
                long s_rf = std::max(1L, rf_stride);
                size_t n_coarse = 0;
                std::vector<float> coarse;
                std::vector<char> mark(n_all, 0);
                if (!transfer) {
                    n_coarse = (n_all + gr_k - 1) / gr_k;
                    par_map(n_coarse, [&](size_t k) {
                        size_t i0 = k * gr_k;
                        return single_run_idx(data, emas, A, range_size, fee,
                                              ext_fee, i0, i0 + L, texp, maxdd, kpow);
                    }, coarse);
                    std::vector<size_t> idx(n_coarse);
                    for (size_t k = 0; k < n_coarse; k++) idx[k] = k;
                    size_t C = std::min<size_t>(gr_c, n_coarse);
                    std::partial_sort(idx.begin(), idx.begin() + C, idx.end(),
                                      [&](size_t a, size_t b) {
                                          return coarse[a] > coarse[b]; });
                    // refinement set: every s-th start within +-k of a top-C
                    // hit (gaps are filled below, around whatever turns out large)
                    long rad = rf_radius > 0 ? rf_radius : gr_k;
                    for (size_t j = 0; j < C; j++) {
                        long c = long(idx[j]) * gr_k;
                        for (long i = std::max(0L, c - rad);
                             i <= std::min(long(n_all) - 1, c + rad); i += s_rf)
                            mark[i] = 1;
                    }
                } else {
                    // transfer: the anchor's hot starts +-r at stride s
                    double hot = tr_gamma * anchor_thr;
                    for (size_t i = 0; i < n_all; i++) {
                        if (!(anchor_loss[i] >= hot)) continue;   // NaN -> false
                        for (long j = std::max(0L, long(i) - tr_r);
                             j <= std::min(long(n_all) - 1, long(i) + tr_r); j += s_rf)
                            mark[j] = 1;
                    }
                }
                // forward minimum of the lows over the loan length, for the
                // spike pre-check (monotone deque, O(n))
                std::vector<double> fwd_min;
                if (sp_w > 0 && sp_bands > 0.0) {
                    fwd_min.assign(n_all, 0.0);
                    std::deque<size_t> dq;
                    for (long i = long(n_all) - 1; i >= 0; i--) {
                        while (!dq.empty() && data[dq.back()].l >= data[i].l)
                            dq.pop_back();
                        dq.push_back(size_t(i));
                        while (!dq.empty() && dq.front() >= size_t(i) + L)
                            dq.pop_front();
                        fwd_min[i] = data[dq.front()].l;
                    }
                }
                // spike starts: a loan opened at the exact minute the price
                // makes a new high gets its bands placed right under that
                // high; if the move reverses, the bands are swept — a 1-3
                // minute knife-edge that sits between grid points. Detected
                // from the opens alone (no simulation): open > max of the
                // previous W opens, +-R minutes around it. (A "peak only"
                // variant missed rising ramps; see the Validation tab.)
                size_t n_spike = 0;
                if (sp_w > 0) {
                    std::deque<size_t> dq;   // sliding max of opens (prev W)
                    std::vector<double> prevmax(n_all, -1e300);
                    for (size_t i = 0; i < n_all; i++) {
                        if (!dq.empty()) prevmax[i] = data[dq.front()].o;
                        while (!dq.empty() && data[dq.back()].o <= data[i].o)
                            dq.pop_back();
                        dq.push_back(i);
                        while (!dq.empty() && dq.front() + size_t(sp_w) <= i)
                            dq.pop_front();
                    }
                    double depth = 1.0 - sp_bands / A;
                    double gs_thr = tr_gs * anchor_thr;
                    for (size_t i = 0; i < n_all; i++) {
                        if (data[i].o > prevmax[i]) {
                            for (long j = std::max(0L, long(i) - sp_r);
                                 j <= std::min(long(n_all) - 1, long(i) + sp_r); j++) {
                                if (mark[j]) continue;
                                if (!fwd_min.empty() && data[j].o > 0.0
                                    && fwd_min[j] / data[j].o > depth)
                                    continue;   // never reaches band 2: no knife-edge
                                // transfer: a spike start the anchor already
                                // simulated is kept only if it mattered there
                                if (transfer && !std::isnan(anchor_loss[j])
                                    && anchor_loss[j] < gs_thr)
                                    continue;
                                mark[j] = 1; n_spike++;
                            }
                        }
                    }
                }
                // jump starts: a loan opened within +-R minutes of a
                // one-minute open move of >= J bands (either direction).
                // Up-jump: the oracle EMA lags the market at open, the bands
                // are placed against the lagging oracle and the catch-up
                // churns them — no later drop needed (validation: 1-day ZCHF
                // clusters starting at a +1.6% minute, never falling after).
                // Down-jump: the starts right before a sharp drop. Both are
                // 1-10 minute knife-edges between grid points. No pre-check.
                size_t n_jump = 0;
                if (sp_jump > 0.0) {
                    double jthr = sp_jump / A;
                    double gs_thr = tr_gs * anchor_thr;
                    for (size_t i = 0; i + 1 < n_all; i++) {
                        if (!(data[i].o > 0.0)) continue;
                        double mv = std::fabs(data[i + 1].o / data[i].o - 1.0);
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
                    if (mark[i] && (transfer || (i % gr_k) != 0))
                        cand.push_back(long(i));
                std::vector<float> fine;
                par_map(cand.size(), [&](size_t k) {
                    size_t i0 = size_t(cand[k]);
                    return single_run_idx(data, emas, A, range_size, fee,
                                          ext_fee, i0, i0 + L, texp, maxdd, kpow);
                }, fine);
                // outward walk (fill-in): every simulated start whose loss
                // is >= rf_beta x the tail threshold seen so far gets its
                // un-simulated neighbours (+-1) simulated; the walk repeats
                // from the newly simulated ones while they stay above the
                // threshold, so a cluster touched at one minute is followed
                // to both edges. Covers the stride-s gaps of the refinement
                // and the 6-11 minute decay after a jump start, which a
                // single +-(s-1) pass could not (validation: 1-day ZCHF).
                size_t n_fill = 0;
                std::vector<float> fill;
                std::vector<long> fill_idx;
                {
                    std::vector<double> seen;
                    seen.reserve(n_coarse + fine.size());
                    for (float v : coarse) seen.push_back(v);
                    for (float v : fine) seen.push_back(v);
                    std::vector<double> sorted_seen = seen;
                    std::sort(sorted_seen.begin(), sorted_seen.end(),
                              std::greater<double>());
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
                        par_map(batch.size(), [&](size_t k) {
                            size_t i0 = size_t(batch[k]);
                            return single_run_idx(data, emas, A, range_size,
                                                  fee, ext_fee, i0, i0 + L,
                                                  texp, maxdd, kpow);
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
                if (R == 1) {
                std::printf("{\"A\": %.10g, \"fee_pct\": %.10g, "
                            "\"loss_pct\": %.6f, \"max_pct\": %.6f, "
                            "\"mode\": \"grid-refine\", "
                            "\"k\": %ld, \"C\": %ld, \"spike_w\": %ld, "
                            "\"spike_r\": %ld, \"n_spike\": %zu, "
                            "\"spike_jump\": %g, \"n_jump\": %zu, "
                            "\"spike_bands\": %g, \"refine_stride\": %ld, "
                            "\"fill_beta\": %g, \"n_fill\": %zu, "
                            "\"refine_radius\": %ld, \"transfer\": %s, "
                            "\"transfer_r\": %ld, \"auto\": %s, \"L\": %zu, "
                            "\"n_all\": %zu, \"m\": %ld, \"n_sims\": %zu, "
                            "\"threshold_pct\": %.6f, \"secs\": %.2f}\n",
                            A, fee_pct, top * 100.0, all[0] * 100.0,
                            gr_k, gr_c, sp_w, sp_r,
                            n_spike, sp_jump, n_jump, sp_bands, s_rf, rf_beta, n_fill,
                            rf_radius, transfer ? "true" : "false", tr_r,
                            auto_mode ? "true" : "false", L, n_all,
                            mm, n_coarse + fine.size() + fill.size(),
                            all[mm - 1] * 100.0, secs);
                std::fflush(stdout);
                } else {
                    acc_loss[ci_out] += top;
                    acc_max[ci_out]  += all[0];
                    acc_secs[ci_out] += secs;
                    acc_sims[ci_out] += double(n_coarse + fine.size() + fill.size());
                    cellA[ci_out] = A; cellFee[ci_out] = fee_pct;
                    cellTransfer[ci_out] = transfer ? 1 : 0;
                    std::fprintf(stderr, "reality %d cell %d done (%.2fs) "
                                 "loss %.6f max %.6f\n",
                                 rseed, ci_out, secs, top * 100.0, all[0] * 100.0);
                }
                ci_out++;
            }
        }
        }   // realities loop
        if (R > 1)
            for (size_t ci = 0; ci < n_cells; ci++)
                std::printf("{\"A\": %.10g, \"fee_pct\": %.10g, "
                            "\"loss_pct\": %.6f, \"max_pct\": %.6f, "
                            "\"mode\": \"grid-refine\", "
                            "\"n_realities\": %d, "
                            "\"transfer\": %s, \"auto\": %s, \"L\": %zu, "
                            "\"n_all\": %zu, \"m\": %ld, \"n_sims\": %zu, "
                            "\"secs\": %.2f}\n",
                            cellA[ci], cellFee[ci],
                            acc_loss[ci] / R * 100.0, acc_max[ci] / R * 100.0,
                            R, cellTransfer[ci] ? "true" : "false",
                            auto_mode ? "true" : "false", L, n_all, m,
                            size_t(acc_sims[ci] / R), acc_secs[ci]);
        return 0;
    }

    if (!dump_all.empty() || !scan_out.empty() || !starts_file.empty()) {
        size_t n_starts = (n_all + stride - 1) / stride;
        std::vector<long> starts;
        if (!starts_file.empty()) {
            std::ifstream f(starts_file);
            long v;
            while (f >> v) starts.push_back(v);
        }
        int ci = 0;
        for (double A : a_list) {
            std::vector<double> kpow_tab = make_kpow(A);
            const double *kpow = kpow_tab.data();
            for (double fee_pct : fee_list) {
                double fee = fee_pct / 100.0;
                char suf[32];
                std::snprintf(suf, sizeof suf, ".c%d", ci);
                if (!dump_all.empty()) {
                    std::vector<float> out;
                    par_map(n_starts, [&](size_t k) {
                        size_t i0 = k * stride;
                        return single_run_idx(data, emas, A, range_size, fee,
                                              ext_fee, i0, i0 + L, texp,
                                              maxdd, kpow);
                    }, out);
                    write_f32(dump_all + suf, out);
                    std::printf("{\"mode\": \"dump\", \"cell\": %d, \"A\": %g, "
                                "\"fee_pct\": %g, \"n_starts\": %zu, "
                                "\"stride\": %ld, \"L\": %zu}\n",
                                ci, A, fee_pct, n_starts, stride, L);
                }
                if (!scan_out.empty()) {
                    double theta = fee + ext_fee;
                    std::vector<float> p1, p2;
                    par_map(n_starts, [&](size_t k) {
                        size_t i0 = k * stride;
                        return proxies_for(data, emas, A, range_size, theta,
                                           i0, i0 + L).p1;
                    }, p1);
                    par_map(n_starts, [&](size_t k) {
                        size_t i0 = k * stride;
                        return proxies_for(data, emas, A, range_size, theta,
                                           i0, i0 + L).p2;
                    }, p2);
                    write_f32(scan_out + suf + ".p1", p1);
                    write_f32(scan_out + suf + ".p2", p2);
                    std::printf("{\"mode\": \"scan\", \"cell\": %d, \"A\": %g, "
                                "\"fee_pct\": %g, \"n_starts\": %zu, "
                                "\"stride\": %ld}\n", ci, A, fee_pct,
                                n_starts, stride);
                }
                if (!starts.empty()) {
                    std::vector<float> out;
                    par_map(starts.size(), [&](size_t k) {
                        size_t i0 = size_t(starts[k]);
                        return single_run_idx(data, emas, A, range_size, fee,
                                              ext_fee, i0, i0 + L, texp,
                                              maxdd, kpow);
                    }, out);
                    std::ofstream f(starts_file + ".out" + suf);
                    for (size_t k = 0; k < starts.size(); k++)
                        f << starts[k] << ' ' << out[k] << '\n';
                    std::printf("{\"mode\": \"starts\", \"cell\": %d, \"A\": %g, "
                                "\"fee_pct\": %g, \"n\": %zu}\n", ci, A,
                                fee_pct, starts.size());
                }
                std::fflush(stdout);
                ci++;
            }
        }
        return 0;
    }

    int cell_i = 0;
    for (double A : a_list) {
        std::vector<double> kpow_tab = make_kpow(A);
        const double *kpow = kpow_tab.data();
        for (double fee_pct : fee_list) {
            std::vector<double> losses(samples);
            std::vector<std::thread> pool;
            long per = (samples + threads - 1) / threads;
            for (int th = 0; th < threads; th++) {
                pool.emplace_back([&, th]() {
                    std::mt19937_64 rng(uint64_t(seed)
                        + uint64_t(cell_i) * 0x9E3779B97F4A7C15ULL
                        + uint64_t(th) * 0xBF58476D1CE4E5B9ULL);
                    std::uniform_real_distribution<double> U(0.0, 1.0);
                    long lo = th * per, hi = std::min<long>(samples,
                                                            (th + 1) * per);
                    for (long s = lo; s < hi; s++)
                        losses[s] = single_run(data, emas, A, range_size,
                                               fee_pct / 100.0, ext_fee,
                                               U(rng), size_frac, texp,
                                               maxdd, kpow);
                });
            }
            for (auto &t : pool) t.join();
            std::sort(losses.begin(), losses.end(), std::greater<double>());
            double top = 0.0;
            for (long i = 0; i < n_top; i++) top += losses[i];
            top /= double(n_top);
            // extra tail cuts (mean of the worst fraction) for recipe fitting
            std::string tails;
            for (double frac : {0.0001, 0.0005, 0.001, 0.002, 0.005, 0.01, 0.05}) {
                long m = std::max(1L, (long)std::ceil(samples * frac));
                double t = 0.0;
                for (long i = 0; i < m; i++) t += losses[i];
                char buf[64];
                std::snprintf(buf, sizeof buf, "\"%g\": %.6f, ", frac, t / m * 100.0);
                tails += buf;
            }
            if (tails.size() > 2) tails.resize(tails.size() - 2);
            std::printf("{\"A\": %.10g, \"fee_pct\": %.10g, "
                        "\"loss_pct\": %.6f, \"tails\": {%s}}\n",
                        A, fee_pct, top * 100.0, tails.c_str());
            std::fflush(stdout);
            cell_i++;
        }
    }
    return 0;
}
