from abc import ABC, abstractmethod


class BasePriceOracle(ABC):
    @abstractmethod
    def calculate_oracle_prices(self, price_data: list): ...


class EmaPriceOracle(BasePriceOracle):
    def __init__(self, t_exp: int):
        self.t_exp = t_exp  # in seconds

    def calculate_oracle_prices(self, price_data: list) -> list:
        """
        Important: price data time is in seconds
        """
        data = []

        ema = price_data[0][1]
        ema_t = price_data[0][0]
        for t, _, _, _, close, _ in price_data:
            ema_mul = 2 ** (-(t - ema_t) / self.t_exp)
            ema = ema * ema_mul + close * (1 - ema_mul)
            ema_t = t
            data.append(ema)

        return data
