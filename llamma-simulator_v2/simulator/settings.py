from enum import StrEnum
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


class Pair(StrEnum):
    BTCUSDT = "BTCUSDT"
    ETHUSDT = "ETHUSDT"
    CRVUSDT = "CRVUSDT"
    WSTETH_ETH = "WSTETH_ETH"
