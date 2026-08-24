import logging

from simulator.calculation import Calculator
from simulator.logging import setup_logger

setup_logger()
logger = logging.getLogger(__name__)


# Change parameters before running
def calculate_a() -> None:
    """
    Iterate through range of A to find best A

    pair - name of pair - "BTCUSDT"
    t_exp - exponential time for oracle EMA
    A - AMM parameter A
    samples - number of samples to iterate through
    n_top_samples - number of top samples to choose (worst case)
    initial_liquidity_range - number of bands initially to have liquidity
    """

    results = Calculator.simulate_A(
        pair="BTCUSDT",
        fee=0.002,
        t_exp=600,
        samples=2_000_000,
        n_top_samples=50,
        dynamic_fee_multiplier=0.25,
        initial_liquidity_range=4,
        is_v2=True,
    )
    logger.info(f"Results: {results}")


if __name__ == "__main__":
    calculate_a()
