use serde::Serialize;
use std::{
    sync::Mutex,
    time::{Duration, Instant},
};

#[derive(Debug)]
struct CircuitInner {
    failures: u32,
    opened_at: Option<Instant>,
    half_open_in_flight: bool,
}

#[derive(Debug)]
pub struct CircuitBreaker {
    threshold: u32,
    recovery: Duration,
    inner: Mutex<CircuitInner>,
}

#[derive(Clone, Debug, Serialize)]
pub struct CircuitSnapshot {
    pub state: &'static str,
    pub failures: u32,
    pub retry_after_ms: u64,
}

impl CircuitBreaker {
    pub fn new(threshold: u32, recovery: Duration) -> Self {
        Self {
            threshold,
            recovery,
            inner: Mutex::new(CircuitInner {
                failures: 0,
                opened_at: None,
                half_open_in_flight: false,
            }),
        }
    }

    pub fn allow_request(&self) -> bool {
        let mut inner = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        match inner.opened_at {
            None => true,
            Some(opened_at) if opened_at.elapsed() >= self.recovery => {
                if inner.half_open_in_flight {
                    false
                } else {
                    inner.half_open_in_flight = true;
                    true
                }
            }
            Some(_) => false,
        }
    }

    pub fn record_success(&self) {
        let mut inner = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        inner.failures = 0;
        inner.opened_at = None;
        inner.half_open_in_flight = false;
    }

    pub fn record_retryable_failure(&self) {
        let mut inner = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        inner.half_open_in_flight = false;
        inner.failures = inner.failures.saturating_add(1);
        if inner.opened_at.is_some() || inner.failures >= self.threshold {
            inner.opened_at = Some(Instant::now());
        }
    }

    pub fn snapshot(&self) -> CircuitSnapshot {
        let inner = self.inner.lock().unwrap_or_else(|error| error.into_inner());
        match inner.opened_at {
            None => CircuitSnapshot {
                state: "closed",
                failures: inner.failures,
                retry_after_ms: 0,
            },
            Some(opened_at) if opened_at.elapsed() >= self.recovery => CircuitSnapshot {
                state: if inner.half_open_in_flight {
                    "half_open"
                } else {
                    "probe_ready"
                },
                failures: inner.failures,
                retry_after_ms: 0,
            },
            Some(opened_at) => CircuitSnapshot {
                state: "open",
                failures: inner.failures,
                retry_after_ms: self
                    .recovery
                    .saturating_sub(opened_at.elapsed())
                    .as_millis() as u64,
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn opens_and_recovers_with_a_single_probe() {
        let breaker = CircuitBreaker::new(2, Duration::from_millis(1));
        assert!(breaker.allow_request());
        breaker.record_retryable_failure();
        breaker.record_retryable_failure();
        assert!(!breaker.allow_request());
        std::thread::sleep(Duration::from_millis(3));
        assert!(breaker.allow_request());
        assert!(!breaker.allow_request());
        breaker.record_success();
        assert_eq!(breaker.snapshot().state, "closed");
    }
}
