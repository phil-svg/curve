from datetime import datetime, timezone

import matplotlib.pyplot as plt

from simulator.import_data.onchain.base import OnchainImporter
from simulator.settings import Pair


def _plot_candles(candles: list[list[float]]) -> None:
    timestamps = [datetime.fromtimestamp(row[0], tz=timezone.utc) for row in candles]
    closes = [row[4] for row in candles]

    plt.figure(figsize=(12, 6))
    plt.plot(timestamps, closes, label="Close")
    plt.xlabel("Time (UTC)")
    plt.ylabel("Price")
    plt.title("WStETH-ETH Onchain Candles")
    plt.legend()
    plt.tight_layout()
    plt.show()


def main() -> None:
    candles = OnchainImporter.load(Pair.WSTETH_ETH)
    if not candles:
        print("No candles returned")
        return
    _plot_candles(candles)


if __name__ == "__main__":
    main()
