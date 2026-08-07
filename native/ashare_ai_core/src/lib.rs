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

    #[pymodule]
    fn ashare_ai_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(calculate_technical_metrics, m)?)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::technical_metrics;

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
}
