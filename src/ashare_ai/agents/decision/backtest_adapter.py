from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from ashare_ai.backtest.engine import BacktestSignal

if TYPE_CHECKING:
    from datetime import date

    from pydantic import AwareDatetime

    from ashare_ai.agents.decision.models import UnifiedDecision

logger = logging.getLogger(__name__)


def decision_to_signal(
    decision: UnifiedDecision,
    snapshot_hash: str,
    industry_code: str,
) -> BacktestSignal:
    """
    将 UnifiedDecision 转换为 BacktestSignal。

    映射规则：
    - position (0/10/20/30/50/70/100) 直接转换为 target_weight (0.0/0.1/.../1.0)
    - action 和 risk 概率用于记录，不影响 target_weight
    - decision_at 映射为 BacktestSignal.decision_at
    - 风险引擎仍是最终约束，决策只提供建议权重

    Args:
        decision: 统一决策输出
        snapshot_hash: 快照哈希
        industry_code: 行业代码

    Returns:
        BacktestSignal 实例
    """
    # position 枚举转换为百分比权重
    target_weight = Decimal(decision.position) / Decimal(100)

    return BacktestSignal(
        signal_date=decision.trading_date,
        decision_at=decision.decision_at,
        snapshot_hash=snapshot_hash,
        symbol=decision.symbol,
        industry_code=industry_code,
        target_weight=target_weight,
    )


def enrich_signal_with_decision_metadata(
    signal: BacktestSignal,
    decision: UnifiedDecision,
) -> dict:
    """
    为回测记录补充决策元数据。

    返回扩展的字典，包含：
    - 原始 BacktestSignal 字段
    - 决策模式、模型版本
    - 动作、风险、仓位
    - 全部概率分布

    Args:
        signal: 回测信号
        decision: 统一决策

    Returns:
        包含完整决策上下文的字典
    """
    signal_dict = signal.model_dump()
    signal_dict.update(
        {
            "decision_mode": decision.mode,
            "model_version": decision.model_version,
            "action": decision.action,
            "action_probabilities": {
                "BUY": decision.probabilities.action.BUY,
                "HOLD": decision.probabilities.action.HOLD,
                "SELL": decision.probabilities.action.SELL,
            },
            "risk": decision.risk,
            "risk_probabilities": {
                "LOW": decision.probabilities.risk.LOW,
                "MEDIUM": decision.probabilities.risk.MEDIUM,
                "HIGH": decision.probabilities.risk.HIGH,
            },
            "position": decision.position,
            "direction_1d": {
                "UP": decision.probabilities.direction_1d.UP,
                "FLAT": decision.probabilities.direction_1d.FLAT,
                "DOWN": decision.probabilities.direction_1d.DOWN,
            },
            "direction_5d": {
                "UP": decision.probabilities.direction_5d.UP,
                "FLAT": decision.probabilities.direction_5d.FLAT,
                "DOWN": decision.probabilities.direction_5d.DOWN,
            },
            "up_over_3pct_5d": {
                "YES": decision.probabilities.up_over_3pct_5d.YES,
                "NO": decision.probabilities.up_over_3pct_5d.NO,
            },
            "input_manifest_hash": decision.input_manifest_hash,
        }
    )
    return signal_dict
