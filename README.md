# AQUA — 多协议 AI API 网关

<div align="center">

**免费、极速、开箱即用的 Cloudflare Workers AI 网关**

统一 OpenAI 兼容协议 · 聚合多家上游 · Rust → Wasm 编译 · 零冷启动

MIT License · Rust · Cloudflare Workers

</div>

---

## 简介

AQUA 是一个部署在 **Cloudflare Workers** 上的多协议 AI API 网关，将 Nvidia NIM、Gitee AI、SiliconFlow、智谱 GLM、讯飞星火、Cloudflare Workers AI 等多家上游统一为 **OpenAI 兼容协议**。一次接入，处处可用——任何支持 OpenAI SDK 的客户端（ChatGPT 客户端、LobeChat、NextChat、Dify 等）都能直接使用。

- **Rust 编译为 WebAssembly**，性能远超 JS 实现，全球边缘节点零冷启动
- **模型名前缀路由**（如 `zhipu/glm-4`、`acu/deepseek-v4-flash`），静态目录兜底自动识别
- **密钥池调度**：Nvidia 上游支持数百密钥自动轮询、限流冷却、健康管理（Durable Object 原子调度）
- **多账号日额度池**：Workers AI 支持多账号分摊日额度（WaiBudget DO 限额）
- **全局并发限流**：acu 通道经 AcuConcurrency DO 中央调控，保护专属上游
- **流式 SSE 完整透传**，Content-Type 正确回传，兼容一切流式客户端

## 项目结构

```
aqua-worker/
├── gateway/          # 网关（Rust → Wasm，核心）
│   ├── src/
│   │   ├── lib.rs        # 路由 / 鉴权 / 供应商配置 / 代理转发
│   │   ├── keypool.rs    # NvKeyPool DO（密钥池调度与健康管理）
│   │   ├── keys.rs       # 环境变量密钥池读取（支持分片合并）
│   │   ├── acu_limit.rs  # AcuConcurrency DO（全局并发限流）
│   │   └── workers_ai.rs # Workers AI 日额度（WaiBudget DO）
│   ├── vars.example.toml # 环境变量样例（复制为 vars.toml 填真实值）
│   └── wrangler.toml     # Workers 配置（KV / D1 / R2 / DO 绑定）
├── frontend/         # 用户前台（Rust Worker + 静态单页）
│   ├── src/lib.rs        # 静态资源服务 + SPA 路由
│   └── public/index.html # 单页前台（模型列表 / API 文档 / 密钥说明）
├── scripts/
│   └── build.ps1     # 构建脚本（cargo + wasm-bindgen + esbuild legacy 流程）
├── .cargo/config.toml # crates.io 镜像加速（可选）
└── package.json      # esbuild 依赖
```

## 功能特性

### 网关端点（OpenAI 兼容）

| 端点 | 方法 | 说明 |
|---|---|---|
| `/v1/models` | GET | 模型列表（实时同步上游可用性） |
| `/v1/chat/completions` | POST | 对话补全（支持 `stream: true` SSE 流式） |
| `/v1/embeddings` | POST | 文本向量化 |
| `/v1/rerank` | POST | 重排序 |
| `/v1/moderations` | POST | 内容审核 |
| `/v1/images/generations` | POST | 图像生成 |
| `/v1/videos/generations` | POST | 视频生成 |
| `/v1/audio/speech` | POST | 语音合成（TTS） |
| `/v1/audio/transcriptions` | POST | 语音识别（ASR，multipart） |
| `/v1/ip_location` | POST | IP 归属地查询 |
| `/assets/*` | GET | R2 生成图片缓存（24h 自动清理） |

### 上游供应商

| 通道 | 模型名前缀 | 说明 |
|---|---|---|
| Nvidia NIM | 无前缀（默认） | 密钥池轮询（NvKeyPool DO），命中限流自动换 key 重试 |
| Gitee AI | `gitee-ai/` | — |
| SiliconFlow | `siliconflow/` | — |
| 智谱 GLM | `zhipu/` | — |
| 讯飞星火 | `spark/` | — |
| Workers AI | `workers-ai/`、`workers-ai-tts/` | Cloudflare 自家 AI，多账号日额度池 |
| 自定义上游 | `acu/` | 直连你自己的专属上游（地址+密钥均由环境变量配置） |

### 工程亮点

- **Durable Object 原子调度**：密钥池、日额度、并发限流全部用 DO 单线程模型实现，天然无竞态
- **模型健康管理**：client_error（400/404）累计封禁问题模型；429/5xx 仅做 key 级冷却，绝不误杀
- **供应商隔离**：封禁/健康状态按通道隔离，一个上游故障不影响其他通道
- **密钥分片**：Nvidia 数百密钥自动按 CF 单变量 5.1KB 上限分片（`NVIDIA_KEYS` + `NVIDIA_KEYS_2..N`），运行时透明合并
- **固定密钥制**：`GATEWAY_KEYS` 支持逗号分隔多把并存，平滑轮换；无效密钥返回 401 中文提示

### 鉴权双模式（AUTH_MODE）

自部署用户可随时在环境变量中切换，改完重新部署即生效：

| AUTH_MODE | 行为 | 适用场景 |
|---|---|---|
| `open`（默认） | 任意非空密钥均可使用（如 `sk-anything`，中英文皆可） | 个人自用 / 公益开放 |
| `key` | 仅 `GATEWAY_KEYS` 中列出的密钥可用，其他密钥返回 401 | 防滥用 / 私有部署 |

```toml
# vars.toml 示例：想开启密钥门槛，改成 key 并配好 GATEWAY_KEYS 即可
AUTH_MODE = "key"
```

## 快速开始

### 1. 前置条件

- [Rust](https://rustup.rs/)（stable，`wasm32-unknown-unknown` target）
- [Node.js](https://nodejs.org/) ≥ 18
- [wrangler](https://developers.cloudflare.com/workers/wrangler/) CLI
- Cloudflare 账户（Workers 免费套餐即可运行）

```bash
rustup target add wasm32-unknown-unknown
npm install -g wrangler
```

### 2. 准备上游密钥

在 `gateway/` 下复制环境变量样例并填入你的真实值：

```bash
cd gateway
cp vars.example.toml vars.toml   # vars.toml 已被 .gitignore 排除，绝不入库
```

编辑 `vars.toml`，至少配置一项上游密钥（如 `NVIDIA_KEYS`）。**没有任何真实密钥会被提交到 Git。**

### 3. 配置 Cloudflare 资源

编辑 `gateway/wrangler.toml`，替换以下占位符为你自己的资源：

| 占位符 | 说明 | 获取方式 |
|---|---|---|
| `REPLACE_WITH_KV_ID` | KV 命名空间 ID | `wrangler kv namespace create MODEL_CACHE` |
| `REPLACE_WITH_D1_ID` | D1 数据库 ID | `wrangler d1 create aqua_logs` |
| `your-gateway-domain.example` | 网关域名 | Cloudflare Dashboard → Workers → 自定义域 |
| `your-frontend-domain.example` | 前台域名 | 同上（frontend/wrangler.toml） |

### 4. 构建

```powershell
npm install
powershell -File scripts/build.ps1 gateway
powershell -File scripts/build.ps1 frontend
```

构建脚本会依次执行：`cargo build`（wasm32）→ `wasm-bindgen`（bundler 模式）→ `esbuild` 打包 legacy shim。

### 5. 部署

```bash
# 方式一：wrangler secret（推荐，密钥存 CF 加密存储，不落任何文件）
wrangler secret put GATEWAY_KEYS
wrangler secret put NVIDIA_KEYS

# 方式二：临时写入 wrangler.toml [env.production.vars] 后部署（记得还原占位符，勿提交）
wrangler deploy --env production
```

### 6. 验证

```bash
curl https://your-gateway-domain.example/v1/models
curl https://your-gateway-domain.example/v1/chat/completions \
  -H "Authorization: Bearer <你的GATEWAY_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"model":"acu/deepseek-v4-flash","messages":[{"role":"user","content":"你好"}]}'
```

## 环境变量

完整清单见 [gateway/vars.example.toml](gateway/vars.example.toml)。常用项：

| 变量 | 必填 | 说明 |
|---|---|---|
| `AUTH_MODE` | 否 | 鉴权模式：`open`（任意密钥可用，默认）/ `key`（指定密钥制，防滥用） |
| `GATEWAY_KEYS` | 否* | 用户调用网关的密钥，逗号分隔支持多把平滑轮换（仅 `AUTH_MODE=key` 时生效） |
| `NVIDIA_KEYS` | 否 | Nvidia 密钥池（逗号分隔，可上百个；支持 `_2.._N` 分片） |
| `GITEE_KEY` / `SILICONFLOW_KEY` / `ZHIPU_KEY` / `SPARK_KEY` | 否 | 各上游密钥，未配置则对应通道 502 |
| `ACU_BASE` / `ACU_KEY` | 否 | 自定义专属上游地址与密钥（未配置则 `acu/*` 502） |
| `WAI_ACCOUNTS` | 否 | Workers AI 多账号池（JSON 数组，含日额度 cap） |
| `ABUSE_*` | 否 | 风控参数（IP/Key 速率、日配额等，按需启用） |

## 隐私与安全

- 仓库内**不含任何真实密钥、上游地址、账户资源 ID**——全部经环境变量或 `wrangler secret` 注入
- `vars.toml`、`*.env`、`wrangler.local.toml` 均已被 `.gitignore` 排除
- 占位符 `REPLACE_WITH_REAL_KEY` 在运行时被视为"未配置"，不会误发请求
- 配置缺失时对应通道返回 502，不泄露任何内部信息

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。

## 致谢

- [worker-rs](https://github.com/cloudflare/workers-rs) — Cloudflare Workers Rust SDK
- 感谢所有上游供应商提供的免费/开放 API 额度
