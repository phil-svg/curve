import random
from abc import ABC, abstractmethod
from collections import deque

from simulator.import_data.binance import BinanceImporter
from simulator.import_data.onchain import OnchainImporter
from simulator.settings import Pair


class BasePriceHistoryLoader(ABC):
    @abstractmethod
    def load_prices(self) -> list: ...


class GenericPriceHistoryLoader(BasePriceHistoryLoader):
    def __init__(self, pair: Pair, add_reverse: bool = True):
        if pair in [Pair.BTCUSDT, Pair.ETHUSDT, Pair.CRVUSDT]:
            self.importer = BinanceImporter()
        elif pair in [Pair.WSTETH_ETH]:
            self.importer = OnchainImporter()
        else:
            raise NotImplementedError("Unsupported importer type")

        self.pair = pair
        self.add_reverse = add_reverse

    def load_prices(self) -> list:
        data = self.importer.load(self.pair)

        # timestamp, OHLC, vol
        unfiltered_data = [[int(d[0])] + [float(x) for x in d[1:6]] for d in data]
        data = []
        prev_time = 0
        for d in unfiltered_data:
            if d[0] >= prev_time:
                data.append(d)
                prev_time = d[0]
        if self.add_reverse:
            t0 = data[-1][0]
            data += [[t0 + (t0 - d[0])] + d[1:] for d in data[::-1]]

        return data


class VolatilityPriceHistoryLoader(GenericPriceHistoryLoader):
    def __init__(self, pair: Pair, period: float = 1 / 48):
        super().__init__(pair, False)
        self.prices = self.load_prices()
        self.period = period  # days

        self.max_drawdown = None

        # used for checking if price in this period is down from open
        self.drawdown_index = 0.9

    def calculate_max_drawdown(self):
        current_window = deque()
        max_drawdown = 0

        for t, _, high, low, _, _ in self.prices:
            # Remove peaks older than period
            while current_window and current_window[0][0] < t - self.period * 60 * 60 * 24:
                current_window.popleft()

            current_window.append((t, high, low))
            current_high = max(w[1] for w in current_window)
            current_low = min(w[2] for w in current_window)

            if (current_drawdown := (current_high - current_low) / current_high) > max_drawdown:
                max_drawdown = current_drawdown

        self.max_drawdown = max_drawdown

    def load_random_period(self) -> list:
        day_fraction = 86400 / (self.prices[-1][0] - self.prices[0][0])  # Which fraction of all data is 1 day
        position_start = random.random()
        position_start = int(position_start * len(self.prices))
        position_end = position_start + int(self.period * day_fraction * len(self.prices))

        return self.prices[position_start:position_end]

    def change_period(self, period: list) -> tuple[bool, list]:
        """
        :param period: period in days
        :return: list of prices

        we want to rescale the prices to have the max drawdown that we have for any period of same size

        price_m = w_high - (w_high - price) * r  # r is some coefficient we try to find
        max_drawdown = max_window_drawdown_modified = (w_high - w_low_m) / w_high =
        = (w_high - (w_high - (w_high - w_low) * r)) / w_high = (w_high - w_low) * r / w_high

        r = (w_high / (w_high - w_low)) * max_drawdown = max_drawdown / window_drawdown
        """

        if self.max_drawdown is None:
            self.calculate_max_drawdown()

        # make drawdown for each period the same - worst case
        result = []
        window_high = max([p[2] for p in period])
        window_low = min([p[3] for p in period])
        current_drawdown = (window_high - window_low) / window_high
        rescaling_factor = self.max_drawdown / current_drawdown  # >= 1

        # rescale high, low and other dots - high stays the same, low gets lower
        for t, open, high, low, close, volume in period:
            open = window_high - (window_high - open) * rescaling_factor
            high = window_high - (window_high - high) * rescaling_factor
            low = window_high - (window_high - low) * rescaling_factor
            close = window_high - (window_high - close) * rescaling_factor

            result.append((t, open, high, low, close, volume))

        # we take into account periods in which prices go down
        # otherwise we won't get significant losses
        is_down = True
        open = period[0][1]
        if (open - window_low) / open < current_drawdown * self.drawdown_index:
            is_down = False

        return is_down, result

    def load_random_changed_period(self) -> tuple[bool, list]:
        return self.change_period(self.load_random_period())
