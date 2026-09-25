//! Small, deterministic kernels used by the Python research pipeline.
//!
//! PIT filtering and domain validation stay in Python. This crate only operates on
//! already-frozen numeric arrays so it can be tested independently and replaced safely.

#[derive(Debug, PartialEq)]
pub struct TechnicalMetrics {
    pub return_5d: Option<f64>,
    pub return_20d: Option<f64>,
    pub close_to_ma20: Option<f64>,
    pub annualized_volatility_20d: Option<f64>,
    pub volume_ratio_5_to_20: Option<f64>,
    pub max_drawdown_60d: Option<f64>,
}

#[derive(Debug, PartialEq, Clone, Copy)]
pub struct SignalEvidence {
    pub triggered: bool,
    pub confidence: f64,
    pub primary: f64,
    pub secondary: f64,
}

#[derive(Debug, PartialEq)]
pub struct SignalDetections {
    pub intraday_drop: SignalEvidence,
    pub volume_breakout: SignalEvidence,
    pub volume_price_divergence: SignalEvidence,
    pub moving_average_death_cross: SignalEvidence,
}

pub fn technical_metrics(
    closes: &[f64],
    volumes: &[f64],
) -> Result<TechnicalMetrics, &'static str> {
    if closes.len() != volumes.len() {
        return Err("closes and volumes must have equal lengths");
    }
    if closes.iter().chain(volumes).any(|value| !value.is_finite()) {
        return Err("technical inputs must be finite");
    }

    let returns: Vec<f64> = closes
        .windows(2)
        .map(|pair| pair[1] / pair[0] - 1.0)
        .collect();
    Ok(TechnicalMetrics {
        return_5d: period_return(closes, 5),
        return_20d: period_return(closes, 20),
        close_to_ma20: close_to_average(closes, 20),
        annualized_volatility_20d: annualized_volatility(&returns, 20),
        volume_ratio_5_to_20: volume_ratio(volumes),
        max_drawdown_60d: max_drawdown(&tail(closes, 60)),
    })
}

/// Detect the four deterministic monitoring signals used by the research-only
/// monitor.  Threshold versioning and PIT timestamps stay in Python; this
/// kernel only evaluates finite, already ordered OHLCV values.
pub fn detect_signals(
    closes: &[f64],
    volumes: &[f64],
) -> Result<SignalDetections, &'static str> {
    if closes.len() != volumes.len() {
        return Err("closes and volumes must have equal lengths");
    }
    if closes.iter().chain(volumes).any(|value| !value.is_finite()) {
        return Err("signal inputs must be finite");
    }
    if closes.iter().any(|value| *value <= 0.0) || volumes.iter().any(|value| *value < 0.0) {
        return Err("signal prices must be positive and volumes non-negative");
    }

    let intraday_drop = if closes.len() >= 4 {
        let start = closes[closes.len() - 4];
        let change = closes[closes.len() - 1] / start - 1.0;
        let confidence = ((-change) / 0.03).clamp(0.0, 1.0);
        SignalEvidence {
            triggered: change <= -0.03,
            confidence,
            primary: change,
            secondary: 4.0,
        }
    } else {
        empty_signal()
    };

    let volume_breakout = if closes.len() >= 21 {
        let last = *closes.last().unwrap();
        let prior = &closes[closes.len() - 21..closes.len() - 1];
        let prior_high = prior.iter().copied().fold(f64::MIN, f64::max);
        let average_volume = mean(&volumes[volumes.len() - 21..volumes.len() - 1]);
        let volume_ratio = if average_volume > 0.0 {
            volumes[volumes.len() - 1] / average_volume
        } else {
            0.0
        };
        let price_ratio = if prior_high > 0.0 { last / prior_high } else { 0.0 };
        let confidence = (((price_ratio - 1.0) / 0.01).max(0.0)
            + ((volume_ratio - 1.5) / 1.5).max(0.0))
            .mul_add(0.5, 0.0)
            .clamp(0.0, 1.0);
        SignalEvidence {
            triggered: price_ratio >= 1.01 && volume_ratio >= 1.5,
            confidence,
            primary: price_ratio - 1.0,
            secondary: volume_ratio,
        }
    } else {
        empty_signal()
    };

    let volume_price_divergence = if closes.len() >= 10 {
        let split = closes.len() - 5;
        let price_change = closes[closes.len() - 1] / closes[split] - 1.0;
        let first_volume = mean(&volumes[volumes.len() - 10..split]);
        let second_volume = mean(&volumes[split..]);
        let volume_change = if first_volume > 0.0 {
            second_volume / first_volume - 1.0
        } else {
            0.0
        };
        let divergence = price_change.abs().min(1.0) * (-(volume_change)).max(0.0);
        SignalEvidence {
            triggered: price_change >= 0.03 && volume_change <= -0.20,
            confidence: (divergence / 0.03).clamp(0.0, 1.0),
            primary: price_change,
            secondary: volume_change,
        }
    } else {
        empty_signal()
    };

    let moving_average_death_cross = if closes.len() >= 21 {
        let previous_short = mean(&closes[closes.len() - 6..closes.len() - 1]);
        let previous_long = mean(&closes[closes.len() - 21..closes.len() - 1]);
        let current_short = mean(&closes[closes.len() - 5..]);
        let current_long = mean(&closes[closes.len() - 20..]);
        let gap = previous_short - previous_long;
        let current_gap = current_short - current_long;
        SignalEvidence {
            triggered: gap >= 0.0 && current_gap < 0.0,
            confidence: ((-current_gap) / current_long.max(f64::EPSILON) / 0.02).clamp(0.0, 1.0),
            primary: current_short / current_long.max(f64::EPSILON) - 1.0,
            secondary: previous_short / previous_long.max(f64::EPSILON) - 1.0,
        }
    } else {
        empty_signal()
    };

    Ok(SignalDetections {
        intraday_drop,
        volume_breakout,
        volume_price_divergence,
        moving_average_death_cross,
    })
}

fn empty_signal() -> SignalEvidence {
    SignalEvidence {
        triggered: false,
        confidence: 0.0,
        primary: 0.0,
        secondary: 0.0,
    }
}

fn tail(values: &[f64], window: usize) -> &[f64] {
    let start = values.len().saturating_sub(window);
    &values[start..]
}

fn period_return(values: &[f64], periods: usize) -> Option<f64> {
    if values.len() <= periods {
        return None;
    }
    let previous = values[values.len() - periods - 1];
    if previous == 0.0 {
        None
    } else {
        Some(values[values.len() - 1] / previous - 1.0)
    }
}

fn close_to_average(values: &[f64], window: usize) -> Option<f64> {
    if values.len() < window {
        return None;
    }
    let average = mean(tail(values, window));
    if average == 0.0 {
        None
    } else {
        Some(values[values.len() - 1] / average - 1.0)
    }
}

fn annualized_volatility(values: &[f64], window: usize) -> Option<f64> {
    if values.len() < window {
        return None;
    }
    let sample = tail(values, window);
    let average = mean(sample);
    let variance = sample
        .iter()
        .map(|value| (value - average).powi(2))
        .sum::<f64>()
        / sample.len() as f64;
    Some(variance.sqrt() * 252.0_f64.sqrt())
}

fn volume_ratio(values: &[f64]) -> Option<f64> {
    if values.len() < 20 {
        return None;
    }
    let long_average = mean(tail(values, 20));
    if long_average == 0.0 {
        None
    } else {
        Some(mean(tail(values, 5)) / long_average)
    }
}

fn max_drawdown(values: &[f64]) -> Option<f64> {
    if values.len() < 2 {
        return None;
    }
    let mut peak = values[0];
    let mut drawdown: f64 = 0.0;
    for value in values.iter().copied() {
        peak = peak.max(value);
        drawdown = drawdown.min(value / peak - 1.0);
    }
    Some(drawdown)
}

fn mean(values: &[f64]) -> f64 {
    values.iter().sum::<f64>() / values.len() as f64
}

#[cfg(feature = "python")]
mod python {
    use super::technical_metrics;
    use pyo3::prelude::*;

    #[pyfunction]
    fn calculate_technical_metrics(
        closes: Vec<f64>,
        volumes: Vec<f64>,
    ) -> PyResult<(
        Option<f64>,
        Option<f64>,
        Option<f64>,
        Option<f64>,
        Option<f64>,
        Option<f64>,
    )> {
        let metrics = technical_metrics(&closes, &volumes)
            .map_err(pyo3::exceptions::PyValueError::new_err)?;
        Ok((
            metrics.return_5d,
            metrics.return_20d,
            metrics.close_to_ma20,
            metrics.annualized_volatility_20d,
            metrics.volume_ratio_5_to_20,
            metrics.max_drawdown_60d,
        ))
    }

    #[pyfunction]
    fn detect_monitor_signals(
        closes: Vec<f64>,
        volumes: Vec<f64>,
    ) -> PyResult<Vec<(String, bool, f64, f64, f64)>> {
        let detections = super::detect_signals(&closes, &volumes)
            .map_err(pyo3::exceptions::PyValueError::new_err)?;
        Ok(vec![
            (
                "INTRADAY_DROP".to_string(),
                detections.intraday_drop.triggered,
                detections.intraday_drop.confidence,
                detections.intraday_drop.primary,
                detections.intraday_drop.secondary,
            ),
            (
                "VOLUME_BREAKOUT".to_string(),
                detections.volume_breakout.triggered,
                detections.volume_breakout.confidence,
                detections.volume_breakout.primary,
                detections.volume_breakout.secondary,
            ),
            (
                "VOLUME_PRICE_DIVERGENCE".to_string(),
                detections.volume_price_divergence.triggered,
                detections.volume_price_divergence.confidence,
                detections.volume_price_divergence.primary,
                detections.volume_price_divergence.secondary,
            ),
            (
                "MA_DEATH_CROSS".to_string(),
                detections.moving_average_death_cross.triggered,
                detections.moving_average_death_cross.confidence,
                detections.moving_average_death_cross.primary,
                detections.moving_average_death_cross.secondary,
            ),
        ])
    }

    #[pymodule]
    fn ashare_ai_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(calculate_technical_metrics, m)?)?;
        m.add_function(wrap_pyfunction!(detect_monitor_signals, m)?)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::{detect_signals, technical_metrics};

    #[test]
    fn calculates_expected_metrics() {
        let closes: Vec<f64> = (1..=25).map(f64::from).collect();
        let volumes = vec![100.0; closes.len()];
        let result = technical_metrics(&closes, &volumes).expect("valid inputs");
        assert!(result.return_5d.is_some());
        assert!(result.return_20d.is_some());
        assert!(result.close_to_ma20.is_some());
        assert!(result.annualized_volatility_20d.is_some());
        assert_eq!(result.volume_ratio_5_to_20, Some(1.0));
        assert!(result.max_drawdown_60d.unwrap().abs() < f64::EPSILON);
    }

    #[test]
    fn rejects_mismatched_or_non_finite_inputs() {
        assert!(technical_metrics(&[1.0], &[]).is_err());
        assert!(technical_metrics(&[f64::NAN], &[1.0]).is_err());
    }

    #[test]
    fn detects_breakout_and_death_cross() {
        let mut closes = vec![10.0; 25];
        for (index, value) in closes.iter_mut().enumerate() {
            *value += index as f64 * 0.1;
        }
        closes[24] = 13.0;
        let mut volumes = vec![100.0; 25];
        volumes[24] = 200.0;
        let result = detect_signals(&closes, &volumes).expect("valid signal input");
        assert!(result.volume_breakout.triggered);
    }
}
