"""
CLI 命令：决策预测。

提供命令行接口用于：
- 单个股票决策预测
- 批量决策预测
- 决策配置与公式版本查询

新预测只接受 ``factor`` 或兼容别名 ``legacy``；``jev`` 已移除。
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

from ashare_ai.agents.decision.router import normalize_request_mode
from ashare_ai.core.system_settings import get_effective_settings
from ashare_ai.orchestration.decision_integration import (
    batch_generate_decisions,
    generate_decision_for_symbol,
)
from ashare_ai.scoring.factor import FACTOR_FORMULA_VERSION

logger = logging.getLogger(__name__)


def _load_bundle(bundle_dir: Path | None, trading_date: date | None):
    """Load the frozen bundle for ``trading_date`` (latest when omitted)."""

    from ashare_ai.orchestration.bundle_loader import find_latest_bundle, load_bundle_from_disk

    directory = bundle_dir or get_effective_settings().bundle_storage_dir
    resolved = trading_date or find_latest_bundle(directory)
    if resolved is None:
        raise ValueError(f"no bundle found in {directory}")
    return load_bundle_from_disk(directory, resolved), resolved


async def predict_single(
    *,
    symbol: str,
    bundle_dir: Path | None = None,
    trading_date: date | None = None,
    mode: str = "factor",
) -> None:
    """Generate and print one decision."""

    normalize_request_mode(mode)
    bundle, resolved_date = _load_bundle(bundle_dir, trading_date)
    decision = await generate_decision_for_symbol(
        bundle=bundle,
        symbol=symbol,
        trading_date=resolved_date,
        mode=mode,
    )
    print(f"symbol={decision.symbol} trading_date={resolved_date}")
    print(f"mode={decision.mode} model_version={decision.model_version}")
    confidence = max(decision.probabilities.action.model_dump().values())
    print(f"action={decision.action} risk_level={decision.risk}")
    print(f"position_size={decision.position / 100:.4f} confidence={confidence:.4f}")
    print(decision.model_dump_json(indent=2))


async def predict_batch(
    *,
    symbols_file: Path,
    output_file: Path,
    bundle_dir: Path | None = None,
    trading_date: date | None = None,
    mode: str = "factor",
) -> None:
    """Generate decisions for every symbol in ``symbols_file``."""

    normalize_request_mode(mode)
    symbols = [
        line.strip() for line in symbols_file.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    bundle, resolved_date = _load_bundle(bundle_dir, trading_date)
    decisions = await batch_generate_decisions(
        bundle=bundle,
        symbols=symbols,
        mode=mode,
        fallback_enabled=True,
    )
    payload = {
        "trading_date": resolved_date.isoformat(),
        "mode": "factor",
        "model_version": FACTOR_FORMULA_VERSION,
        "requested": len(symbols),
        "decisions": {symbol: json.loads(item.model_dump_json()) for symbol, item in decisions.items()},
        "failed_symbols": [symbol for symbol in symbols if symbol not in decisions],
    }
    output_file.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"wrote {len(decisions)} decisions to {output_file}")


def show_info() -> None:
    """Print the effective decision configuration."""

    config = get_effective_settings()
    print(f"decision_mode={config.decision_mode}")
    print(f"formula_version={FACTOR_FORMULA_VERSION}")
    print(f"fallback_enabled={config.decision_fallback_enabled}")
    print(f"confidence_threshold={config.factor_confidence_threshold}")
    print(f"system2_enabled={config.decision_system2_enabled}")
    print(f"strategy_evolution_enabled={config.strategy_evolution_enabled}")
    print(f"strategy_evolution_weekday={config.strategy_evolution_weekday}")
    print(f"strategy_evolution_hour={config.strategy_evolution_hour}")


def list_models() -> None:
    """List the available decision formulas.

    Only the versioned deterministic factor formula exists; there is no local
    model directory to scan.
    """

    print(f"{FACTOR_FORMULA_VERSION}\tbuiltin\tfactor")
