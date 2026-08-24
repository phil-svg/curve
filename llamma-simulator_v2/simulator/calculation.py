import json
import logging

from numpy import log10, logspace

from simulator.amm.intitial_liquidity import ConstantInitialLiquidity
from simulator.amm.price_history_loader import GenericPriceHistoryLoader, VolatilityPriceHistoryLoader
from simulator.amm.price_oracle import EmaPriceOracle
from simulator.amm.simulator import get_loss_rate, get_loss_rate_v2
from simulator.settings import BASE_DIR, Pair

logger = logging.getLogger(__name__)


class Calculator:
    EXTERNAL_FEE = 5e-4  # fee paid by arbitragers to external platforms

    @classmethod
    def simulate_fee(
        cls,
        pair: str,
        a: int,
        t_exp: int,
        samples: int = 500000,
        n_top_samples: int = 50,
        dynamic_fee_multiplier: float | None = 0.25,
        min_loan_duration: float | None = None,
        max_loan_duration: float | None = None,
        initial_liquidity_range: int = 4,
        fee_range: list | None = None,
        is_v2: bool = False,
    ):
        price_oracle = EmaPriceOracle(t_exp=t_exp)
        if is_v2:
            price_history_loader = VolatilityPriceHistoryLoader(pair=Pair(pair))
        else:
            price_history_loader = GenericPriceHistoryLoader(pair=Pair(pair))

        losses = []
        discounts = []

        kwargs = {
            "samples": samples,
            "n_top_samples": n_top_samples,
            "A": a,
            "initial_liquidity_range": initial_liquidity_range,
            "dynamic_fee_multiplier": dynamic_fee_multiplier,
            "min_loan_duration": min_loan_duration,
            "max_loan_duration": max_loan_duration,
        }

        if fee_range is None:
            fee_range = [a / 1000 for a in range(1, 50, 2)]

        for fee in fee_range:
            kwargs_with_fee = {
                **kwargs,
                "fee": fee,
                "initial_liquidity_class": ConstantInitialLiquidity,
                "price_history_loader": price_history_loader,
                "price_oracle": price_oracle,
                "external_fee": cls.EXTERNAL_FEE,
            }
            if is_v2:
                loss = get_loss_rate_v2(**kwargs_with_fee)
            else:
                loss = get_loss_rate(**kwargs_with_fee)

            # Simplified formula
            # bands_coefficient = (((A - 1) / A) ** range_size) ** 0.5
            # More precise
            bands_coefficient = (
                sum(((a - 1) / a) ** (k + 0.5) for k in range(initial_liquidity_range)) / initial_liquidity_range
            )
            liquidation_discount = 1 - (1 - loss) * bands_coefficient

            logger.info(f"Params: {kwargs_with_fee}, loss: {loss}, liquidation discount: {liquidation_discount}")

            losses.append(loss)
            discounts.append(liquidation_discount)

        results = [(fee_range, losses), (fee_range, discounts)]

        name = "lossesV2" if is_v2 else "losses"
        save_json_results(pair, f"{name}_fee__{samples}_{n_top_samples}", results)
        save_plot(
            pair,
            f"{name}_fee__{samples}_{n_top_samples}",
            (fee_range, losses),
            (fee_range, discounts),
            {"xlabel": "fee", "ylabel": "Loss"},
            kwargs,
        )
        return results

    @classmethod
    def simulate_A(
        cls,
        pair: str,
        t_exp: int,
        fee: float,
        samples: int = 500000,
        n_top_samples: int = 50,
        dynamic_fee_multiplier: float | None = 0.25,
        min_loan_duration: float | None = None,
        max_loan_duration: float | None = None,
        initial_liquidity_range: int = 4,
        a_range: list | None = None,
        is_v2: bool = False,
    ):
        price_oracle = EmaPriceOracle(t_exp=t_exp)
        if is_v2:
            price_history_loader = VolatilityPriceHistoryLoader(pair=Pair(pair))
        else:
            price_history_loader = GenericPriceHistoryLoader(pair=Pair(pair))

        losses = []
        borrowed_adjusted_losses = []

        kwargs = {
            "samples": samples,
            "n_top_samples": n_top_samples,
            "initial_liquidity_range": initial_liquidity_range,
            "dynamic_fee_multiplier": dynamic_fee_multiplier,
            "min_loan_duration": min_loan_duration,
            "max_loan_duration": max_loan_duration,
        }

        if a_range is None:
            a_range = [int(a) for a in logspace(log10(10), log10(500), 30)]

        for a in a_range:
            kwargs_with_a = {
                **kwargs,
                "A": a,
                "fee": fee,
                "initial_liquidity_class": ConstantInitialLiquidity,
                "price_history_loader": price_history_loader,
                "price_oracle": price_oracle,
                "external_fee": cls.EXTERNAL_FEE,
            }
            if is_v2:
                loss = get_loss_rate_v2(**kwargs_with_a)
            else:
                loss = get_loss_rate(**kwargs_with_a)

            # Simplified formula
            # bands_coefficient = (((A - 1) / A) ** range_size) ** 0.5
            # More precise
            bands_coefficient = (
                sum(((a - 1) / a) ** (k + 0.5) for k in range(initial_liquidity_range)) / initial_liquidity_range
            )
            borrowed_adjusted_loss = 1 - (1 - loss) * bands_coefficient

            logger.info(f"Params: {kwargs_with_a}, loss: {loss}, borrowed loss: {borrowed_adjusted_loss}")

            losses.append(loss)
            borrowed_adjusted_losses.append(borrowed_adjusted_loss)

        results = [(a_range, losses), (a_range, borrowed_adjusted_losses)]

        name = "lossesV2" if is_v2 else "losses"
        save_json_results(pair, f"{name}_A__{samples}_{n_top_samples}", results)
        save_plot(
            pair,
            f"{name}_A__{samples}_{n_top_samples}",
            (a_range, losses),
            (a_range, borrowed_adjusted_losses),
            {"xlabel": "A", "ylabel": "Loss"},
            kwargs,
        )
        return results

    @classmethod
    def simulate_range(
        cls,
        pair: str,
        t_exp: int,
        a: int,
        fee: float,
        samples: int = 500000,
        n_top_samples: int = 50,
        dynamic_fee_multiplier: float | None = 0.25,
        min_loan_duration: float | None = None,
        max_loan_duration: float | None = None,
    ):
        price_oracle = EmaPriceOracle(t_exp=t_exp)
        price_history_loader = GenericPriceHistoryLoader(pair=Pair(pair))

        losses = []
        borrowed_adjusted_losses = []

        kwargs = {
            "samples": samples,
            "n_top_samples": n_top_samples,
            "A": a,
            "fee": fee,
            "dynamic_fee_multiplier": dynamic_fee_multiplier,
            "min_loan_duration": min_loan_duration,
            "max_loan_duration": max_loan_duration,
        }

        liquidity_range = list(range(4, 50, 4))
        for initial_liquidity_range in liquidity_range:
            kwargs_with_a = {
                **kwargs,
                "initial_liquidity_range": initial_liquidity_range,
                "initial_liquidity_class": ConstantInitialLiquidity,
                "price_history_loader": price_history_loader,
                "price_oracle": price_oracle,
                "external_fee": cls.EXTERNAL_FEE,
            }
            loss = get_loss_rate(**kwargs_with_a)

            # Simplified formula
            # bands_coefficient = (((A - 1) / A) ** range_size) ** 0.5
            # More precise
            bands_coefficient = (
                sum(((a - 1) / a) ** (k + 0.5) for k in range(initial_liquidity_range)) / initial_liquidity_range
            )
            borrowed_adjusted_loss = 1 - (1 - loss) * bands_coefficient

            logger.info(f"Params: {kwargs_with_a}, loss: {loss}, borrowed loss: {borrowed_adjusted_loss}")

            losses.append(loss)
            borrowed_adjusted_losses.append(borrowed_adjusted_loss)

        results = [(liquidity_range, losses), (liquidity_range, borrowed_adjusted_losses)]

        save_json_results(pair, f"losses_initial_range__{samples}_{n_top_samples}", results)
        save_plot(
            pair,
            f"losses_initial_range__{samples}_{n_top_samples}",
            (liquidity_range, losses),
            (liquidity_range, borrowed_adjusted_losses),
            {"xlabel": "Initial range N", "ylabel": "Loss"},
            kwargs,
        )
        return results

    @classmethod
    def simulate_dynamic_fee(
        cls,
        pair: str,
        t_exp: int,
        a: int,
        fee: float,
        samples: int = 500000,
        n_top_samples: int = 50,
        min_loan_duration: float | None = None,
        max_loan_duration: float | None = None,
        initial_liquidity_range: int = 4,
    ):
        price_oracle = EmaPriceOracle(t_exp=t_exp)
        price_history_loader = GenericPriceHistoryLoader(pair=Pair(pair))

        losses = []
        borrowed_adjusted_losses = []

        kwargs = {
            "samples": samples,
            "n_top_samples": n_top_samples,
            "A": a,
            "fee": fee,
            "initial_liquidity_range": initial_liquidity_range,
            "min_loan_duration": min_loan_duration,
            "max_loan_duration": max_loan_duration,
        }

        d_fee_range = [d / 100 for d in range(10, 50, 3)]
        for d_fee in d_fee_range:
            kwargs_with_a = {
                **kwargs,
                "dynamic_fee_multiplier": d_fee,
                "initial_liquidity_class": ConstantInitialLiquidity,
                "price_history_loader": price_history_loader,
                "price_oracle": price_oracle,
                "external_fee": cls.EXTERNAL_FEE,
            }
            loss = get_loss_rate(**kwargs_with_a)

            # Simplified formula
            # bands_coefficient = (((A - 1) / A) ** range_size) ** 0.5
            # More precise
            bands_coefficient = (
                sum(((a - 1) / a) ** (k + 0.5) for k in range(initial_liquidity_range)) / initial_liquidity_range
            )
            borrowed_adjusted_loss = 1 - (1 - loss) * bands_coefficient

            logger.info(f"Params: {kwargs_with_a}, loss: {loss}, borrowed loss: {borrowed_adjusted_loss}")

            losses.append(loss)
            borrowed_adjusted_losses.append(borrowed_adjusted_loss)

        results = [(d_fee_range, losses), (d_fee_range, borrowed_adjusted_losses)]

        save_json_results(pair, f"losses_dynamic_fee__{samples}_{n_top_samples}", results)
        save_plot(
            pair,
            f"losses_dynamic_fee__{samples}_{n_top_samples}",
            (d_fee_range, losses),
            (d_fee_range, borrowed_adjusted_losses),
            {"xlabel": "Dynamic fee", "ylabel": "Loss"},
            kwargs,
        )
        return results


def save_plot(
    pair: str,
    file_name: str,
    losses: tuple,
    borrowed_losses: tuple,
    plot_kwargs: dict,
    capture_kwargs: dict,
):
    import matplotlib.pyplot as plt

    plt.figure()
    plt.plot(losses[0], losses[1], label="Loss")
    plt.plot(borrowed_losses[0], borrowed_losses[1], label="Borrowed loss")

    # Min liquidation discount
    min_discount = min(borrowed_losses[1])
    min_discount_index = borrowed_losses[1].index(min_discount)
    min_discount_A = borrowed_losses[0][min_discount_index]
    min_discount_loss = losses[1][min_discount_index]
    plt.axvline(x=min_discount_A, color="black", linestyle="--", linewidth=2)
    plt.text(
        min_discount_A * 1.05,
        max(borrowed_losses[1]) * 0.2,
        f"{plot_kwargs.get('xlabel', 'x')} = {min_discount_A}, Borrowed loss={min_discount:.3f}, loss={min_discount_loss:.3f}",
        rotation=90,
        color="black",
        va="bottom",
    )

    # Caption text for parameters
    plt.text(
        max(borrowed_losses[0]) * 15 / 100,
        max(borrowed_losses[1]),  # (x, y) position on chart
        "\n".join(f"{k}: {capture_kwargs[k]}" for k in capture_kwargs if capture_kwargs[k] is not None),
        color="black",
        bbox=dict(
            facecolor="lightyellow",  # background color
            edgecolor="black",  # border color
            boxstyle="round,pad=0.5",  # rounded corners and padding
        ),
    )

    plt.grid()
    plt.xlabel(plot_kwargs.get("xlabel", "x"))
    plt.ylabel(plot_kwargs.get("ylabel", "Loss"))
    plt.legend(loc="best")

    path = BASE_DIR / "results" / pair / f"{file_name}.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(path, dpi=300, bbox_inches="tight")
    plt.close()


def save_json_results(pair, file_name, results):
    path = BASE_DIR / "results" / pair / f"{file_name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(results, f)
