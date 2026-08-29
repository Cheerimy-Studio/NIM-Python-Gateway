//! AcuConcurrency Durable Object：acu/* 通道模型的全局并发限流
//!
//! 中央调控：同一时间只允许 N 个 acu/ 请求进入上游，超出排队（返回建议
//! 等待毫秒数，网关侧 sleep 后重试），避免免费上游把整池 key 限流。

use serde::{Deserialize, Serialize};
use worker::*;

#[derive(Serialize, Deserialize, Default, Clone)]
pub struct AcuState {
    /// 当前占用并发槽位
    pub active: u32,
    /// 排队中请求数（估算）
    pub waiting: u32,
    /// 排队游标/已派发序号
    pub ticket: u64,
}

#[durable_object]
pub struct AcuConcurrency {
    state: State,
    env: Env,
}

impl AcuConcurrency {
    fn max_concurrent(&self) -> u32 {
        self.env
            .var("ACU_MAX_CONCURRENT")
            .ok()
            .and_then(|v| v.to_string().parse().ok())
            .unwrap_or(8)
    }

    async fn load(&self) -> AcuState {
        self.state.storage().get("state").await.ok().flatten().unwrap_or_default()
    }

    async fn save(&self, st: &AcuState) {
        let _ = self.state.storage().put("state", st).await;
    }
}

impl DurableObject for AcuConcurrency {
    fn new(state: State, env: Env) -> Self {
        Self { state, env }
    }

    async fn fetch(&self, mut req: Request) -> Result<Response> {
        let max = self.max_concurrent();
        let body: serde_json::Value = req.json().await.unwrap_or(serde_json::json!({}));
        let cmd = body.get("cmd").and_then(|v| v.as_str()).unwrap_or("");
        let mut st = self.load().await;

        match cmd {
            // 尝试占用一个并发槽位
            "acquire" => {
                if st.active < max {
                    st.active += 1;
                    self.save(&st).await;
                    Response::from_json(&serde_json::json!({
                        "granted": true,
                        "active": st.active,
                        "max": max,
                    }))
                } else {
                    st.waiting += 1;
                    st.ticket += 1;
                    let my_ticket = st.ticket;
                    // 估算等待时长：前面每个请求按 ~2s 计
                    let wait_ms = (st.waiting as u64) * 2000;
                    self.save(&st).await;
                    Response::from_json(&serde_json::json!({
                        "granted": false,
                        "active": st.active,
                        "max": max,
                        "waiting": st.waiting,
                        "ticket": my_ticket,
                        "wait_ms": wait_ms,
                    }))
                }
            }
            // 释放并发槽位（请求结束无论成败）
            "release" => {
                if st.active > 0 {
                    st.active -= 1;
                }
                if st.waiting > 0 {
                    st.waiting -= 1;
                }
                self.save(&st).await;
                Response::from_json(&serde_json::json!({
                    "ok": true,
                    "active": st.active,
                    "max": max,
                }))
            }
            // 状态查询
            "status" => Response::from_json(&serde_json::json!({
                "active": st.active,
                "max": max,
                "waiting": st.waiting,
            })),
            _ => Response::from_json(&serde_json::json!({"error": "unknown_cmd"})),
        }
    }

    async fn alarm(&self) -> Result<Response> {
        // 周期性复位过期的等待计数
        let mut st = self.load().await;
        st.waiting = 0;
        self.save(&st).await;
        let _ = self.state.storage().set_alarm(60 * 1000_i64).await;
        Response::empty()
    }
}
