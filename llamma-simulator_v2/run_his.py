"""Run zchf_crvusd/sweep_parameters.py UNMODIFIED on macOS: import it as a
real module (so `simulate` is picklable for the pool) and force fork so the
workers inherit MARKET/ORACLE, as his script assumes on Linux."""
import multiprocessing as mp, sys
mp.set_start_method("fork", force=True)
from zchf_crvusd import sweep_parameters as sp
sys.argv = ["sweep_parameters.py"] + sys.argv[1:]
raise SystemExit(sp.main())
