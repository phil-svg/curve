// replay_yb.cpp — transaction-cascade replay driver for the Yield Basis
// AMM (levamm.hpp) + LT vault (lt.hpp) engines.
//
//   replay_yb --job job.json
//
// One YB transaction emits an ordered cascade of events (an arb tx may
// exchange several times; a deposit adds liquidity, mints shares and then
// distributes fees). The driver walks the tx's events in log order as a
// sequential simulation, carrying AMM + LT state from step to step, and
// diffs every recomputable field wei-for-wei.
//
// Job JSON: { "txs": [ { "block": n, "i": idx, "kind": label,
//   "pre": { "amm": {collateral,debt,fee,minted,redeemed,stables},
//            "lt": {admin(signed str),total,ideal_staked,staked,supply,
//                   staked_tokens,has_staker,min_admin_fee,stables},
//            "p_lp": ..., "p_lt": ..., "lp_price": ... },
//   "steps": [ {"t":"ex","i":0|1,"in":...,"out":...,"p_o":...}
//            | {"t":"add","d_coll":...,"d_debt":...,"invariant":...,
//               "p_o":...}
//            | {"t":"dep","shares":...}
//            | {"t":"wd","shares":...}
//            | {"t":"rem","d_coll":...,"d_debt":...}
//            | {"t":"fees","amount":...,"new_supply":...}
//            | {"t":"bfees","amount":...,"min_amount":...,"discount":...}
//            | {"t":"afees","amount":...} ] } ] }
#include "lt.hpp"

#include <fstream>
#include <iostream>
#include <nlohmann/json.hpp>

using nlohmann::json;
using namespace levamm;
using namespace ltvault;

static u256 U(const json& j) {
    if (j.is_string()) return to_u256(j.get<std::string>());
    return u256(j.get<uint64_t>());
}
static z256 Zj(const json& j) { return z256(j.get<std::string>()); }

int main(int argc, char** argv) {
    std::string job_path;
    for (int i = 1; i + 1 < argc; i += 2)
        if (std::string(argv[i]) == "--job") job_path = argv[i + 1];
    if (job_path.empty()) {
        std::cerr << "usage: replay_yb --job job.json\n";
        return 2;
    }
    json job;
    std::ifstream(job_path) >> job;
    Params P;
    const bool adjust = job.value("adjust", false);

    size_t n = 0, mism = 0;
    json field_ok = {{"exchange_out", 0}, {"add_invariant", 0},
                     {"rem_pair", 0}, {"fees", 0}, {"dep_shares", 0},
                     {"afees_amount", 0}, {"bfees", 0}, {"bfees_min", 0}};
    json field_n = field_ok;

    for (const auto& tx : job.at("txs")) {
        n++;
        const auto& pj = tx.at("pre");
        const auto& aj = pj.at("amm");
        State amm;
        amm.collateral = U(aj.at("collateral"));
        amm.debt = U(aj.at("debt"));
        amm.fee = U(aj.at("fee"));
        amm.minted = U(aj.at("minted"));
        amm.redeemed = U(aj.at("redeemed"));
        amm.stables_balance = U(aj.at("stables"));
        LtState lt;
        const auto& lj = pj.at("lt");
        lt.liquidity.admin = Zj(lj.at("admin"));
        lt.liquidity.total = U(lj.at("total"));
        lt.liquidity.ideal_staked = U(lj.at("ideal_staked"));
        lt.liquidity.staked = U(lj.at("staked"));
        lt.total_supply = U(lj.at("supply"));
        lt.staked_tokens = U(lj.at("staked_tokens"));
        lt.has_staker = lj.at("has_staker").get<bool>();
        lt.min_admin_fee = U(lj.at("min_admin_fee"));
        u256 lt_stables = U(lj.at("stables"));
        const u256 p_lp = U(pj.at("p_lp"));
        const u256 p_lt = U(pj.at("p_lt"));
        const u256 lp_price = U(pj.at("lp_price"));
        const u256 xcp0 = pj.contains("xcp") ? U(pj.at("xcp")) : 0;
        const u256 vp0 = pj.contains("vp") ? U(pj.at("vp")) : 0;
        auto adj = [&](const u256& coll, const u256& xcp, const u256& vp) {
            return adjust && vp != 0 ? adjust_collateral(coll, xcp, vp)
                                     : coll;
        };

        json bad = json::object();
        auto cmp = [&](const char* name, const u256& got, const u256& want,
                       bool strict = true) {
            field_n[name] = field_n[name].get<int>() + 1;
            if (got == want)
                field_ok[name] = field_ok[name].get<int>() + 1;
            else if (strict)
                bad[name] = {{"got", got.str()}, {"want", want.str()}};
        };

        // pending deposit context (add step precedes dep step)
        u256 dep_value_after = 0;
        bool have_add = false;

        try {
            for (const auto& st : tx.at("steps")) {
                const std::string t = st.at("t").get<std::string>();
                if (t == "ex") {
                    auto r = exchange(P, amm, st.at("i").get<unsigned>(),
                                      U(st.at("in")), U(st.at("p_o")));
                    if (!r.ok)
                        throw std::runtime_error("exchange: " + r.err);
                    cmp("exchange_out", r.out_amount, U(st.at("out")));
                    if (st.at("i").get<unsigned>() == 0) {
                        amm.stables_balance += U(st.at("in"));
                        amm.redeemed += U(st.at("in"));
                    } else {
                        amm.stables_balance -= r.out_amount;
                        amm.minted += r.out_amount;
                    }
                    amm.collateral = r.collateral_after;
                    amm.debt = r.debt_after;
                } else if (t == "add") {
                    u256 d_coll = U(st.at("d_coll"));
                    u256 d_debt = U(st.at("d_debt"));
                    auto dr = amm_deposit(P, amm, d_coll, d_debt,
                                          U(st.at("p_o")));
                    cmp("add_invariant", dr.value, U(st.at("invariant")));
                    dep_value_after = dr.value;
                    u256 sxcp = st.contains("xcp") ? U(st.at("xcp")) : xcp0;
                    u256 svp = st.contains("vp") ? U(st.at("vp")) : vp0;
                    if (adjust && svp != 0)
                        dep_value_after = get_x0(
                            P, U(st.at("p_o")),
                            adj(amm.collateral + d_coll, sxcp, svp),
                            amm.debt + d_debt, true)
                            * E18() / (2 * P.leverage - E18());
                    have_add = true;
                    // shares math needs the PRE-deposit AMM value at the
                    // same oracle price — compute before mutating
                    if (lt.total_supply > 0) {
                        u256 vo = value_oracle(P, U(st.at("p_o")),
                                               adj(amm.collateral, sxcp,
                                                   svp), amm.debt);
                        auto lv = calculate_values(lt, p_lt, vo);
                        lt.liquidity.admin = lv.admin;
                        lt.liquidity.total = lv.total;
                        lt.liquidity.staked = lv.staked;
                        lt.total_supply = lv.supply_tokens;
                        lt.staked_tokens = lv.staked_tokens;
                    }
                    amm.collateral = dr.collateral_after;
                    amm.debt = dr.debt_after;
                    amm.minted += d_debt;
                    amm.stables_balance -= d_debt;
                } else if (t == "dep") {
                    u256 shares;
                    u256 value_after =
                        to_uint256_mod(as_z256(dep_value_after * E18() / p_lt)
                                       - lt.liquidity.admin);
                    if (lt.total_supply > 0 && lt.liquidity.total > 0
                            && have_add) {
                        u256 supply = lt.total_supply;
                        shares = supply * value_after / lt.liquidity.total
                               - supply;
                    } else {
                        shares = dep_value_after * E18() / p_lt;
                        value_after = shares + lt.total_supply;
                        lt.liquidity.admin = 0;
                        lt.liquidity.staked = 0;
                        lt.liquidity.ideal_staked = 0;
                        lt.staked_tokens = 0;
                    }
                    cmp("dep_shares", shares, U(st.at("shares")));
                    lt.liquidity.total = value_after;
                    lt.total_supply += U(st.at("shares"));  // chain truth
                    have_add = false;
                } else if (t == "wdraw") {
                    // merged RemoveLiquidityRaw + Withdraw (log-adjacent);
                    // mid-tx withdraws carry their own positioned oracles
                    u256 shares = U(st.at("shares"));
                    u256 s_lp = st.contains("p_lp") ? U(st.at("p_lp")) : p_lp;
                    u256 s_lt = st.contains("p_lt") ? U(st.at("p_lt")) : p_lt;
                    u256 vo = value_oracle(P, s_lp,
                                           adj(amm.collateral, xcp0, vp0),
                                           amm.debt);
                    auto lv = calculate_values(lt, s_lt, vo);
                    u256 frac = withdraw_frac(lv, shares);
                    auto wr = amm_withdraw(amm, frac);
                    field_n["rem_pair"] = field_n["rem_pair"].get<int>() + 1;
                    if (wr.d_collateral == U(st.at("d_coll"))
                        && wr.d_debt == U(st.at("d_debt")))
                        field_ok["rem_pair"] =
                            field_ok["rem_pair"].get<int>() + 1;
                    else
                        bad["rem_pair"] = {
                            {"got", {wr.d_collateral.str(), wr.d_debt.str()}},
                            {"want", {U(st.at("d_coll")).str(),
                                      U(st.at("d_debt")).str()}}};
                    lt.liquidity.admin = lv.admin;
                    lt.liquidity.staked = lv.staked;
                    lt.staked_tokens = lv.staked_tokens;
                    lt.liquidity.total = lv.total
                        * (lv.supply_tokens - shares) / lv.supply_tokens;
                    if (lv.admin < 0)
                        lt.liquidity.admin = ltvault::floordiv(
                            lv.admin * as_z256(lv.supply_tokens - shares),
                            as_z256(lv.supply_tokens));
                    lt.total_supply = lv.supply_tokens - shares;
                    amm.collateral -= U(st.at("d_coll"));
                    amm.debt -= U(st.at("d_debt"));
                    amm.redeemed += U(st.at("d_debt"));
                    amm.stables_balance += U(st.at("d_debt"));
                } else if (t == "fees") {
                    auto fr = collect_fees(amm);
                    field_n["fees"] = field_n["fees"].get<int>() + 1;
                    if (fr.amount == U(st.at("amount"))
                        && fr.new_supply == U(st.at("new_supply")))
                        field_ok["fees"] = field_ok["fees"].get<int>() + 1;
                    else
                        bad["fees"] = {
                            {"got", {fr.amount.str(), fr.new_supply.str()}},
                            {"want", {U(st.at("amount")).str(),
                                      U(st.at("new_supply")).str()}}};
                    amm.minted = fr.minted_after;
                    amm.stables_balance -= fr.amount;
                    lt_stables += fr.amount;
                } else if (t == "bfees") {
                    cmp("bfees", lt_stables, U(st.at("amount")));
                    cmp("bfees_min",
                        bfees_min_amount(lt_stables, U(st.at("discount")),
                                         lp_price),
                        U(st.at("min_amount")), false);
                    lt_stables = 0;   // everything went into the cryptopool
                } else if (t == "afees") {
                    u256 vo = value_oracle(P, p_lp,
                                           adj(amm.collateral, xcp0, vp0),
                                           amm.debt);
                    auto lv = calculate_values(lt, p_lt, vo);
                    cmp("afees_amount", admin_fee_mint(lv),
                        U(st.at("amount")));
                    u256 minted = admin_fee_mint(lv);
                    lt.liquidity.total = lv.total
                        + (lv.admin > 0 ? u256(lv.admin) : u256(0));
                    lt.liquidity.admin = 0;
                    lt.liquidity.staked = lv.staked;
                    lt.staked_tokens = lv.staked_tokens;
                    lt.total_supply = lv.supply_tokens + minted;
                }
            }
        } catch (const std::exception& e) {
            bad["error"] = e.what();
        }

        if (!bad.empty()) {
            mism++;
            std::cout << json{{"i", tx.at("i")}, {"block", tx.at("block")},
                              {"kind", tx.at("kind")},
                              {"bad", bad}}.dump() << "\n";
        }
    }
    std::cout << json{{"txs", n}, {"mismatches", mism},
                      {"fields_ok", field_ok},
                      {"fields_n", field_n}}.dump() << std::endl;
    return mism == 0 ? 0 : 1;
}
