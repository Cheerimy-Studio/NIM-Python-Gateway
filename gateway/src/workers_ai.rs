//! WaiBudget Durable Object：Workers AI 每日用量绝对限额的原子计数
//!
//! 多个 Cloudflare 账号的免费额度每日（00:00 UTC）重置，本 DO 用单一状态机
//! 做"日额度账本"：round-robin 挑选还有余量的账号，超额即返回 quota_exhausted。

use serde::{Deserialize, Serialize};
use worker::*;

#[derive(Serialize, Deserialize, Clone)]
pub struct WaiAccount {
    pub name: String,
    pub account_id: String,
    pub token: String,
    /// 每日限额（神经元）
    pub cap: f64,
}

#[derive(Serialize, Deserialize, Clone)]
pub struct WaiBudgetState {
    /// 账本日期 YYYY-MM-DD（跨天自动清零）
    pub date: String,
    /// 每个账号今日已用
    pub used: Vec<f64>,
    /// 轮询游标
    pub rot: usize,
}

impl Default for WaiBudgetState {
    fn default() -> Self {
        Self {
            date: String::new(),
            used: Vec::new(),
            rot: 0,
        }
    }
}

#[durable_object]
pub struct WaiBudget {
    state: State,
    env: Env,
}

impl WaiBudget {
    fn today_utc(&self) -> String {
        let d = js_sys::Date::new_0();
        let year = d.get_utc_full_year();
        let month = d.get_utc_month() + 1;
        let day = d.get_utc_date();
        format!("{:04}-{:02}-{:02}", year, month, day)
    }

    async fn load(&self, accounts: &[WaiAccount]) -> WaiBudgetState {
        let mut st: WaiBudgetState = self.state.storage().get("state").await.ok().flatten().unwrap_or_default();
        let today = self.today_utc();
        if st.date != today {
            st.date = today;
            st.used = vec![0.0; accounts.len()];
            st.rot = 0;
        }
        st.used.resize(accounts.len(), 0.0);
        st
    }

    async fn save(&self, st: &WaiBudgetState) {
        let _ = self.state.storage().put("state", st).await;
    }

    fn accounts(&self) -> Vec<WaiAccount> {
        if let Ok(raw) = self.env.var("WAI_ACCOUNTS") {
            if let Ok(arr) = serde_json::from_str::<Vec<WaiAccount>>(&raw.to_string()) {
                if !arr.is_empty() {
                    return arr;
                }
            }
        }
        // 兜底：单个默认账号（读取 WAI_ACCOUNT_ID / WAI_API_TOKEN）
        let account_id = self
            .env
            .var("WAI_ACCOUNT_ID")
            .map(|v| v.to_string())
            .unwrap_or_default();
        let token = self
            .env
            .var("WAI_API_TOKEN")
            .map(|v| v.to_string())
            .unwrap_or_default();
        if account_id.is_empty() || token.is_empty() {
            return Vec::new();
        }
        vec![WaiAccount {
            name: "default".into(),
            account_id,
            token,
            cap: self
                .env
                .var("WAI_CAP_GLOBAL")
                .ok()
                .and_then(|v| v.to_string().parse().ok())
                .unwrap_or(10000.0),
        }]
    }
}

impl DurableObject for WaiBudget {
    fn new(state: State, env: Env) -> Self {
        Self { state, env }
    }

    async fn fetch(&self, mut req: Request) -> Result<Response> {
        let accounts = self.accounts();
        let body: serde_json::Value = req.json().await.unwrap_or(serde_json::json!({}));
        let cmd = body.get("cmd").and_then(|v| v.as_str()).unwrap_or("");
        let mut st = self.load(&accounts).await;

        match cmd {
            // 预估扣减（请求前检查额度；超限返回 quota_exhausted）
            "check" => {
                let amount = body.get("amount").and_then(|v| v.as_f64()).unwrap_or(1.0);
                if accounts.is_empty() {
                    return Response::from_json(&serde_json::json!({
                        "allowed": false,
                        "error": "no_wai_accounts",
                    }));
                }
                let mut picked: Option<usize> = None;
                for step in 0..accounts.len() {
                    let i = (st.rot + step) % accounts.len();
                    if st.used[i] + amount <= accounts[i].cap {
                        picked = Some(i);
                        break;
                    }
                }
                match picked {
                    Some(i) => {
                        st.used[i] += amount;
                        st.rot = (i + 1) % accounts.len();
                        self.save(&st).await;
                        let acc = &accounts[i];
                        Response::from_json(&serde_json::json!({
                            "allowed": true,
                            "idx": i,
                            "name": acc.name,
                            "account_id": acc.account_id,
                            "token": acc.token,
                            "used": st.used[i],
                            "cap": acc.cap,
                            "remaining": acc.cap - st.used[i],
                        }))
                    }
                    None => {
                        let exhausted: Vec<serde_json::Value> = accounts
                            .iter()
                            .enumerate()
                            .map(|(i, a)| {
                                serde_json::json!({
                                    "name": a.name,
                                    "used": st.used[i],
                                    "cap": a.cap,
                                    "remaining": a.cap - st.used[i],
                                    "state": "exhausted",
                                })
                            })
                            .collect();
                        Response::from_json(&serde_json::json!({
                            "allowed": false,
                            "error": "quota_exhausted",
                            "date": st.date,
                            "accounts": exhausted,
                        }))
                    }
                }
            }
            // 状态查询（供 /v1/models 展示 status=exhausted）
            "status" => {
                let exhausted: Vec<serde_json::Value> = accounts
                    .iter()
                    .enumerate()
                    .map(|(i, a)| {
                        serde_json::json!({
                            "name": a.name,
                            "used": st.used[i],
                            "cap": a.cap,
                            "remaining": a.cap - st.used[i],
                            "state": if st.used[i] >= a.cap { "exhausted" } else { "ok" },
                        })
                    })
                    .collect();
                let all_exhausted = !accounts.is_empty()
                    && accounts.iter().enumerate().all(|(i, a)| st.used[i] >= a.cap);
                Response::from_json(&serde_json::json!({
                    "date": st.date,
                    "exhausted": all_exhausted,
                    "accounts": exhausted,
                }))
            }
            _ => Response::from_json(&serde_json::json!({"error": "unknown_cmd"})),
        }
    }

    async fn alarm(&self) -> Result<Response> {
        // 跨天清理：由 load() 的日期比对自动完成，此处仅续期定时器
        let _ = self.state.storage().set_alarm(3600 * 1000_i64).await;
        Response::empty()
    }
}
