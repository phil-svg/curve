// replay.cpp — event replay driver for the PegKeeper engine.
//
//   replay --job job.json
//
// Job JSON (one keeper):
//   { "version": "V1"|"V2", "peg_mul": "<dec>",
//     "events": [ { "block": n, "ts": "<dec>", "kind": "provide"|"withdraw",
//                   "amount": "<dec>",            // chain event amount
//                   "chain_debt_after": "<dec>",  // debt() at event block
//                   "pre": { "last_change": ..., "action_delay": ...,
//                            "bal_pegged": ..., "bal_peg_raw": ...,
//                            "debt": ..., "pegged_balance": ...,
//                            "agg_price": ...,
//                            "reg": {  // V2 only
//                              "killed_provide": b, "killed_withdraw": b,
//                              "worst_price_threshold": ..., "price_deviation": ...,
//                              "alpha": ..., "beta": ...,
//                              "pk_debt": ..., "pk_stable_balance": ...,
//                              "infos": [ { "is_self": b, "is_inverse": b,
//                                           "price_oracle": ..., "get_p": ...,
//                                           "debt": ..., "stable_balance": ... } ] } } } ] }
//
// For every event the engine recomputes the action from pre-state and prints
// one JSON line when it disagrees with the chain; a summary line ends the run.
// Exit 0 = every event matched exactly.
#include "pegkeeper.hpp"

#include <fstream>
#include <iostream>
#include <nlohmann/json.hpp>

using nlohmann::json;
using namespace pegkeeper;

static u256 U(const json& j) {
    if (j.is_string()) return to_u256(j.get<std::string>());
    return u256(j.get<uint64_t>());
}

int main(int argc, char** argv) {
    std::string job_path;
    for (int i = 1; i + 1 < argc; i += 2)
        if (std::string(argv[i]) == "--job") job_path = argv[i + 1];
    if (job_path.empty()) { std::cerr << "usage: replay --job job.json\n"; return 2; }

    json job;
    std::ifstream(job_path) >> job;
    const bool v2 = job.at("version").get<std::string>() == "V2";
    const u256 peg_mul = U(job.at("peg_mul"));

    size_t n = 0, amount_ok = 0, debt_ok = 0, kind_ok = 0, mism = 0;
    size_t reg_checked = 0, reg_exact = 0, reg_ext = 0;
    for (const auto& ev : job.at("events")) {
        const size_t idx = n;
        n++;
        const auto& pj = ev.at("pre");
        PreState p;
        p.ts = U(ev.at("ts"));
        p.last_change = U(pj.at("last_change"));
        p.action_delay = U(pj.at("action_delay"));
        p.bal_pegged = U(pj.at("bal_pegged"));
        p.bal_peg_raw = U(pj.at("bal_peg_raw"));
        p.debt = U(pj.at("debt"));
        p.pegged_balance = U(pj.at("pegged_balance"));
        p.agg_price = U(pj.at("agg_price"));
        if (v2) {
            RegState r;
            const auto& rj = pj.at("reg");
            r.killed_provide = rj.at("killed_provide").get<bool>();
            r.killed_withdraw = rj.at("killed_withdraw").get<bool>();
            r.agg_price = p.agg_price;
            r.worst_price_threshold = U(rj.at("worst_price_threshold"));
            r.price_deviation = U(rj.at("price_deviation"));
            r.alpha = U(rj.at("alpha"));
            r.beta = U(rj.at("beta"));
            r.pk_debt = U(rj.at("pk_debt"));
            r.pk_stable_balance = U(rj.at("pk_stable_balance"));
            for (const auto& ij : rj.at("infos")) {
                RegKeeperInfo i;
                i.is_self = ij.at("is_self").get<bool>();
                i.is_inverse = ij.at("is_inverse").get<bool>();
                i.price_oracle = U(ij.at("price_oracle"));
                if (ij.contains("get_p")) i.get_p = U(ij.at("get_p"));
                if (ij.contains("debt")) i.debt = U(ij.at("debt"));
                if (ij.contains("stable_balance"))
                    i.stable_balance = U(ij.at("stable_balance"));
                r.infos.push_back(i);
            }
            p.reg = r;
        }

        Action a = v2 ? update_v2(p, peg_mul) : update_v1(p, peg_mul);
        // allowed_chain: the regulator's answer read on-chain at the exact
        // call position. Where the keeper's regulator is the standard
        // PegKeeperRegulator our port must agree (counted in reg_exact);
        // where it is an external contract (e.g. PegKeeperOffboarding) the
        // chain value substitutes and the event counts as reg_ext.
        if (v2 && ev.at("pre").contains("allowed_chain")) {
            const u256 ac = U(ev.at("pre").at("allowed_chain"));
            reg_checked++;
            if (ac == a.allowed) {
                reg_exact++;
            } else {
                reg_ext++;
                Action b;
                b.debt_after = p.debt;
                b.allowed = ac;
                const u256 bal_peg = p.bal_peg_raw * peg_mul;
                if (!(p.last_change + p.action_delay > p.ts)) {
                    if (bal_peg > p.bal_pegged) {
                        if (ac == 0) { b.kind = Action::BLOCKED; }
                        else {
                            u256 amt = unsafe_sub(bal_peg, p.bal_pegged) / 5;
                            if (ac < amt) amt = ac;
                            if (p.pegged_balance < amt) amt = p.pegged_balance;
                            b.kind = Action::PROVIDE; b.amount = amt;
                            b.debt_after = p.debt + amt;
                        }
                    } else {
                        if (ac == 0) { b.kind = Action::BLOCKED; }
                        else {
                            u256 amt = unsafe_sub(p.bal_pegged, bal_peg) / 5;
                            if (ac < amt) amt = ac;
                            if (p.debt < amt) amt = p.debt;
                            b.kind = Action::WITHDRAW; b.amount = amt;
                            b.debt_after = p.debt - amt;
                        }
                    }
                }
                a = b;
            }
        }
        const std::string want_kind = ev.at("kind").get<std::string>();
        const u256 want_amt = U(ev.at("amount"));
        const u256 want_debt = U(ev.at("chain_debt_after"));
        const std::string got_kind =
            a.kind == Action::PROVIDE ? "provide"
            : a.kind == Action::WITHDRAW ? "withdraw"
            : a.kind == Action::BLOCKED ? "blocked" : "none";
        bool k = got_kind == want_kind;
        bool am = k && a.amount == want_amt;
        bool db = k && a.debt_after == want_debt;
        kind_ok += k; amount_ok += am; debt_ok += db;
        if (!(k && am && db)) {
            mism++;
            {
                std::cout << json{{"i", idx}, {"block", ev.at("block")},
                    {"want", {{"kind", want_kind},
                              {"amount", want_amt.str()},
                              {"debt_after", want_debt.str()}}},
                    {"got", {{"kind", got_kind},
                             {"amount", a.amount.str()},
                             {"debt_after", a.debt_after.str()},
                             {"allowed", a.allowed.str()}}}}.dump()
                    << "\n";
            }
        }
    }
    std::cout << json{{"events", n}, {"kind_ok", kind_ok},
                      {"amount_ok", amount_ok}, {"debt_ok", debt_ok},
                      {"reg_checked", reg_checked}, {"reg_exact", reg_exact},
                      {"reg_ext", reg_ext},
                      {"mismatches", mism}}.dump() << std::endl;
    return mism == 0 ? 0 : 1;
}
