use serde::Serialize;
use std::{
    sync::atomic::{AtomicBool, AtomicU64, Ordering},
    time::{Instant, SystemTime, UNIX_EPOCH},
};

#[derive(Debug, Default)]
pub struct ProviderMetrics {
    requests: AtomicU64,
    successes: AtomicU64,
    errors: AtomicU64,
    retryable_errors: AtomicU64,
    fallback_in: AtomicU64,
    active: AtomicU64,
    total_latency_us: AtomicU64,
    last_latency_us: AtomicU64,
}

#[derive(Clone, Debug, Serialize)]
pub struct MetricsSnapshot {
    pub requests: u64,
    pub successes: u64,
    pub errors: u64,
    pub retryable_errors: u64,
    pub fallback_in: u64,
    pub active: u64,
    pub average_latency_ms: f64,
    pub last_latency_ms: f64,
}

impl ProviderMetrics {
    pub fn begin(&self) -> Instant {
        self.requests.fetch_add(1, Ordering::Relaxed);
        self.active.fetch_add(1, Ordering::Relaxed);
        Instant::now()
    }

    pub fn finish(&self, started: Instant, success: bool, retryable: bool) {
        let elapsed = started.elapsed().as_micros().min(u64::MAX as u128) as u64;
        self.active.fetch_sub(1, Ordering::Relaxed);
        self.total_latency_us.fetch_add(elapsed, Ordering::Relaxed);
        self.last_latency_us.store(elapsed, Ordering::Relaxed);
        if success {
            self.successes.fetch_add(1, Ordering::Relaxed);
        } else {
            self.errors.fetch_add(1, Ordering::Relaxed);
            if retryable {
                self.retryable_errors.fetch_add(1, Ordering::Relaxed);
            }
        }
    }

    pub fn record_fallback_in(&self) {
        self.fallback_in.fetch_add(1, Ordering::Relaxed);
    }

    pub fn snapshot(&self) -> MetricsSnapshot {
        let requests = self.requests.load(Ordering::Relaxed);
        let total_latency = self.total_latency_us.load(Ordering::Relaxed);
        MetricsSnapshot {
            requests,
            successes: self.successes.load(Ordering::Relaxed),
            errors: self.errors.load(Ordering::Relaxed),
            retryable_errors: self.retryable_errors.load(Ordering::Relaxed),
            fallback_in: self.fallback_in.load(Ordering::Relaxed),
            active: self.active.load(Ordering::Relaxed),
            average_latency_ms: if requests == 0 {
                0.0
            } else {
                total_latency as f64 / requests as f64 / 1_000.0
            },
            last_latency_ms: self.last_latency_us.load(Ordering::Relaxed) as f64 / 1_000.0,
        }
    }
}

#[derive(Debug)]
pub struct ProbeState {
    pub healthy: AtomicBool,
    pub checked_at: AtomicU64,
    pub status_code: AtomicU64,
}

impl Default for ProbeState {
    fn default() -> Self {
        Self {
            healthy: AtomicBool::new(false),
            checked_at: AtomicU64::new(0),
            status_code: AtomicU64::new(0),
        }
    }
}

impl ProbeState {
    pub fn update(&self, healthy: bool, status_code: u16) {
        self.healthy.store(healthy, Ordering::Relaxed);
        self.status_code
            .store(status_code as u64, Ordering::Relaxed);
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        self.checked_at.store(now, Ordering::Relaxed);
    }
}
