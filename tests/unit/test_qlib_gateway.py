from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest

from ashare_ai.quant.qlib_gateway import (
    QlibGateway,
    QlibUnavailableError,
    evaluate_predictions,
    filter_prediction_quantile,
    mature_training_rows,
    rolling_splits,
    validate_prediction_frame,
    validate_training_frame,
)


def test_rolling_splits_have_strict_train_valid_test_order() -> None:
    dates = [date(2025, 1, 1) + timedelta(days=index) for index in range(10)]
    splits = rolling_splits(
        dates,
        train_window=4,
        valid_window=2,
        test_window=2,
        step=2,
    )
    assert len(splits) == 2
    assert splits[0].train_end < splits[0].valid_start
    assert splits[0].valid_end < splits[0].test_start


def test_training_cutoff_rejects_future_and_immature_labels() -> None:
    cutoff = datetime(2025, 1, 10, 18, tzinfo=UTC)
    frame = pd.DataFrame(
        {
            "datetime": ["2025-01-01"],
            "instrument": ["600000.SH"],
            "label": [0.01],
            "label_end_date": ["2025-01-11"],
            "available_at": ["2025-01-10T10:00:00Z"],
        }
    )
    with pytest.raises(ValueError, match="immature labels"):
        validate_training_frame(frame, training_cutoff=cutoff)

    frame.loc[0, "label_end_date"] = "2025-01-09"
    frame.loc[0, "available_at"] = "2025-01-10T19:00:00Z"
    with pytest.raises(ValueError, match="unavailable"):
        validate_training_frame(frame, training_cutoff=cutoff)


def test_mature_training_rows_filter_before_validation() -> None:
    cutoff = datetime(2025, 1, 10, 18, tzinfo=UTC)
    frame = pd.DataFrame(
        {
            "datetime": ["2025-01-01", "2025-01-02"],
            "instrument": ["600000.SH", "600001.SH"],
            "label": [0.01, 0.02],
            "label_end_date": ["2025-01-09", "2025-01-11"],
            "available_at": ["2025-01-10T10:00:00Z", "2025-01-10T10:00:00Z"],
        }
    )
    mature = mature_training_rows(frame, training_cutoff=cutoff)
    assert mature["instrument"].tolist() == ["600000.SH"]


def test_ic_rank_ic_groups_stability_and_quantile_filter() -> None:
    rows: list[dict[str, object]] = []
    for day in ("2025-01-01", "2025-01-02"):
        for index in range(10):
            rows.append(
                {
                    "datetime": day,
                    "instrument": f"{600000 + index:06d}.SH",
                    "prediction": float(index),
                    "label": float(index) / 100,
                    "available_at": f"{day}T17:00:00Z",
                    "prediction_at": f"{day}T18:00:00Z",
                }
            )
    frame = pd.DataFrame(rows)
    evaluation = evaluate_predictions(
        frame,
        date_column="datetime",
        prediction_column="prediction",
        label_column="label",
        groups=5,
        stability_window=1,
    )
    assert evaluation.mean_ic == pytest.approx(1.0)
    assert evaluation.mean_rank_ic == pytest.approx(1.0)
    assert evaluation.long_short_group_return > 0
    assert len(evaluation.stability_ic) == 2

    filtered = filter_prediction_quantile(
        frame,
        date_column="datetime",
        prediction_column="prediction",
        minimum_percentile=0.8,
    )
    assert len(filtered) == 6


def test_prediction_frame_rejects_future_features() -> None:
    frame = pd.DataFrame(
        {
            "datetime": ["2025-01-02"],
            "instrument": ["600000.SH"],
            "prediction": [0.1],
            "available_at": ["2025-01-02T18:00:01Z"],
            "prediction_at": ["2025-01-02T18:00:00Z"],
        }
    )
    with pytest.raises(ValueError, match="unavailable"):
        validate_prediction_frame(frame)


def test_fit_predict_returns_cutoff_manifest_and_model_metadata() -> None:
    cutoff = datetime(2025, 1, 10, 18, tzinfo=UTC)
    train = pd.DataFrame(
        {
            "datetime": ["2025-01-01"],
            "instrument": ["600000.SH"],
            "label": [0.01],
            "label_end_date": ["2025-01-09"],
            "available_at": ["2025-01-10T10:00:00Z"],
        }
    )
    test = pd.DataFrame(
        {
            "datetime": ["2025-01-10"],
            "instrument": ["600000.SH"],
            "available_at": ["2025-01-10T17:00:00Z"],
            "prediction_at": ["2025-01-10T18:00:00Z"],
        }
    )

    class Model:
        def fit(self, train_frame: pd.DataFrame, valid_frame: pd.DataFrame | None = None) -> None:
            assert len(train_frame) == 1
            assert valid_frame is None

        def predict(self, test_frame: pd.DataFrame) -> pd.Series:
            return pd.Series([0.5], index=test_frame["instrument"])

    batch = QlibGateway().fit_predict(
        model=Model(),
        train_frame=train,
        test_frame=test,
        training_cutoff=cutoff,
        prediction_at=cutoff,
        dataset_manifest_hash="a" * 64,
        model_artifact_hash="b" * 64,
    )
    assert batch.metadata.training_cutoff == cutoff
    assert batch.metadata.dataset_manifest_hash == "a" * 64
    assert batch.metadata.model_artifact_hash == "b" * 64
    assert batch.metadata.row_count == 1


def test_qlib_dependency_is_optional() -> None:
    gateway = QlibGateway()
    if not gateway.available:
        with pytest.raises(QlibUnavailableError):
            gateway.require()
