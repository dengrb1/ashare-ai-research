from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ashare_ai.core.hashing import stable_hash
from ashare_ai.storage.models import TradingRuleRow

RULESET_VERSION = "cn-a-composite-current-2023-08-28-v2"
STAMP_TAX_EFFECTIVE = date(2023, 8, 28)
STAMP_TAX_PUBLISHED = datetime(2023, 8, 27, 9, tzinfo=UTC)
STAMP_TAX_SOURCE = "https://www.gov.cn/zhengce/zhengceku/202308/content_6900595.htm"
TRANSFER_FEE_EFFECTIVE = date(2022, 4, 29)
TRANSFER_FEE_SOURCE = "https://www.chinaclear.cn/"

BOARD_RULES: dict[str, dict[str, Any]] = {
    "MAIN": {
        "ratio": Decimal("0.10"),
        "st_ratio": Decimal("0.05"),
        "price_rule_effective": date(2023, 4, 10),
        "new_sessions": (1, 5),
        "source": "https://www.sse.com.cn/",
        "document_title": "上海证券交易所交易规则（2023年修订）",
    },
    "STAR": {
        "ratio": Decimal("0.20"),
        "st_ratio": Decimal("0.20"),
        "price_rule_effective": date(2019, 7, 22),
        "new_sessions": (1, 5),
        "source": "https://www.sse.com.cn/",
        "document_title": "上海证券交易所科创板股票交易特别规定",
    },
    "CHINEXT": {
        "ratio": Decimal("0.20"),
        "st_ratio": Decimal("0.20"),
        "price_rule_effective": date(2020, 8, 24),
        "new_sessions": (1, 5),
        "source": "https://www.szse.cn/lawrules/rule/stock/trade/",
        "document_title": "深圳证券交易所创业板交易特别规定",
    },
    "BSE": {
        "ratio": Decimal("0.30"),
        "st_ratio": Decimal("0.30"),
        "price_rule_effective": date(2021, 11, 15),
        "new_sessions": (1, 1),
        "source": "https://www.bse.cn/",
        "document_title": "北京证券交易所交易规则（试行）",
    },
}


def listing_special_phase(board: str, listing_session: int) -> str:
    config = BOARD_RULES.get(board)
    if config is None:
        return "NORMAL"
    first, last = config["new_sessions"]
    if first <= listing_session <= last:
        return f"NEW_LISTING_{first}_{last}"
    return "NORMAL"


def ensure_builtin_trading_rules(session: Session) -> None:
    if session.scalar(
        select(TradingRuleRow.rule_id)
        .where(TradingRuleRow.rule_version == RULESET_VERSION)
        .limit(1)
    ):
        return
    rows = []
    for board, config in BOARD_RULES.items():
        rows.append(
            _rule(
                board=board,
                is_st=False,
                ratio=config["ratio"],
                suffix="normal",
                config=config,
            )
        )
        rows.append(
            _rule(
                board=board,
                is_st=True,
                ratio=config["st_ratio"],
                suffix="risk-warning",
                config=config,
                priority=20,
            )
        )
        first, last = config["new_sessions"]
        rows.append(
            _rule(
                board=board,
                is_st=None,
                ratio=None,
                suffix=f"new-listing-{first}-{last}",
                config=config,
                priority=100,
                special_phase=f"NEW_LISTING_{first}_{last}",
                listing_session_from=first,
                listing_session_to=last,
                no_price_limit=True,
            )
        )
    session.add_all(rows)
    session.flush()


def _rule(
    *,
    board: str,
    is_st: bool | None,
    ratio: Decimal | None,
    suffix: str,
    config: dict[str, Any],
    priority: int = 10,
    special_phase: str | None = None,
    listing_session_from: int | None = None,
    listing_session_to: int | None = None,
    no_price_limit: bool = False,
) -> TradingRuleRow:
    effective_from = max(
        STAMP_TAX_EFFECTIVE,
        TRANSFER_FEE_EFFECTIVE,
        config["price_rule_effective"],
    )
    payload = {
        "rule_version": RULESET_VERSION,
        "board": board,
        "is_st": is_st,
        "ratio": str(ratio) if ratio is not None else None,
        "special_phase": special_phase,
        "listing_session_from": listing_session_from,
        "listing_session_to": listing_session_to,
        "price_rule_source": config["source"],
        "price_rule_document": config["document_title"],
        "price_rule_effective": config["price_rule_effective"].isoformat(),
        "stamp_tax_source": STAMP_TAX_SOURCE,
        "stamp_tax_effective": STAMP_TAX_EFFECTIVE.isoformat(),
        "transfer_fee_source": TRANSFER_FEE_SOURCE,
        "transfer_fee_effective": TRANSFER_FEE_EFFECTIVE.isoformat(),
        "commission_assumption": "0.0003 capped configurable assumption",
    }
    digest = stable_hash(payload)
    return TradingRuleRow(
        rule_id=f"builtin-{RULESET_VERSION}-{board.casefold()}-{suffix}",
        rule_type="COMPOSITE",
        rule_version=RULESET_VERSION,
        priority=priority,
        market="A",
        board=board,
        security_type="STOCK",
        risk_status="NORMAL",
        is_st=is_st,
        special_phase=special_phase,
        listing_session_from=listing_session_from,
        listing_session_to=listing_session_to,
        effective_from=effective_from,
        published_at=STAMP_TAX_PUBLISHED,
        enabled=True,
        price_limit_ratio=ratio,
        no_price_limit=no_price_limit,
        lot_size=100,
        t_plus_one=True,
        stamp_tax_rate=Decimal("0.0005"),
        commission_rate=Decimal("0.0003"),
        minimum_commission=Decimal("5"),
        transfer_fee_rate=Decimal("0.00001"),
        source_uri=config["source"],
        source_type="VERSIONED_REGULATORY_RULESET_WITH_CONFIGURED_COMMISSION",
        raw_payload_sha256=digest,
        ingested_at=STAMP_TAX_PUBLISHED,
        details={
            "price_tick": "0.01",
            "minimum_order_quantity": 100,
            "order_increment": 1 if board == "BSE" else 100,
            "price_rule_effective": config["price_rule_effective"].isoformat(),
            "price_rule_source": config["source"],
            "price_rule_document": config["document_title"],
            "stamp_tax_effective": STAMP_TAX_EFFECTIVE.isoformat(),
            "stamp_tax_source": STAMP_TAX_SOURCE,
            "transfer_fee_effective": TRANSFER_FEE_EFFECTIVE.isoformat(),
            "transfer_fee_source": TRANSFER_FEE_SOURCE,
            "commission_basis": "CONFIGURED_BACKTEST_ASSUMPTION_NOT_OFFICIAL_RATE",
        },
    )
