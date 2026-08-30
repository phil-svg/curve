# yieldbasis — Yield Basis pool engine

`twocrypto_yb.hpp`: wei-exact replay state machine for the Yield Basis
Twocrypto v3.0.0 fork (stableswap math, donation protection, WBTC policy
controller). Header-only; it is compiled into ../pools/build/replay through
the include path (`make` in ../pools builds both) and depends on
../pools/src/crypto_math.hpp.

The OLDER Yield Basis pool family (Twocrypto v2.1.0d, also iREET) is a
different vintage and lives in ../pools/src/twocrypto_ng.hpp.

This is the canonical copy; the validation harness is maintained
privately alongside this repo.
