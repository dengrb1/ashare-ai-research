"""Stable error codes mapped to fixed user-facing Simplified-Chinese text.

The persisted ``JobRun.error_message``/``BacktestRun`` failure fields and the
HTTP ``detail`` values that the research, report, Trade Plan and backtest
workflows return to Web are *display* text, not diagnostics.  This module maps
stable codes to fixed Simplified-Chinese copy so the public surface never
echoes exception text, URLs, paths, credentials or vendor responses.

Audit events keep carrying structured ``error_type`` and safe internal details
so operations can still diagnose a failure; see ``core/security.py`` for the
sanitizer used by CLI and internal logging.
"""

from __future__ import annotations

_PUBLIC_ERROR_MESSAGES: dict[str, str] = {
    "RESEARCH_FAILED": "本次研究未完成，请稍后重试。",
    "DATA_READINESS_TIMEOUT": "必要的基准数据未在安全期限内完成同步，本次研究未完成。",
    "TRADING_CALENDAR_UNAVAILABLE": "权威交易日历暂时不可用，无法安全确定研究日期。",
    "QUEUE_UNAVAILABLE": "任务队列暂时不可用，请稍后重试。",
    "TRADE_PLAN_FAILED": "模拟交易方案生成失败，请稍后重试。",
    "BACKTEST_FAILED": "本次回测未完成，请稍后重试。",
    "RESEARCH_DEDUPLICATED": "重复的研究请求已合并，无需再次提交。",
    "RESEARCH_SUBMISSION_CONFLICTED": "研究提交冲突，请稍后重试。",
    "TRADE_PLAN_SUBMISSION_CONFLICTED": "模拟交易方案提交冲突，请稍后重试。",
    "BACKTEST_SUBMISSION_CONFLICTED": "回测提交冲突，请稍后重试。",
    "TASK_FAILED": "任务失败，请稍后重试。",
}

_DEFAULT_TEXT = "任务失败，请稍后重试。"


def public_error_message(code: str, *, fallback_code: str = "TASK_FAILED") -> str:
    """Return the fixed Simplified-Chinese message for a stable error code.

    An unknown ``code`` falls back to ``fallback_code``; an unknown fallback
    still resolves to fixed Chinese copy.  The original code, exception text and
    any provider details are never echoed to the caller.
    """
    text = _PUBLIC_ERROR_MESSAGES.get(code)
    if text is None:
        text = _PUBLIC_ERROR_MESSAGES.get(fallback_code)
    return text if text is not None else _DEFAULT_TEXT
