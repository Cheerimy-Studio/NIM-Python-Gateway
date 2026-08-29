# AQUA — 多协议 AI API 网关

<div align="center">

**免费 · 极速 · 免注册的 OpenAI 兼容 AI 网关**

Rust → WebAssembly · Cloudflare Workers 边缘运行 · 多上游聚合 · 任意密钥即用

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Rust](https://img.shields.io/badge/Rust-stable-orange.svg)](https://rustup.rs/)
[![Cloudflare Workers](https://img.shields.io/badge/Cloudflare-Workers-f38020.svg)](https://workers.cloudflare.com/)

</div>

---

## 这是什么

AQUA 把 **Nvidia NIM、Gitee AI、SiliconFlow、智谱 GLM、讯飞星火、Cloudflare Workers AI** 等多家 AI 上游，聚合成一套 **OpenAI 兼容 API**。用任何支持 OpenAI SDK 的客户端（ChatGPT 客户端、LobeChat、NextChat、Dify、沉浸式翻译等），填一个 Base URL 就能用上全线模型。

网关本体用 **Rust 编译为 WebAssembly**，跑在 Cloudflare 边缘节点上：全球就近接入、无冷启动、免费套餐即可运行。

**任意密钥即可调用**——不需要注册、不需要申请，填任意非空 Key（`sk-****`、甚至中文）就能用。

## 模型通道

| 通道 | 模型名前缀 | 说明 |
|---|---|---|
| Nvidia NIM | 无前缀 / `nvidia/` | 默认通道；数百密钥池轮询，限流自动换 Key 重试 |
| Gitee AI | `gitee-ai/` | 含 IP 归属地查询等特色端点 |
| SiliconFlow | `siliconflow/` | — |
| 智谱 GLM | `zhipu/` | 含 CogView 绘图、CogVideo 视频 |
| 讯飞星火 | `spark/` | — |
| Workers AI | `workers-ai/`、`workers-ai-tts/` | Cloudflare 自家 AI，多账号日额度池 |
| 自定义上游 | `acu/` | 直连部署者自己的专属上游（地址+密钥均由环境变量配置） |

模型名带前缀自动路由到对应上游；不带前缀的走静态模型目录识别，未识别的兜底到 Nvidia。

## API 端点（OpenAI 兼容）

| 端点 | 方法 | 说明 |
|---|---|---|
| `/v1/models` | GET | 模型列表（公开访问，实时反映上游可用性） |
| `/v1/chat/completions` | POST | 对话补全，`stream: true` 时 SSE 流式透传 |
| `/v1/embeddings` | POST | 文本向量化 |
| `/v1/rerank` | POST | 重排序 |
| `/v1/moderations` | POST | 内容审核 |
| `/v1/images/generations` | POST | 图像生成 |
| `/v1/videos/generations` | POST | 视频生成 |
| `/v1/audio/speech` | POST | 语音合成 TTS |
| `/v1/audio/transcriptions` | POST | 语音识别 ASR（multipart 上传） |
| `/v1/ip_location` | POST | IP 归属地查询 |
| `/assets/*` | GET | 生成图片的 R2 缓存（24h 自动清理） |

所有错误响应为 OpenAI 兼容结构，并附带 `help` 字段（官网、QQ 频道/群引导），方便客户端直接展示排障信息。

## 工程实现

三个 **Durable Object** 负责有状态调度（单线程模型，天然无竞态）：

- **NvKeyPool** — Nvidia 密钥池：随机轮询、每 Key 每分钟 38 次限速（避开上游 429）、限流冷却、失效隔离；模型封禁只针对 client_error（400/404），5xx/429 仅冷却 Key 不误杀模型
- **WaiBudget** — Workers AI 日额度：多账号 JSON 池按容量分摊，原子计数，超限自动切下一账号
- **AcuConcurrency** — 自定义上游全局并发闸：并发满时排队等待（最长 10s），保护后端

其他特性：

- **供应商隔离**：封禁/健康状态按通道隔离，一个上游故障不影响其他通道
- **密钥分片合并**：Cloudflare 单环境变量上限 5.1KB，`NVIDIA_KEYS` 支持自动切分为 `NVIDIA_KEYS_2..N`，运行时透明合并成完整池
- **鉴权双模式**：`AUTH_MODE` 一键切换开放/密钥制（见下）

## 鉴权双模式

| AUTH_MODE | 行为 | 适用场景 |
|---|---|---|
| `open`（默认，未配置时） | 任意非空密钥均可使用，中英文皆可 | 公益开放 / 个人自用 |
| `key` | 仅 `GATEWAY_KEYS` 列表中的密钥可用，其余返回 401 | 防滥用 / 私有部署 |

401 响应会附官网与 QQ 频道/群引导字段，客户端可直接取用展示。

## 快速部署

### 前置条件

- Rust stable + `wasm32-unknown-unknown` target（`rustup target add wasm32-unknown-unknown`）
- Node.js ≥ 18 + `npm install`
- wrangler CLI（`npm install -g wrangler`）并 `wrangler login`

### 1. 配置上游密钥

```bash
cd gateway
cp vars.example.toml vars.toml   # vars.toml 已被 .gitignore 排除，绝不入库
```

编辑 `vars.toml`，填入你自己的上游密钥（Nvidia / Gitee / SiliconFlow / 智谱 / 星火 / 自定义上游，有几项填几项，没配的通道自动禁用）。

### 2. 替换 Cloudflare 资源

编辑 `gateway/wrangler.toml`，替换为你自己的资源 ID 与域名：

| 占位符 | 获取方式 |
|---|---|
| `REPLACE_WITH_KV_ID` | `wrangler kv namespace create MODEL_CACHE` |
| `REPLACE_WITH_D1_ID` | `wrangler d1 create aqua_logs` |
| `your-gateway-domain.example` | 你的网关域名（frontend/wrangler.toml 同理） |

### 3. 构建

```powershell
powershell -File scripts/build.ps1 gateway    # 构建网关
powershell -File scripts/build.ps1 frontend   # 构建前台
```

流程：cargo 编译 wasm32 → wasm-bindgen 生成胶水 → esbuild 打包 legacy shim。

### 4. 注入密钥并部署

```bash
# 方式一：wrangler secret（推荐，密钥存加密存储不落文件）
wrangler secret put NVIDIA_KEYS
wrangler secret put AUTH_MODE

# 方式二：写入 wrangler.toml [env.production.vars] 后部署（勿提交该文件）
wrangler deploy --env production
```

### 5. 验证

```bash
curl https://your-gateway-domain.example/v1/models
curl https://your-gateway-domain.example/v1/chat/completions \
  -H "Authorization: Bearer whatever-you-like" \
  -H "Content-Type: application/json" \
  -d '{"model":"acu/deepseek-v4-flash","messages":[{"role":"user","content":"你好"}]}'
```

## 环境变量

完整样例见 [gateway/vars.example.toml](gateway/vars.example.toml)：

| 变量 | 默认 | 说明 |
|---|---|---|
| `AUTH_MODE` | `open` | `open` 任意密钥可用；`key` 指定密钥制 |
| `GATEWAY_KEYS` | — | `AUTH_MODE=key` 时的合法密钥列表，逗号分隔多把平滑轮换 |
| `NVIDIA_KEYS` | — | Nvidia 密钥池（逗号分隔可上百个；支持 `_2.._N` 分片） |
| `NVIDIA_BASE` 等 `*_BASE` | 官方地址 | 各上游 Base URL，一般不用改 |
| `GITEE_KEY` / `SILICONFLOW_KEY` / `ZHIPU_KEY` / `SPARK_KEY` | — | 对应上游密钥，未配置则该通道 502 |
| `ACU_BASE` / `ACU_KEY` | — | 自定义专属上游地址与密钥 |
| `WAI_ACCOUNTS` | — | Workers AI 多账号池，JSON 数组 `[{"name","account_id","token","cap"}]` |
| `WAI_ACCOUNT_ID` / `WAI_API_TOKEN` / `WAI_CAP_GLOBAL` | — | Workers AI 单账号兜底配置 |

## 项目结构

```
aqua-worker/
├── gateway/                 # 网关（Rust → Wasm）
│   ├── src/
│   │   ├── lib.rs           # 入口：路由 / 鉴权 / 供应商配置 / 代理转发
│   │   ├── keypool.rs       # NvKeyPool DO：Nvidia 密钥池调度
│   │   ├── keys.rs          # 环境变量密钥池读取（分片合并）
│   │   ├── acu_limit.rs     # AcuConcurrency DO：全局并发闸
│   │   └── workers_ai.rs    # WaiBudget DO：Workers AI 日额度
│   ├── vars.example.toml    # 环境变量样例（真实值不入库）
│   └── wrangler.toml        # Workers 配置（KV / D1 / R2 / DO）
├── frontend/                # 用户前台（Rust Worker）
│   ├── src/lib.rs           # 静态资源 + SPA 路由
│   └── public/index.html    # 单页：首页 / 模型列表 / 能力矩阵 / API 文档
├── scripts/
│   ├── build.ps1            # 构建脚本（cargo → wasm-bindgen → esbuild）
│   └── shim.legacy.template.js
└── .cargo/config.toml       # crates.io 镜像加速（可选）
```

## 隐私与安全

- 仓库**不含任何真实密钥、上游地址、Cloudflare 资源 ID**，全部经环境变量或 secret 注入
- `vars.toml`、`wrangler.local.toml`、`*.env` 均被 `.gitignore` 排除
- 未配置的上游通道返回 502 且不泄露任何内部信息
- 构建产物（`build/`）不入库

## 社区

- **QQ 频道（官方主阵地）**：大版本更新与重要公告均在此通知 → [点击加入](https://pd.qq.com/s/e4ktxw1b8)（频道号 `pd57362562`）
- **QQ 群（休闲交流）**：日常闲聊、技术交流 → 群号 `1103667832`

## 协议

[MIT License](LICENSE)
