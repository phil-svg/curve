// u256.hpp — 256-bit integer aliases that mimic Vyper unsafe_* semantics.
//
// We use Boost.Multiprecision cpp_int with fixed width, unchecked overflow, and
// unsigned/signed variants. Vyper's `unsafe_add/sub/mul/div` wrap without
// bounds checks, which matches Boost's `cpp_int_backend<256, 256, ..., unchecked, void>`
// behaviour for well-defined arithmetic modulo 2^256.
//
// Notes:
// * All 18-decimal fixed-point values are stored as u256 (bare integer).
// * Band indices `n` in Vyper are int256; we mirror that with i256.
// * Right-shifts on i256 in Boost are arithmetic (sign-extending), matching Vyper
//   `shift` for negative shift amounts.

#pragma once
#include <boost/multiprecision/cpp_int.hpp>
#include <cstdint>
#include <string>

using boost::multiprecision::cpp_int_backend;
using boost::multiprecision::cpp_int;
using boost::multiprecision::number;
using boost::multiprecision::signed_magnitude;
using boost::multiprecision::unsigned_magnitude;
using boost::multiprecision::unchecked;

using u256 = number<cpp_int_backend<256, 256, unsigned_magnitude, unchecked, void>>;
using i256 = number<cpp_int_backend<256, 256, signed_magnitude,   unchecked, void>>;
// z256: arbitrary-precision signed int, used internally where Vyper's signed
// two's-complement 256-bit ops don't map to Boost's sign-magnitude i256 without
// surprises around convert(int256, uint256). We do the math in z256 then
// truncate to 256 bits explicitly.
using z256 = cpp_int;

// Truncate a possibly-negative arbitrary-precision int to Vyper `uint256`
// semantics: return x mod 2^256 as a non-negative u256.
inline u256 to_uint256_mod(const z256& x) {
    static const z256 MOD = z256(1) << 256;
    z256 t = x % MOD;
    if (t < 0) t += MOD;
    return u256(t);
}
inline z256 as_z256(const u256& x) { return z256(x); }
inline z256 as_z256(const i256& x) { return z256(x); }

// Convenience: powers of 10 as constexpr (rebuilt each call — cheap for our sizes).
inline u256 pow10(int e) {
    u256 r = 1;
    for (int i = 0; i < e; ++i) r *= 10;
    return r;
}

// Common constants used all over the port.
inline const u256& ONE_1E18() { static const u256 v = pow10(18); return v; }
inline const u256& ONE_1E36() { static const u256 v = pow10(36); return v; }

// Vyper-esque unsafe_* wrappers. In C++ these are just plain operators on
// unchecked cpp_int types, but naming them keeps the port readable line-for-line.
inline u256 unsafe_add(u256 a, u256 b) { return a + b; }
inline u256 unsafe_sub(u256 a, u256 b) { return a - b; }
inline u256 unsafe_mul(u256 a, u256 b) { return a * b; }
inline u256 unsafe_div(u256 a, u256 b) { return a / b; }
inline i256 unsafe_add(i256 a, i256 b) { return a + b; }
inline i256 unsafe_sub(i256 a, i256 b) { return a - b; }
inline i256 unsafe_mul(i256 a, i256 b) { return a * b; }
inline i256 unsafe_div(i256 a, i256 b) { return a / b; }

// pow_mod256: Vyper builtin that returns (a**b) mod 2^256.
inline u256 pow_mod256(u256 base, u256 exp) {
    u256 r = 1;
    for (u256 i = 0; i < exp; i += 1) r *= base;    // small exponents only
    return r;
}

// Bit-safe conversions.
inline u256 to_u256(const std::string& s) { return u256(s); }
inline i256 to_i256(const std::string& s) { return i256(s); }
