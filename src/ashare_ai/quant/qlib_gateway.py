from __future__ import annotations

import importlib
import importlib.util
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

import numpy as np
import pandas as pd  # type: ignore[import-untyped]
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ashare_ai.core.hashing import stable_hash


class QlibUnavailableError(RuntimeError):
    pass


class WalkForwardSplit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    train_start: date
    train_end: date
    valid_start: date
    valid_end: date
    test_start: date
    test_end: date


def rolling_splits(
    dates: Sequence[date],
    *,
    train_window: int,
    valid_window: int,
    test_window: int,
    step: int,
) -> tuple[WalkForwardSplit, ...]:
    if min(train_window, valid_window, test_window, step) <= 0:
        raise ValueError("all rolling-window lengths must be positive")
    ordered = sorted(set(dates))
    required = train_window + valid_window + test_window
    splits: list[WalkForwardSplit] = []
    start = 0
    while start + required <= len(ordered):
        train = ordered[start : start + train_window]
        valid_start_index = start + train_window
        valid = ordered[valid_start_index : valid_start_index + valid_window]
        test_start_index = valid_start_index + valid_window
        test = ordered[test_start_index : test_start_index + test_window]
        splits.append(
            WalkForwardSplit(
                train_start=train[0],
                train_end=train[-1],
                valid_start=valid[0],
                valid_end=valid[-1],
                test_start=test[0],
                test_end=test[-1],
            )
        )
        start += step
    return tuple(splits)


def _coerce_utc(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, utc=True, errors="raise")


def validate_training_frame(
    frame: pd.DataFrame,
    *,
    training_cutoff: datetime,
    feature_date_column: str = "datetime",
    label_end_column: str = "label_end_date",
    available_at_column: str = "available_at",
) -> None:
    required = {feature_date_column, label_end_column, available_at_column, "label"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"training frame is missing columns: {sorted(missing)}")
    if training_cutoff.tzinfo is None or training_cutoff.utcoffset() is None:
        raise ValueError("training_cutoff must be timezone-aware")
    available = _coerce_utc(frame[available_at_column])
    cutoff = pd.Timestamp(training_cutoff).tz_convert("UTC")
    if (available > cutoff).any():
        raise ValueError("training frame contains rows unavailable at training cutoff")
    label_end = pd.to_datetime(frame[label_end_column], errors="raise").dt.date
    if (label_end > training_cutoff.date()).any():
        raise ValueError("training frame contains immature labels")
    feature_date = pd.to_datetime(frame[feature_date_column], errors="raise").dt.date
    if (label_end <= feature_date).any():
        raise ValueError("label_end_date must be after feature date")
    if frame["label"].isna().any():
        raise ValueError("training frame contains missing labels")


def mature_training_rows(
    frame: pd.DataFrame,
    *,
    training_cutoff: datetime,
    feature_date_column: str = "datetime",
    label_end_column: str = "label_end_date",
    available_at_column: str = "available_at",
) -> pd.DataFrame:
    required = {feature_date_column, label_end_column, available_at_column, "label"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"training frame is missing columns: {sorted(missing)}")
    if training_cutoff.tzinfo is None or training_cutoff.utcoffset() is None:
        raise ValueError("training_cutoff must be timezone-aware")
    cutoff = pd.Timestamp(training_cutoff).tz_convert("UTC")
    available = _coerce_utc(frame[available_at_column])
    label_end = pd.to_datetime(frame[label_end_column], errors="raise").dt.date
    mask = (available <= cutoff) & (label_end <= training_cutoff.date()) & frame["label"].notna()
    result = frame.loc[mask].copy()
    validate_training_frame(
        result,
        training_cutoff=training_cutoff,
        feature_date_column=feature_date_column,
        label_end_column=label_end_column,
        available_at_column=available_at_column,
    )
    return result.sort_values([feature_date_column, "instrument"], kind="mergesort")


class PredictionEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mean_ic: float
    mean_rank_ic: float
    ic_std: float
    rank_ic_std: float
    ic_positive_ratio: float
    rank_ic_positive_ratio: float
    group_returns: dict[int, float]
    long_short_group_return: float
    stability_ic: tuple[float, ...]
    stability_rank_ic: tuple[float, ...]


class PredictionMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    training_cutoff: AwareDatetime
    prediction_at: AwareDatetime
    dataset_manifest_hash: str = Field(min_length=64, max_length=64)
    model_artifact_hash: str = Field(min_length=64, max_length=64)
    output_hash: str = Field(min_length=64, max_length=64)
    row_count: int = Field(ge=0)


@dataclass(frozen=True)
class PredictionBatch:
    predictions: Any
    metadata: PredictionMetadata


def validate_prediction_frame(
    frame: pd.DataFrame,
    *,
    expected_prediction_at: datetime | None = None,
    feature_date_column: str = "datetime",
    available_at_column: str = "available_at",
    prediction_at_column: str = "prediction_at",
) -> None:
    required = {
        "instrument",
        feature_date_column,
        available_at_column,
        prediction_at_column,
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"prediction frame is missing columns: {sorted(missing)}")
    available = _coerce_utc(frame[available_at_column])
    prediction_at = _coerce_utc(frame[prediction_at_column])
    if (available > prediction_at).any():
        raise ValueError("prediction frame contains features unavailable at prediction_at")
    if expected_prediction_at is not None:
        if expected_prediction_at.tzinfo is None or expected_prediction_at.utcoffset() is None:
            raise ValueError("prediction_at must be timezone-aware")
        expected = pd.Timestamp(expected_prediction_at).tz_convert("UTC")
        if (prediction_at != expected).any():
            raise ValueError("prediction frame prediction_at does not match requested cutoff")
    feature_dates = pd.to_datetime(frame[feature_date_column], errors="raise").dt.date
    prediction_dates = prediction_at.dt.date
    if (feature_dates > prediction_dates).any():
        raise ValueError("prediction frame contains future feature dates")


def _daily_correlations(
    frame: pd.DataFrame,
    *,
    date_column: str,
    prediction_column: str,
    label_column: str,
    method: str,
) -> pd.Series:
    values: dict[object, float] = {}
    for trading_date, group in frame.groupby(date_column, sort=True):
        clean = group[[prediction_column, label_column]].dropna()
        if len(clean) < 2 or clean[prediction_column].nunique() < 2:
            continue
        if method == "spearman":
            left = clean[prediction_column].rank(method="average")
            right = clean[label_column].rank(method="average")
            correlation = left.corr(right, method="pearson")
        else:
            correlation = clean[prediction_column].corr(clean[label_column], method=method)
        if pd.notna(correlation):
            values[trading_date] = float(correlation)
    return pd.Series(values, dtype=float).sort_index()


def evaluate_predictions(
    frame: pd.DataFrame,
    *,
    date_column: str,
    prediction_column: str,
    label_column: str,
    groups: int,
    stability_window: int,
) -> PredictionEvaluation:
    if groups < 2 or stability_window <= 0:
        raise ValueError("groups must be >= 2 and stability_window positive")
    required = {date_column, prediction_column, label_column, "instrument"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"prediction frame is missing columns: {sorted(missing)}")
    ordered = frame.sort_values([date_column, "instrument"], kind="mergesort").copy()
    ic = _daily_correlations(
        ordered,
        date_column=date_column,
        prediction_column=prediction_column,
        label_column=label_column,
        method="pearson",
    )
    rank_ic = _daily_correlations(
        ordered,
        date_column=date_column,
        prediction_column=prediction_column,
        label_column=label_column,
        method="spearman",
    )
    if ic.empty or rank_ic.empty:
        raise ValueError("not enough cross-sectional variation to calculate IC")

    grouped_returns: dict[int, list[float]] = {group: [] for group in range(groups)}
    for _, daily in ordered.groupby(date_column, sort=True):
        clean = daily[[prediction_column, label_column, "instrument"]].dropna().copy()
        if len(clean) < groups:
            continue
        clean = clean.sort_values([prediction_column, "instrument"], kind="mergesort")
        ranks = clean[prediction_column].rank(method="first", pct=True)
        clean["_group"] = np.minimum((ranks * groups).astype(int), groups - 1)
        for group, values in clean.groupby("_group", sort=True):
            grouped_returns[int(group)].append(float(values[label_column].mean()))
    group_means = {
        group: float(np.mean(values)) if values else float("nan")
        for group, values in grouped_returns.items()
    }
    if any(np.isnan(value) for value in group_means.values()):
        raise ValueError("not enough observations for all prediction groups")

    stability_ic = tuple(
        float(ic.iloc[start : start + stability_window].mean())
        for start in range(0, len(ic), stability_window)
    )
    stability_rank_ic = tuple(
        float(rank_ic.iloc[start : start + stability_window].mean())
        for start in range(0, len(rank_ic), stability_window)
    )
    return PredictionEvaluation(
        mean_ic=float(ic.mean()),
        mean_rank_ic=float(rank_ic.mean()),
        ic_std=float(ic.std(ddof=0)),
        rank_ic_std=float(rank_ic.std(ddof=0)),
        ic_positive_ratio=float((ic > 0).mean()),
        rank_ic_positive_ratio=float((rank_ic > 0).mean()),
        group_returns=group_means,
        long_short_group_return=group_means[groups - 1] - group_means[0],
        stability_ic=stability_ic,
        stability_rank_ic=stability_rank_ic,
    )


def filter_prediction_quantile(
    frame: pd.DataFrame,
    *,
    date_column: str,
    prediction_column: str,
    minimum_percentile: float,
) -> pd.DataFrame:
    if not 0 <= minimum_percentile <= 1:
        raise ValueError("minimum_percentile must be within [0, 1]")
    validate_prediction_frame(frame, feature_date_column=date_column)
    ordered = frame.sort_values([date_column, "instrument"], kind="mergesort").copy()
    ordered["prediction_percentile"] = ordered.groupby(date_column, sort=True)[
        prediction_column
    ].rank(method="first", pct=True)
    return ordered.loc[ordered["prediction_percentile"] >= minimum_percentile].reset_index(
        drop=True
    )


class TrainableModel(Protocol):
    def fit(self, train_frame: pd.DataFrame, valid_frame: pd.DataFrame | None = None) -> Any: ...

    def predict(self, test_frame: pd.DataFrame) -> Any: ...


class QlibGateway:
    """Optional gateway that keeps Qlib imports outside the mandatory runtime path."""

    @property
    def available(self) -> bool:
        return importlib.util.find_spec("qlib") is not None

    def require(self) -> Any:
        if not self.available:
            raise QlibUnavailableError(
                "pyqlib is not installed; install the project's quant optional dependency"
            )
        return importlib.import_module("qlib")

    def initialize(self, **kwargs: Any) -> None:
        qlib = self.require()
        qlib.init(**kwargs)

    def fit_predict(
        self,
        *,
        model: TrainableModel,
        train_frame: pd.DataFrame,
        test_frame: pd.DataFrame,
        training_cutoff: datetime,
        prediction_at: datetime,
        dataset_manifest_hash: str,
        model_artifact_hash: str,
        valid_frame: pd.DataFrame | None = None,
    ) -> PredictionBatch:
        validate_training_frame(train_frame, training_cutoff=training_cutoff)
        if valid_frame is not None:
            validate_training_frame(valid_frame, training_cutoff=training_cutoff)
        validate_prediction_frame(
            test_frame,
            expected_prediction_at=prediction_at,
        )
        model.fit(train_frame, valid_frame)
        predictions = model.predict(test_frame)
        return PredictionBatch(
            predictions=predictions,
            metadata=PredictionMetadata(
                training_cutoff=training_cutoff,
                prediction_at=prediction_at,
                dataset_manifest_hash=dataset_manifest_hash,
                model_artifact_hash=model_artifact_hash,
                output_hash=stable_hash(_prediction_payload(predictions)),
                row_count=len(test_frame),
            ),
        )


def _prediction_payload(predictions: Any) -> Any:
    if isinstance(predictions, pd.DataFrame):
        return json.loads(predictions.sort_index().to_json(orient="split", date_format="iso"))
    if isinstance(predictions, pd.Series):
        ordered = predictions.sort_index()
        return json.loads(ordered.to_json(orient="split", date_format="iso"))
    if isinstance(predictions, np.ndarray):
        return predictions.tolist()
    if isinstance(predictions, Sequence) and not isinstance(predictions, (str, bytes)):
        return list(predictions)
    return predictions
