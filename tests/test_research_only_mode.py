"""
Tests for research-only mode enforcement.

Phase 1 requirement: ensure the system cannot be configured for live trading.
"""
from __future__ import annotations

import pytest

from ashare_ai.core.config import Settings


def test_research_only_mode_is_enabled():
    """Research-only mode must be enabled and cannot be disabled."""
    settings = Settings()
    assert settings.research_only_mode is True
    assert settings.qmt_enabled is False
    assert settings.auto_trading_enabled is False
    assert settings.execution_mode == "RESEARCH_ONLY"


def test_qmt_cannot_be_enabled():
    """QMT integration must not be available even if config attempts to enable it."""
    settings = Settings(qmt_enabled=True)
    # Should be forced to False regardless of input
    assert settings.qmt_enabled is False


def test_auto_trading_cannot_be_enabled():
    """Auto trading must not be available even if config attempts to enable it."""
    settings = Settings(auto_trading_enabled=True)
    # Should be forced to False regardless of input
    assert settings.auto_trading_enabled is False


def test_execution_mode_is_locked():
    """Execution mode must be locked to RESEARCH_ONLY."""
    settings = Settings(execution_mode="LIVE")
    # Should be forced to RESEARCH_ONLY regardless of input
    assert settings.execution_mode == "RESEARCH_ONLY"


def test_health_endpoint_exposes_research_mode():
    """Health endpoint response model must include research-only fields.

    This tests the HealthResponse model structure. Full endpoint testing
    requires a running PostgreSQL instance.
    """
    from ashare_ai.api.schemas import HealthResponse

    # Verify HealthResponse model has the required fields
    response = HealthResponse(
        status="ok",
        version="test",
        database="ok",
        qmt_enabled=False,
        auto_trading_enabled=False,
        execution_mode="RESEARCH_ONLY",
    )

    assert response.qmt_enabled is False
    assert response.auto_trading_enabled is False
    assert response.execution_mode == "RESEARCH_ONLY"
