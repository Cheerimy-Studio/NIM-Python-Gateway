//! Nvidia 密钥池来源（开源友好版）
//!
//! 真实密钥不进代码仓库：全部经环境变量注入（逗号分隔）。
//! 由于 Cloudflare vars 单值上限 5.1KB，支持分片变量自动合并：
//!   NVIDIA_KEYS, NVIDIA_KEYS_2, NVIDIA_KEYS_3, ... （按序拼接，每片独立 ≤5KB）
//! 开源仓库中不含任何密钥；部署方在本地配置文件（不入 Git）或 Secret 中自行配置。

const CHUNK_LIMIT: usize = 4800; // 留 6% 余量，低于 CF 5.1KB 限制

/// 从环境变量读取密钥池（自动合并 NVIDIA_KEYS 与 NVIDIA_KEYS_2..N 分片）；未配置返回空列表
pub fn key_pool_from_env(env: &worker::Env) -> Vec<String> {
    // 收集所有分片：(片号0=主变量, 1..=N)
    let mut parts: Vec<(usize, String)> = Vec::new();
    if let Ok(v) = env.var("NVIDIA_KEYS") {
        let s = v.to_string();
        if !s.trim().is_empty() && s.trim() != "REPLACE_WITH_REAL_KEY" {
            parts.push((0, s));
        }
    }
    for i in 2..=40 {
        let name = format!("NVIDIA_KEYS_{i}");
        let Ok(v) = env.var(&name) else { break };
        let s = v.to_string();
        if s.trim().is_empty() || s.trim() == "REPLACE_WITH_REAL_KEY" {
            break;
        }
        parts.push((i - 1, s));
    }
    parts.sort_by_key(|(idx, _)| *idx);
    let joined = parts
        .into_iter()
        .map(|(_, s)| s.trim().trim_end_matches(',').to_string())
        .collect::<Vec<_>>()
        .join(",");

    joined
        .split(',')
        .map(|k| k.trim().to_string())
        .filter(|k| !k.is_empty())
        .collect()
}

/// 部署辅助：把逗号分隔长串切成 ≤limit 的片（用于规避 CF 单变量 5.1KB 上限）
pub fn split_hint(joined: &str, limit: usize) -> Vec<String> {
    let mut out = Vec::new();
    let mut cur = String::new();
    for k in joined.split(',') {
        let k = k.trim();
        if k.is_empty() {
            continue;
        }
        if !cur.is_empty() && cur.len() + 1 + k.len() > limit {
            out.push(cur);
            cur = String::new();
        }
        if !cur.is_empty() {
            cur.push(',');
        }
        cur.push_str(k);
    }
    if !cur.is_empty() {
        out.push(cur);
    }
    out
}

/// 默认分片上限（供外部工具对齐）
pub const SPLIT_LIMIT: usize = CHUNK_LIMIT;
