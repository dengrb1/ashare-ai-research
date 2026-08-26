use crate::config::matches_model;
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

/// 单个渠道的 API Key 池（参照 ccx keypool）：
/// - 按权重降序尝试（权重相同保持声明顺序，稳定排序）；
/// - 401/403 后进入冷却，冷却期内跳过该 Key；
/// - 每个 Key 可配置上游模型白名单（通配 / ! 否定）。
pub struct KeyPool {
    keys: Vec<Option<Arc<str>>>,
    weights: Vec<u32>,
    models: Vec<Vec<String>>,
    failed: Mutex<HashMap<usize, Instant>>,
    cooldown: Duration,
}

impl KeyPool {
    pub fn new(
        keys: Vec<Option<String>>,
        weights: Vec<u32>,
        models: Vec<Vec<String>>,
        cooldown: Duration,
    ) -> Self {
        debug_assert_eq!(keys.len(), weights.len());
        debug_assert_eq!(keys.len(), models.len());
        Self {
            keys: keys
                .into_iter()
                .map(|key| key.map(Arc::<str>::from))
                .collect(),
            weights,
            models,
            failed: Mutex::new(HashMap::new()),
            cooldown,
        }
    }

    /// 是否没有任何可用 Key（keyless 本地渠道）。
    pub fn keyless(&self) -> bool {
        self.keys.iter().all(Option::is_none)
    }

    /// 是否存在至少一个解析成功的 Key。
    pub fn has_key(&self) -> bool {
        self.keys.iter().any(Option::is_some)
    }

    /// 返回候选 Key 索引（权重降序、跳过冷却期内的失败 Key、按模型白名单过滤）。
    /// keyless 渠道返回 `[0]`，对应无 Authorization 头的单次尝试。
    pub fn candidates(&self, model: &str) -> Vec<usize> {
        if self.keyless() {
            return vec![0];
        }
        let now = Instant::now();
        let mut failed = self
            .failed
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let mut out = Vec::new();
        for (index, key) in self.keys.iter().enumerate() {
            let Some(_) = key else { continue };
            if let Some(failed_at) = failed.get(&index) {
                if now.duration_since(*failed_at) < self.cooldown {
                    continue;
                }
                failed.remove(&index);
            }
            if !self.models[index].is_empty() && !matches_model(model, &self.models[index]) {
                continue;
            }
            out.push(index);
        }
        if out.len() > 1 {
            out.sort_by(|left, right| {
                let lw = self.weights[*left].max(1);
                let rw = self.weights[*right].max(1);
                rw.cmp(&lw)
            });
        }
        out
    }

    /// Key 对应的密钥；None 表示不发送 Authorization 头。
    pub fn key(&self, index: usize) -> Option<Arc<str>> {
        self.keys.get(index).cloned().flatten()
    }

    pub fn mark_failed(&self, index: usize) {
        if index >= self.keys.len() {
            return;
        }
        self.failed
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .insert(index, Instant::now());
    }

    pub fn clear_failed(&self, index: usize) {
        self.failed
            .lock()
            .unwrap_or_else(|error| error.into_inner())
            .remove(&index);
    }

    /// 冷却期内失败 Key 数量（供状态页展示）。
    pub fn failed_count(&self) -> usize {
        let now = Instant::now();
        let failed = self
            .failed
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        failed
            .iter()
            .filter(|(_, failed_at)| now.duration_since(**failed_at) < self.cooldown)
            .count()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pool() -> KeyPool {
        KeyPool::new(
            vec![
                Some("k1".to_owned()),
                Some("k2".to_owned()),
                Some("k3".to_owned()),
            ],
            vec![1, 5, 1],
            vec![Vec::new(), vec!["gpt-4*".to_owned()], Vec::new()],
            Duration::from_secs(60),
        )
    }

    #[test]
    fn weight_ordering_and_model_filter() {
        let pool = pool();
        let candidates = pool.candidates("gpt-4o");
        assert_eq!(candidates, vec![1, 0, 2]);
        let candidates = pool.candidates("claude-opus");
        assert_eq!(candidates, vec![0, 2]);
    }

    #[test]
    fn failed_key_is_skipped_until_cooldown() {
        let pool = pool();
        pool.mark_failed(0);
        let candidates = pool.candidates("claude-opus");
        assert_eq!(candidates, vec![2]);
        assert_eq!(pool.failed_count(), 1);
        pool.clear_failed(0);
        let candidates = pool.candidates("claude-opus");
        assert_eq!(candidates, vec![0, 2]);
    }

    #[test]
    fn keyless_pool_yields_single_candidate() {
        let pool = KeyPool::new(
            vec![None],
            vec![1],
            vec![Vec::new()],
            Duration::from_secs(60),
        );
        assert!(pool.keyless());
        assert_eq!(pool.candidates("anything"), vec![0]);
        assert!(pool.key(0).is_none());
    }
}
