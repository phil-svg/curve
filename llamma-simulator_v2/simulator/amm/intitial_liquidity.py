from abc import ABC, abstractmethod
from typing import Any


class BaseRangeInitialLiquidity(ABC):

    def __init__(self, p0: float, dn: int):
        self.p0 = p0
        self.dn = dn

    @abstractmethod
    def deposit(self, amm: Any, initial_liquidity: float): ...


class ConstantInitialLiquidity(BaseRangeInitialLiquidity):

    def deposit(self, amm: Any, initial_liquidity: float):
        amm.deposit_nrange(initial_liquidity, self.p0, self.dn)
