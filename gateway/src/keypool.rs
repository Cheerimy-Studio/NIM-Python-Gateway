//! NvKeyPool Durable Object：Nvidia 密钥池的中央调度（健康记忆 + 轮询 + 冷却/封禁）
//!
//! 状态机：
//! - ACTIVE   ：健康，可参与轮询（每 key 每分钟限 38 次，避免触发上游 429）
//! - COOLDOWN ：收到 429/限流，冷却 60s 后自动回 ACTIVE
//! - DEAD     ：401/403 连续失败 ≥3 次，隔离 12h 后自动复活重测
//! - 模型级封禁：某个模型被 ≥3 个不同 key 拒绝 → 该模型封禁 10 分钟（避免空转重试）

use std::collections::HashMap;
use serde::{Deserialize, Serialize};
use worker::*;

/// 每个 key 每分钟最多请求次数（上游 Nvidia 免费 key 的限制）
const RATE_PER_MIN: u32 = 38;
/// 429 冷却时长（秒）
const COOLDOWN_SECS: i64 = 60;
/// 连续失败多少次判定 DEAD
const DEAD_THRESHOLD: u32 = 3;
/// DEAD 隔离时长（秒）
const DEAD_SECS: i64 = 12 * 3600;
/// 模型被 N 个 key 拒绝后封禁
const MODEL_BLOCK_THRESHOLD: u32 = 3;
/// 模型封禁时长（秒）
const MODEL_BLOCK_SECS: i64 = 600;

#[derive(Serialize, Deserialize, Default, Clone)]
pub struct KeyPoolState {
    /// 每个 key 当前分钟窗口内的请求计数（与 window_min 配套）
    pub counts: Vec<u32>,
    /// 当前窗口的分钟桶（unix ts / 60）
    pub window_min: i64,
    /// 每个 key 的冷却截止时间（unix 秒），429 时设置
    pub cooldown_until: Vec<i64>,
    /// 每个 key 的封禁截止时间（unix 秒），401/403 累计失败后设置
    pub dead_until: Vec<i64>,
    /// 每个 key 的连续失败次数
    pub fails: Vec<u32>,
    /// 轮询游标（round-robin）
    pub cursor: usize,
    /// 模型级错误计数
    pub model_errors: HashMap<String, u32>,
    /// 模型级封禁截止时间
    pub model_blocked_until: HashMap<String, i64>,
    /// 本分钟已派发总数
    pub busy_this_min: u32,
    /// 当前并发（估算）
    pub busy: u32,
}

impl KeyPoolState {
    fn ensure_len(&mut self, n: usize) {
        self.counts.resize(n, 0);
        self.cooldown_until.resize(n, 0);
        self.dead_until.resize(n, 0);
        self.fails.resize(n, 0);
    }
}

#[durable_object]
pub struct NvKeyPool {
    state: State,
    env: Env,
    /// 运行时密钥池（来自 env NVIDIA_KEYS，逗号分隔；DO 实例常驻，构造时读一次）
    keys: Vec<String>,
}

impl NvKeyPool {
    fn now(&self) -> i64 {
        js_sys::Date::new_0().get_time() as i64 / 1000
    }

    async fn load_state(&self) -> KeyPoolState {
        let mut s: KeyPoolState = self
            .state
            .storage()
            .get("state")
            .await
            .ok()
            .flatten()
            .unwrap_or_default();
        let n = self.keys.len();
        s.ensure_len(n);
        // 跨分钟自动重置计数窗口
        let bucket = self.now() / 60;
        if s.window_min != bucket {
            s.window_min = bucket;
            for c in s.counts.iter_mut() {
                *c = 0;
            }
            s.busy_this_min = 0;
        }
        s
    }

    async fn save_state(&self, s: &KeyPoolState) {
        let _ = self.state.storage().put("state", s).await;
    }

    /// 选一个健康 key；全池不可用返回 None
    fn pick_active_idx(&self, s: &KeyPoolState, n: usize) -> Option<usize> {
        let now = self.now();
        // 从游标起最多扫两圈
        for step in 0..n {
            let i = (s.cursor + step) % n;
            if s.dead_until[i] > now {
                continue;
            }
            if s.cooldown_until[i] > now {
                continue;
            }
            if s.counts[i] >= RATE_PER_MIN {
                continue;
            }
            return Some(i);
        }
        None
    }
}

impl DurableObject for NvKeyPool {
    fn new(state: State, env: Env) -> Self {
        let keys = crate::keys::key_pool_from_env(&env);
        Self { state, env, keys }
    }

    async fn fetch(&self, mut req: Request) -> Result<Response> {
        let n = self.keys.len();
        let body: serde_json::Value = req.json().await.unwrap_or(serde_json::json!({}));
        let cmd = body.get("cmd").and_then(|v| v.as_str()).unwrap_or("");
        let mut st = self.load_state().await;
        match cmd {
            // 选 key
            "pick" => {
                let model = body.get("model").and_then(|v| v.as_str()).unwrap_or("");
                let now = self.now();
                if !model.is_empty() {
                    if let Some(until) = st.model_blocked_until.get(model) {
                        if *until > now {
                            let remain = *until - now;
                            return Response::from_json(&serde_json::json!({
                                "error": "model_blocked",
                                "retry_after": remain,
                            }));
                        } else {
                            st.model_blocked_until.remove(model);
                        }
                    }
                }
                match self.pick_active_idx(&st, n) {
                    Some(i) => {
                        st.counts[i] += 1;
                        st.busy_this_min += 1;
                        st.busy += 1;
                        st.cursor = (i + 1) % n;
                        self.save_state(&st).await;
                        Response::from_json(&serde_json::json!({
                            "key_idx": i,
                            "key": self.keys[i],
                            "busy": st.busy,
                        }))
                    }
                    None => Response::from_json(&serde_json::json!({
                        "error": "all_keys_busy",
                        "message": "all keys busy",
                    })),
                }
            }
            // 回写结果
            "report" => {
                let model = body.get("model").and_then(|v| v.as_str()).unwrap_or("");
                let idx = body.get("key_idx").and_then(|v| v.as_u64()).unwrap_or(0) as usize;
                let ok = body.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
                let err = body.get("err_type").and_then(|v| v.as_str()).unwrap_or("");
                let now = self.now();
                if idx < n {
                    if ok {
                        st.fails[idx] = 0;
                        // 成功即清零该模型的失败计数（避免历史瞬时错误累积误封）
                        if !model.is_empty() {
                            st.model_errors.remove(model);
                        }
                    } else {
                        st.fails[idx] += 1;
                        match err {
                            "rate_limited" => {
                                st.cooldown_until[idx] = now + COOLDOWN_SECS;
                            }
                            "client_error" => {
                                if st.fails[idx] >= DEAD_THRESHOLD {
                                    st.dead_until[idx] = now + DEAD_SECS;
                                    st.fails[idx] = 0;
                                }
                                // 模型级封禁：仅当模型被上游明确拒绝（400/404 等 client_error，
                                // 即模型不存在/无访问权限）才计入封禁。
                                // 429 限流 / 5xx / 网络抖动均为瞬时问题，不得误杀健康模型
                                //（否则 3 个 key 同时 429 就会把 ok=99% 的模型临时下线）。
                                if !model.is_empty() {
                                    let cnt = st.model_errors.entry(model.to_string()).or_insert(0);
                                    *cnt += 1;
                                    if *cnt >= MODEL_BLOCK_THRESHOLD {
                                        st.model_blocked_until
                                            .insert(model.to_string(), now + MODEL_BLOCK_SECS);
                                        st.model_errors.remove(model);
                                    }
                                }
                            }
                            _ => {
                                // upstream_error / timeout / network_error：轻微惩罚 5s
                                if st.cooldown_until[idx] < now + 5 {
                                    st.cooldown_until[idx] = now + 5;
                                }
                            }
                        }
                        if st.busy > 0 {
                            st.busy -= 1;
                        }
                    }
                }
                self.save_state(&st).await;
                Response::from_json(&serde_json::json!({"ok": true}))
            }
            // 已封禁模型（供 /v1/models 展示 status）
            "blocked" => {
                let now = self.now();
                let blocked: Vec<serde_json::Value> = st
                    .model_blocked_until
                    .iter()
                    .filter(|(_, until)| **until > now)
                    .map(|(m, until)| {
                        serde_json::json!({"model": m, "until": until, "retry_after": *until - now})
                    })
                    .collect();
                Response::from_json(&serde_json::json!({"blocked": blocked}))
            }
            // 池子统计
            "status" => {
                let now = self.now();
                let active = st
                    .counts
                    .iter()
                    .enumerate()
                    .filter(|(i, _)| st.dead_until[*i] <= now && st.cooldown_until[*i] <= now)
                    .count();
                Response::from_json(&serde_json::json!({
                    "total": n,
                    "active": active,
                    "busy": st.busy,
                    "busy_this_min": st.busy_this_min,
                    "window_min": st.window_min,
                }))
            }
            _ => Response::from_json(&serde_json::json!({"error": "unknown_cmd"})),
        }
    }

    async fn alarm(&self) -> Result<Response> {
        // 周期清理：重置窗口、移除过期的冷却/封禁状态
        let now = self.now();
        let mut st = self.load_state().await;
        let n = self.keys.len();
        for i in 0..n {
            if st.dead_until[i] <= now {
                st.dead_until[i] = 0;
            }
            if st.cooldown_until[i] <= now {
                st.cooldown_until[i] = 0;
            }
        }
        st.model_blocked_until.retain(|_, until| *until > now);
        st.model_errors.clear();
        st.busy = 0;
        self.save_state(&st).await;
        let _ = self
            .state
            .storage()
            .set_alarm((now as i64) + 3600 * 1000)
            .await;
        Response::empty()
    }
}
