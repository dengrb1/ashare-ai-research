from __future__ import annotations

import decimal
from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from math import floor, sqrt
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from ashare_ai.core.contracts import Side, TradeStatus
from ashare_ai.trading.rules import (
    PriceLimitPolicy,
    RuleNotFoundError,
    TradingRule,
    TradingRuleError,
    resolve_price_band,
)


class RejectReason(StrEnum):
    SUSPENDED = "SUSPENDED"
    MISSING_TRADE_CONTEXT = "MISSING_TRADE_CONTEXT"
    MISSING_OFFICIAL_LIMIT = "MISSING_OFFICIAL_LIMIT"
    OUTSIDE_PRICE_BAND = "OUTSIDE_PRICE_BAND"
    LIMIT_PRICE_NOT_MARKETABLE = "LIMIT_PRICE_NOT_MARKETABLE"
    LIMIT_UP_LOCKED = "LIMIT_UP_LOCKED"
    LIMIT_DOWN_LOCKED = "LIMIT_DOWN_LOCKED"
    T1_NOT_SELLABLE = "T1_NOT_SELLABLE"
    INVALID_ORDER_UNIT = "INVALID_ORDER_UNIT"
    ODD_LOT_NOT_FULL_EXIT = "ODD_LOT_NOT_FULL_EXIT"
    INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    PARTICIPATION_LIMIT = "PARTICIPATION_LIMIT"
    DATA_QUALITY_BLOCK = "DATA_QUALITY_BLOCK"
    ORDER_EXPIRED = "ORDER_EXPIRED"


class OrderStatus(StrEnum):
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class Order(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: str
    symbol: str
    side: Side
    quantity: int = Field(gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)


class ExecutionBar(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    trading_date: date
    available_at: AwareDatetime
    snapshot_hash: str = Field(min_length=64, max_length=64)
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: int = Field(ge=0)
    amount: Decimal = Field(ge=0)
    prev_close: Decimal | None = Field(default=None, gt=0)
    trade_status: TradeStatus = TradeStatus.TRADING
    official_limit_up: Decimal | None = Field(default=None, gt=0)
    official_limit_down: Decimal | None = Field(default=None, gt=0)
    price_basis: Literal["RAW", "HFQ"] = "RAW"

    @model_validator(mode="after")
    def validate_ohlc(self) -> ExecutionBar:
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high is below an OHLC value")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low is above an OHLC value")
        return self


class SlippageTier(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum_adv_amount: Decimal = Field(ge=0)
    half_spread_bps: Decimal = Field(ge=0)


class ExecutionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    participation_rate: Decimal = Field(gt=0, le=1)
    price_limit_policy: PriceLimitPolicy
    slippage_tiers: tuple[SlippageTier, ...] = Field(min_length=1)
    impact_coefficient: Decimal = Field(ge=0)
    max_slippage_bps: Decimal = Field(ge=0)
    fee_quantum: Decimal = Field(gt=0)
    odd_lot_sell_all_only: bool


class PositionLot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    quantity: int = Field(ge=0)
    acquired_date: date
    sellable_date: date
    unit_cost: Decimal = Field(ge=0)


class AccountState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cash: Decimal = Field(ge=0)
    lots: list[PositionLot] = Field(default_factory=list)

    def total_quantity(self, symbol: str) -> int:
        return sum(lot.quantity for lot in self.lots if lot.symbol == symbol)

    def sellable_quantity(self, symbol: str, trading_date: date) -> int:
        return sum(
            lot.quantity
            for lot in self.lots
            if lot.symbol == symbol and lot.sellable_date <= trading_date
        )

    def consume_sellable(self, symbol: str, trading_date: date, quantity: int) -> None:
        remaining = quantity
        for lot in sorted(
            self.lots,
            key=lambda item: (item.sellable_date, item.acquired_date, item.unit_cost),
        ):
            if lot.symbol != symbol or lot.sellable_date > trading_date or lot.quantity == 0:
                continue
            consumed = min(lot.quantity, remaining)
            lot.quantity -= consumed
            remaining -= consumed
            if remaining == 0:
                break
        if remaining:
            raise ValueError("attempted to consume more than sellable quantity")
        self.lots = [lot for lot in self.lots if lot.quantity > 0]


class ExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    order_id: str
    symbol: str
    side: Side
    status: OrderStatus
    requested_quantity: int
    filled_quantity: int = Field(ge=0)
    fill_price: Decimal | None = None
    notional: Decimal = Field(ge=0)
    commission: Decimal = Field(ge=0)
    stamp_tax: Decimal = Field(ge=0)
    transfer_fee: Decimal = Field(ge=0)
    total_fee: Decimal = Field(ge=0)
    slippage_bps: Decimal = Field(ge=0)
    slippage_cost: Decimal = Field(ge=0)
    market_volume: int = Field(ge=0)
    participation_rate: Decimal = Field(ge=0, le=1)
    reject_reason: RejectReason | None = None


class DailyExecutionModel:
    def __init__(self, config: ExecutionConfig) -> None:
        self.config = config

    def execute_orders(
        self,
        *,
        orders: list[Order] | tuple[Order, ...],
        bars: dict[str, ExecutionBar],
        rules: dict[str, TradingRule],
        account: AccountState,
        next_trading_date: date,
        adv_amounts: dict[str, Decimal],
        volatilities: dict[str, float],
    ) -> tuple[ExecutionResult, ...]:
        consumed_volume: dict[str, int] = defaultdict(int)
        results: list[ExecutionResult] = []
        ordered = (
            orders
            if len(orders) < 2
            else sorted(
                orders,
                key=lambda order: (
                    0 if order.side == Side.SELL else 1,
                    order.symbol,
                    order.order_id,
                ),
            )
        )
        for order in ordered:
            bar = bars.get(order.symbol)
            rule = rules.get(order.symbol)
            if bar is None or rule is None:
                results.append(self._reject(order, RejectReason.MISSING_TRADE_CONTEXT))
                continue
            result = self._execute_one(
                order=order,
                bar=bar,
                rule=rule,
                account=account,
                next_trading_date=next_trading_date,
                adv_amount=adv_amounts.get(order.symbol, Decimal("0")),
                volatility=volatilities.get(order.symbol, 0.0),
                already_consumed_volume=consumed_volume[order.symbol],
            )
            consumed_volume[order.symbol] += result.filled_quantity
            results.append(result)
        return tuple(results)

    def execute_order(
        self,
        *,
        order: Order,
        bar: ExecutionBar,
        rule: TradingRule,
        account: AccountState,
        next_trading_date: date,
        adv_amount: Decimal,
        volatility: float,
        reference_open: Decimal | None = None,
    ) -> ExecutionResult:
        """Execute one optimizer order without allocating sort/context containers."""
        return self._execute_one(
            order=order,
            bar=bar,
            rule=rule,
            account=account,
            next_trading_date=next_trading_date,
            adv_amount=adv_amount,
            volatility=volatility,
            already_consumed_volume=0,
            reference_open=reference_open,
        )

    def _execute_one(
        self,
        *,
        order: Order,
        bar: ExecutionBar,
        rule: TradingRule,
        account: AccountState,
        next_trading_date: date,
        adv_amount: Decimal,
        volatility: float,
        already_consumed_volume: int,
        reference_open: Decimal | None = None,
    ) -> ExecutionResult:
        if bar.trade_status != TradeStatus.TRADING or bar.volume <= 0:
            return self._reject(order, RejectReason.SUSPENDED)

        try:
            band = resolve_price_band(
                rule,
                prev_close=bar.prev_close,
                official_limit_up=bar.official_limit_up,
                official_limit_down=bar.official_limit_down,
                policy=self.config.price_limit_policy,
            )
        except RuleNotFoundError:
            return self._reject(order, RejectReason.MISSING_OFFICIAL_LIMIT)
        except (TradingRuleError, decimal.InvalidOperation):
            # Conflicting or malformed rule data fails this one order rather
            # than aborting the entire backtest.  The position simply does not
            # trade that day.
            return self._reject(order, RejectReason.DATA_QUALITY_BLOCK)

        if order.limit_price is not None:
            if band.upper is not None and order.limit_price > band.upper:
                return self._reject(order, RejectReason.OUTSIDE_PRICE_BAND)
            if band.lower is not None and order.limit_price < band.lower:
                return self._reject(order, RejectReason.OUTSIDE_PRICE_BAND)

        if order.side == Side.BUY and band.upper is not None and self._one_price(bar, band.upper):
            return self._reject(order, RejectReason.LIMIT_UP_LOCKED)
        if order.side == Side.SELL and band.lower is not None and self._one_price(bar, band.lower):
            return self._reject(order, RejectReason.LIMIT_DOWN_LOCKED)

        unit_reject = self._validate_order_unit(order, rule, account, bar.trading_date)
        if unit_reject is not None:
            return self._reject(order, unit_reject)

        max_market_quantity = floor(Decimal(bar.volume) * self.config.participation_rate)
        available_capacity = max(0, max_market_quantity - already_consumed_volume)
        if available_capacity <= 0:
            return self._reject(order, RejectReason.PARTICIPATION_LIMIT)
        fill_quantity = min(order.quantity, available_capacity)

        execution_open = reference_open if reference_open is not None else bar.open
        slippage_bps = self._slippage_bps(
            order=order,
            reference_price=execution_open,
            adv_amount=adv_amount,
            volatility=volatility,
        )
        direction = Decimal("1") if order.side == Side.BUY else Decimal("-1")
        fill_price = execution_open * (
            Decimal("1") + direction * slippage_bps / Decimal("10000")
        )
        if band.upper is not None:
            fill_price = min(fill_price, band.upper)
        if band.lower is not None:
            fill_price = max(fill_price, band.lower)
        if order.limit_price is not None:
            if order.side == Side.BUY and fill_price > order.limit_price:
                return self._reject(order, RejectReason.LIMIT_PRICE_NOT_MARKETABLE)
            if order.side == Side.SELL and fill_price < order.limit_price:
                return self._reject(order, RejectReason.LIMIT_PRICE_NOT_MARKETABLE)

        try:
            tick = Decimal(
                str(
                    rule.details.get(
                        "price_tick", self.config.price_limit_policy.default_price_tick
                    )
                )
            )
        except (ValueError, decimal.InvalidOperation):
            return self._reject(order, RejectReason.DATA_QUALITY_BLOCK)
        fill_price = self._round_money(fill_price, tick)
        notional = fill_price * fill_quantity
        commission = max(rule.minimum_commission, notional * rule.commission_rate)
        stamp_tax = notional * rule.stamp_tax_rate if order.side == Side.SELL else Decimal("0")
        transfer_fee = notional * rule.transfer_fee_rate
        commission = self._round_money(commission, self.config.fee_quantum)
        stamp_tax = self._round_money(stamp_tax, self.config.fee_quantum)
        transfer_fee = self._round_money(transfer_fee, self.config.fee_quantum)
        total_fee = commission + stamp_tax + transfer_fee

        if order.side == Side.BUY:
            total_cost = notional + total_fee
            if total_cost > account.cash:
                return self._reject(order, RejectReason.INSUFFICIENT_CASH)
            account.cash -= total_cost
            sellable_date = next_trading_date if rule.t_plus_one else bar.trading_date
            account.lots.append(
                PositionLot(
                    symbol=order.symbol,
                    quantity=fill_quantity,
                    acquired_date=bar.trading_date,
                    sellable_date=sellable_date,
                    unit_cost=total_cost / fill_quantity,
                )
            )
        else:
            account.consume_sellable(order.symbol, bar.trading_date, fill_quantity)
            account.cash += notional - total_fee

        status = (
            OrderStatus.FILLED if fill_quantity == order.quantity else OrderStatus.PARTIALLY_FILLED
        )
        return ExecutionResult(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            status=status,
            requested_quantity=order.quantity,
            filled_quantity=fill_quantity,
            fill_price=fill_price,
            notional=notional,
            commission=commission,
            stamp_tax=stamp_tax,
            transfer_fee=transfer_fee,
            total_fee=total_fee,
            slippage_bps=slippage_bps,
            slippage_cost=abs(fill_price - execution_open) * fill_quantity,
            market_volume=bar.volume,
            participation_rate=Decimal(fill_quantity) / Decimal(bar.volume),
        )

    def _validate_order_unit(
        self,
        order: Order,
        rule: TradingRule,
        account: AccountState,
        trading_date: date,
    ) -> RejectReason | None:
        try:
            buy_min = int(rule.details.get("buy_min_qty", rule.lot_size))
            buy_step = int(rule.details.get("buy_qty_step", rule.lot_size))
            sell_step = int(rule.details.get("sell_qty_step", rule.lot_size))
        except (TypeError, ValueError):
            return RejectReason.DATA_QUALITY_BLOCK
        if min(buy_min, buy_step, sell_step) <= 0:
            return RejectReason.DATA_QUALITY_BLOCK

        if order.side == Side.BUY:
            if order.quantity < buy_min or order.quantity % buy_step != 0:
                return RejectReason.INVALID_ORDER_UNIT
            return None

        total_quantity = account.total_quantity(order.symbol)
        sellable = account.sellable_quantity(order.symbol, trading_date)
        if order.quantity > total_quantity:
            return RejectReason.INSUFFICIENT_POSITION
        if order.quantity > sellable:
            return RejectReason.T1_NOT_SELLABLE
        if order.quantity % sell_step == 0:
            return None
        if self.config.odd_lot_sell_all_only and order.quantity != total_quantity:
            return RejectReason.ODD_LOT_NOT_FULL_EXIT
        return None

    def _slippage_bps(
        self,
        *,
        order: Order,
        reference_price: Decimal,
        adv_amount: Decimal,
        volatility: float,
    ) -> Decimal:
        tiers = sorted(
            self.config.slippage_tiers,
            key=lambda tier: tier.minimum_adv_amount,
            reverse=True,
        )
        spread = next(
            (tier.half_spread_bps for tier in tiers if adv_amount >= tier.minimum_adv_amount),
            tiers[-1].half_spread_bps,
        )
        impact = Decimal("0")
        if adv_amount > 0 and volatility > 0:
            participation = float(reference_price * order.quantity / adv_amount)
            impact = (
                self.config.impact_coefficient
                * Decimal(str(volatility))
                * Decimal(str(sqrt(max(participation, 0.0))))
                * Decimal("10000")
            )
        return min(self.config.max_slippage_bps, spread + impact)

    @staticmethod
    def _one_price(bar: ExecutionBar, price: Decimal) -> bool:
        return bar.open == bar.high == bar.low == bar.close == price

    @staticmethod
    def _round_money(value: Decimal, quantum: Decimal) -> Decimal:
        units = (value / quantum).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return units * quantum

    @staticmethod
    def _reject(order: Order, reason: RejectReason) -> ExecutionResult:
        return ExecutionResult(
            order_id=order.order_id,
            symbol=order.symbol,
            side=order.side,
            status=OrderStatus.REJECTED,
            requested_quantity=order.quantity,
            filled_quantity=0,
            notional=Decimal("0"),
            commission=Decimal("0"),
            stamp_tax=Decimal("0"),
            transfer_fee=Decimal("0"),
            total_fee=Decimal("0"),
            slippage_bps=Decimal("0"),
            slippage_cost=Decimal("0"),
            market_volume=0,
            participation_rate=Decimal("0"),
            reject_reason=reason,
        )
