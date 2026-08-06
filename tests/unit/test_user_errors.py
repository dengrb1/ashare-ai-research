from __future__ import annotations

import pytest

from ashare_ai.core.user_errors import public_error_message

_EXPECTED = {
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


@pytest.mark.parametrize(("code", "expected"), sorted(_EXPECTED.items()))
def test_public_error_message_returns_fixed_chinese_copy(code: str, expected: str) -> None:
    assert public_error_message(code) == expected


def test_unknown_code_uses_the_default_chinese_fallback() -> None:
    assert public_error_message("UNKNOWN_CODE") == "任务失败，请稍后重试。"


def test_unknown_code_falls_back_to_the_requested_fallback_code() -> None:
    assert public_error_message("UNKNOWN_CODE", fallback_code="BACKTEST_FAILED") == (
        "本次回测未完成，请稍后重试。"
    )


def test_unknown_fallback_code_still_resolves_to_fixed_chinese_copy() -> None:
    assert public_error_message("UNKNOWN_CODE", fallback_code="ALSO_UNKNOWN") == (
        "任务失败，请稍后重试。"
    )


@pytest.mark.parametrize(
    "malicious",
    [
        "https://evil.example/api?token=secret",
        "C:\\Windows\\System32\\config\\secret.key",
        "/var/lib/postgresql/secret",
        "InternalError: SELECT * FROM users WHERE password='hunter2'",
        "Bearer eyJhbGciOiJIUzI1NiJ9.abc",
    ],
)
def test_public_error_message_never_echoes_code_or_exception_text(malicious: str) -> None:
    # A malicious "code" is treated as unknown: the response is the fixed fallback
    # copy, never the input itself nor any embedded URL/path/token.
    assert public_error_message(malicious) == "任务失败，请稍后重试。"
