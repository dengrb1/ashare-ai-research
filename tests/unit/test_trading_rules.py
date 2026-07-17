from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ashare_ai.core.contracts import Side, TradeStatus
from ashare_ai.storage.models import TradingRuleRow
from ashare_ai.trading.execution import (
    AccountState,
    DailyExecutionModel,
    ExecutionBar,
    ExecutionConfig,
    Order,
    OrderStatus,
    PositionLot,
    RejectReason,
    SlippageTier,
)
from ashare_ai.trading.rules import (
    PriceLimitPolicy,
    RuleConflictError,
    RuleContext,
    RuleNotFoundError,
    TradingRule,
    TradingRuleBook,
    TradingRuleRepository,
)

SHANGHAI = ZoneInfo("Asia/Shanghai")
SNAPSHOT_HASH = "f" * 64


def make_rule(**overrides: object) -> TradingRule:
    values: dict[str, object] = {
        "rule_id": "main-normal",
        "rule_version": "v1",
        "priority": 10,
        "market": "A",
        "board": "MAIN",
        "is_st": False,
        "effective_from": date(2024, 1, 1),
        "price_limit_ratio": Decimal("0.10"),
        "no_price_limit": False,
        "lot_size": 100,
        "t_plus_one": True,
        "stamp_tax_rate": Decimal("0.0005"),
        "commission_rate": Decimal("0.00025"),
        "minimum_commission": Decimal("5"),
        "transfer_fee_rate": Decimal("0.00001"),
        "details": {"price_tick": "0.01"},
    }
    values.update(overrides)
    return TradingRule(**values)


def make_config(*, participation_rate: str = "1") -> ExecutionConfig:
    return ExecutionConfig(
        participation_rate=Decimal(participation_rate),
        price_limit_policy=PriceLimitPolicy(
            require_official_limits=True,
            default_price_tick=Decimal("0.01"),
        ),
        slippage_tiers=(
            SlippageTier(
                minimum_adv_amount=Decimal("0"),
                half_spread_bps=Decimal("0"),
            ),
        ),
        impact_coefficient=Decimal("0"),
        max_slippage_bps=Decimal("100"),
        fee_quantum=Decimal("0.01"),
        odd_lot_sell_all_only=True,
    )


def make_bar(
    *,
    trading_date: date = date(2025, 1, 2),
    price: str = "10",
    volume: int = 10_000,
    status: TradeStatus = TradeStatus.TRADING,
    upper: str = "11",
    lower: str = "9",
) -> ExecutionBar:
    value = Decimal(price)
    return ExecutionBar(
        symbol="600000.SH",
        trading_date=trading_date,
        available_at=datetime(
            trading_date.year,
            trading_date.month,
            trading_date.day,
            17,
            tzinfo=SHANGHAI,
        ),
        snapshot_hash=SNAPSHOT_HASH,
        open=value,
        high=value,
        low=value,
        close=value,
        volume=volume,
        amount=value * volume,
        prev_close=Decimal("10"),
        trade_status=status,
        official_limit_up=Decimal(upper),
        official_limit_down=Decimal(lower),
    )


def test_rule_matching_is_effective_dated_and_st_specific() -> None:
    normal = make_rule()
    st = make_rule(
        rule_id="main-st",
        is_st=True,
        price_limit_ratio=Decimal("0.05"),
    )
    context = RuleContext(
        symbol="600000.SH",
        trading_date=date(2025, 1, 2),
        decision_at=datetime(2025, 1, 2, 18, tzinfo=SHANGHAI),
        exchange="SH",
        market="A",
        board="MAIN",
        security_type="STOCK",
        risk_status="ST",
        is_st=True,
        listing_days=500,
        listing_session=500,
    )
    assert TradingRuleBook([normal, st]).resolve(context).rule_id == "main-st"
    with pytest.raises(RuleNotFoundError):
        TradingRuleBook([normal]).resolve(context)


def test_rule_matching_rejects_ambiguous_top_rank() -> None:
    context = RuleContext(
        symbol="600000.SH",
        trading_date=date(2025, 1, 2),
        decision_at=datetime(2025, 1, 2, 18, tzinfo=SHANGHAI),
        exchange="SH",
        market="A",
        board="MAIN",
        security_type="STOCK",
        risk_status="NORMAL",
        is_st=False,
        listing_days=500,
        listing_session=500,
    )
    with pytest.raises(RuleConflictError):
        TradingRuleBook([make_rule(), make_rule(rule_id="duplicate")]).resolve(context)


def test_storage_row_explicit_selector_fields_override_legacy_details() -> None:
    row = SimpleNamespace(
        rule_id="explicit",
        rule_type="COMPOSITE",
        rule_version="v2",
        priority=1,
        exchange="SH",
        market="A",
        board="STAR",
        security_type="STOCK",
        risk_status="NORMAL",
        is_st=False,
        symbol="688001.SH",
        special_phase="NORMAL",
        listing_session_from=5,
        listing_session_to=None,
        min_listing_days=5,
        max_listing_days=None,
        effective_from=date(2025, 1, 1),
        effective_to=None,
        published_at=datetime(2024, 12, 1, 12, tzinfo=SHANGHAI),
        ingested_at=datetime(2024, 12, 1, 13, tzinfo=SHANGHAI),
        enabled=True,
        price_limit_ratio=Decimal("0.20"),
        no_price_limit=False,
        lot_size=200,
        t_plus_one=True,
        stamp_tax_rate=Decimal("0.0005"),
        commission_rate=Decimal("0.00025"),
        minimum_commission=Decimal("5"),
        transfer_fee_rate=Decimal("0.00001"),
        raw_payload_sha256=SNAPSHOT_HASH,
        details={"symbol": "600000.SH", "special_phase": "IPO_UNLIMITED"},
    )
    rule = TradingRule.from_storage_row(row)
    assert rule.symbol == "688001.SH"
    assert rule.special_phase == "NORMAL"
    assert rule.exchange == "SH"
    assert rule.listing_session_from == 5


def test_rule_repository_loads_only_rows_visible_at_decision_time() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    TradingRuleRow.__table__.create(engine)
    decision_at = datetime(2025, 1, 2, 18, tzinfo=SHANGHAI)
    with Session(engine) as session:
        session.add_all(
            [
                TradingRuleRow(
                    rule_id="visible",
                    rule_version="v1",
                    selector_key="visible-selector",
                    priority=1,
                    exchange="SH",
                    market="A",
                    board="MAIN",
                    security_type="STOCK",
                    risk_status="NORMAL",
                    is_st=False,
                    symbol="600000.SH",
                    special_phase="NORMAL",
                    listing_session_from=1,
                    effective_from=date(2024, 1, 1),
                    published_at=None,
                    ingested_at=None,
                    price_limit_ratio=Decimal("0.10"),
                    lot_size=100,
                    raw_payload_sha256=SNAPSHOT_HASH,
                ),
                TradingRuleRow(
                    rule_id="future",
                    rule_version="v1",
                    selector_key="future-selector",
                    priority=2,
                    exchange="SH",
                    market="A",
                    board="MAIN",
                    security_type="STOCK",
                    risk_status="NORMAL",
                    is_st=False,
                    symbol="600000.SH",
                    special_phase="NORMAL",
                    listing_session_from=1,
                    effective_from=date(2024, 1, 1),
                    published_at=decision_at + timedelta(seconds=1),
                    ingested_at=decision_at + timedelta(seconds=1),
                    price_limit_ratio=Decimal("0.10"),
                    lot_size=100,
                    raw_payload_sha256="e" * 64,
                ),
            ]
        )
        session.commit()
        context = RuleContext(
            symbol="600000.SH",
            trading_date=date(2025, 1, 3),
            decision_at=decision_at,
            exchange="SH",
            market="A",
            board="MAIN",
            security_type="STOCK",
            risk_status="NORMAL",
            is_st=False,
            listing_days=500,
            listing_session=500,
        )
        rule = TradingRuleRepository().resolve(session, context)
    assert rule.rule_id == "visible"


def test_execution_rejects_suspension_limit_lock_and_invalid_lot() -> None:
    model = DailyExecutionModel(make_config())
    rule = make_rule()
    account = AccountState(cash=Decimal("100000"))
    order = Order(order_id="buy", symbol="600000.SH", side=Side.BUY, quantity=100)

    suspended = model.execute_orders(
        orders=[order],
        bars={"600000.SH": make_bar(status=TradeStatus.SUSPENDED)},
        rules={"600000.SH": rule},
        account=account,
        next_trading_date=date(2025, 1, 3),
        adv_amounts={"600000.SH": Decimal("100000000")},
        volatilities={"600000.SH": 0.02},
    )[0]
    assert suspended.reject_reason == RejectReason.SUSPENDED

    locked = model.execute_orders(
        orders=[order],
        bars={"600000.SH": make_bar(price="11")},
        rules={"600000.SH": rule},
        account=account,
        next_trading_date=date(2025, 1, 3),
        adv_amounts={"600000.SH": Decimal("100000000")},
        volatilities={"600000.SH": 0.02},
    )[0]
    assert locked.reject_reason == RejectReason.LIMIT_UP_LOCKED

    invalid = model.execute_orders(
        orders=[Order(order_id="odd", symbol="600000.SH", side=Side.BUY, quantity=99)],
        bars={"600000.SH": make_bar()},
        rules={"600000.SH": rule},
        account=account,
        next_trading_date=date(2025, 1, 3),
        adv_amounts={"600000.SH": Decimal("100000000")},
        volatilities={"600000.SH": 0.02},
    )[0]
    assert invalid.reject_reason == RejectReason.INVALID_ORDER_UNIT


def test_t_plus_one_lots_prevent_same_day_resale() -> None:
    model = DailyExecutionModel(make_config())
    rule = make_rule(minimum_commission=Decimal("0"))
    account = AccountState(cash=Decimal("100000"))
    day = date(2025, 1, 2)
    buy = model.execute_orders(
        orders=[Order(order_id="buy", symbol="600000.SH", side=Side.BUY, quantity=100)],
        bars={"600000.SH": make_bar(trading_date=day)},
        rules={"600000.SH": rule},
        account=account,
        next_trading_date=date(2025, 1, 3),
        adv_amounts={"600000.SH": Decimal("100000000")},
        volatilities={"600000.SH": 0.0},
    )[0]
    assert buy.status == OrderStatus.FILLED
    sell = model.execute_orders(
        orders=[Order(order_id="sell", symbol="600000.SH", side=Side.SELL, quantity=100)],
        bars={"600000.SH": make_bar(trading_date=day)},
        rules={"600000.SH": rule},
        account=account,
        next_trading_date=date(2025, 1, 3),
        adv_amounts={"600000.SH": Decimal("100000000")},
        volatilities={"600000.SH": 0.0},
    )[0]
    assert sell.reject_reason == RejectReason.T1_NOT_SELLABLE


def test_participation_partial_fill_and_sell_fees() -> None:
    model = DailyExecutionModel(make_config(participation_rate="0.05"))
    rule = make_rule()
    account = AccountState(
        cash=Decimal("0"),
        lots=[
            PositionLot(
                symbol="600000.SH",
                quantity=100,
                acquired_date=date(2025, 1, 1),
                sellable_date=date(2025, 1, 2),
                unit_cost=Decimal("9"),
            )
        ],
    )
    result = model.execute_orders(
        orders=[Order(order_id="sell", symbol="600000.SH", side=Side.SELL, quantity=100)],
        bars={"600000.SH": make_bar(volume=1000)},
        rules={"600000.SH": rule},
        account=account,
        next_trading_date=date(2025, 1, 3),
        adv_amounts={"600000.SH": Decimal("100000000")},
        volatilities={"600000.SH": 0.0},
    )[0]
    assert result.status == OrderStatus.PARTIALLY_FILLED
    assert result.filled_quantity == 50
    assert result.commission == Decimal("5.00")
    assert result.stamp_tax == Decimal("0.25")
    assert result.transfer_fee == Decimal("0.01")


def test_odd_lot_must_be_full_exit() -> None:
    model = DailyExecutionModel(make_config())
    account = AccountState(
        cash=Decimal("0"),
        lots=[
            PositionLot(
                symbol="600000.SH",
                quantity=150,
                acquired_date=date(2025, 1, 1),
                sellable_date=date(2025, 1, 2),
                unit_cost=Decimal("9"),
            )
        ],
    )
    result = model.execute_orders(
        orders=[Order(order_id="sell", symbol="600000.SH", side=Side.SELL, quantity=50)],
        bars={"600000.SH": make_bar()},
        rules={"600000.SH": make_rule()},
        account=account,
        next_trading_date=date(2025, 1, 3),
        adv_amounts={"600000.SH": Decimal("100000000")},
        volatilities={"600000.SH": 0.0},
    )[0]
    assert result.reject_reason == RejectReason.ODD_LOT_NOT_FULL_EXIT
