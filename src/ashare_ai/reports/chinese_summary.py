from __future__ import annotations

from collections.abc import Sequence

_COMPONENT_LABELS = {
    "fundamental": "基本面",
    "technical": "技术面",
    "sentiment": "市场情绪",
}

_REASON_LABELS = {
    "CRITICAL_EVENT_RISK": "重大事件风险，暂不建议买入",
    "GLOBAL_RISK_FUSE_ACTIVE": "全局风控熔断，当前只做观察",
    "INCOMPLETE_LATEST_FINANCIAL_PERIOD": "最新财报期数据不完整",
    "MISSING_OFFICIAL_DISCLOSURE": "缺少官方披露信息",
    "FUNDAMENTAL_DATA_INCOMPLETE": "基本面数据不完整",
    "DISCLOSURE_DATA_INCOMPLETE": "公告或市场情绪数据不完整",
}


def reason_label(code: str) -> str:
    return _REASON_LABELS.get(code, f"未通过条件：{code}")


def component_summary(component: str, score: float, confidence: float | None = None) -> str:
    label = _COMPONENT_LABELS.get(component, component)
    assessment = "相对较好" if score >= 70 else "一般" if score >= 50 else "偏弱"
    summary = f"{label}得分 {score:.2f} 分，整体表现{assessment}。"
    if confidence is not None:
        summary += f"本项依据的资料完整度约为 {confidence * 100:.0f}%。"
    return summary


def symbol_summary(
    *,
    total_score: float,
    fundamental_score: float,
    technical_score: float,
    sentiment_score: float,
    advice_eligible: bool,
    reasons: Sequence[str],
) -> str:
    del total_score
    dimensions = {
        "基本面": fundamental_score,
        "技术面": technical_score,
        "市场情绪": sentiment_score,
    }
    strongest = max(dimensions, key=dimensions.__getitem__)
    weakest = min(dimensions, key=dimensions.__getitem__)
    dimension_note = f"从本次研究维度看，{strongest}相对更有支撑，{weakest}相对需要继续观察。"
    if advice_eligible:
        return (
            f"{dimension_note} 数据门禁已通过，当前没有使模拟买入建议失效的风险结论，"
            "可以查看基于冻结数据和规则校验的模拟方案；仅用于模拟研究，不构成收益承诺。"
        )
    details = "；".join(reason_label(reason) for reason in reasons) or "未通过个股建议门禁"
    return (
        f"{dimension_note} 当前数据门禁或风险结论不支持生成买入建议，主要原因是：{details}。"
        "仅用于模拟研究，不构成收益承诺。"
    )
