import logging

import click

from simulator.calculation import Calculator
from simulator.import_data import BinanceImporter
from simulator.import_data.onchain import OnchainImporter
from simulator.logging import setup_logger
from simulator.settings import Pair

setup_logger()

logger = logging.getLogger(__name__)


@click.group("simulator")
def simulator_commands(): ...


@simulator_commands.command("import_data", short_help="import price data")
@click.argument("pair", type=click.STRING)
def import_data(pair: str) -> None:
    BinanceImporter.run(Pair(pair))


@simulator_commands.command("import_onchain_data", short_help="import price data")
@click.argument("pair", type=click.STRING)
def import_onchain_data(pair: str) -> None:
    OnchainImporter.run(Pair(pair))


# Change parameters before running
@simulator_commands.command("calculate_A", short_help="import price data")
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
        samples=500_000,
        n_top_samples=50,
        dynamic_fee_multiplier=0.25,
        initial_liquidity_range=4,
    )
    logger.info(f"Results: {results}")


if __name__ == "__main__":
    simulator_commands()
