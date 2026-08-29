# gateway/ — 网关核心（Rust → Wasm）

这是 AQUA 的**核心**：一个跑在 Cloudflare Workers 上的 AI API 网关，把多家上游聚合成 OpenAI 兼容接口。

## 文件导览

| 文件 | 作用 |
|---|---|
| `src/lib.rs` | 入口与主干：路由注册、鉴权（AUTH_MODE 双模式）、供应商配置（全部读环境变量）、各通道代理转发、模型目录 |
| `src/keypool.rs` | `NvKeyPool` Durable Object：Nvidia 密钥池调度（随机轮询、38 次/分限速、限流冷却、失效隔离、模型封禁管理） |
| `src/keys.rs` | 从环境变量读取密钥池：支持 `NVIDIA_KEYS` + `NVIDIA_KEYS_2..N` 分片自动合并（绕过 CF 单变量 5.1KB 上限） |
| `src/acu_limit.rs` | `AcuConcurrency` Durable Object：自定义上游（acu/*）的全局并发闸，并发满时排队最长 10s |
| `src/workers_ai.rs` | `WaiBudget` Durable Object：Workers AI 多账号日额度原子计数，超限自动切号 |
| `vars.example.toml` | **环境变量样例**——复制为 `vars.toml` 并填入你的真实密钥（`vars.toml` 不入库） |
| `wrangler.toml` | Workers 配置：DO/KV/D1/R2 绑定、构建命令、域名路由（部署前替换占位符） |
| `Cargo.toml` | Rust 依赖（worker 0.8 + serde 等） |

## 新手三步上手

1. `cp vars.example.toml vars.toml`，填入你自己的上游密钥（有几项填几项）
2. 编辑 `wrangler.toml`，把 `REPLACE_WITH_KV_ID` / `REPLACE_WITH_D1_ID` / 域名占位符换成你自己的
3. 回到仓库根目录执行 `powershell -File scripts/build.ps1 gateway`，然后 `cd gateway && wrangler deploy --env production`

## 关键概念

- **模型前缀路由**：`zhipu/glm-4-flash` 走智谱，`acu/xxx` 走你的自定义上游，无前缀走 Nvidia 池
- **供应商隔离**：任何上游的故障/封禁只影响自己通道，绝不波及其他通道
- **密钥零入库**：所有密钥只存在于环境变量或 secret，代码仓库里只有占位符
