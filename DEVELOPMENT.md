# AQUA 二次开发指南（DEVELOPMENT.md）

> 本文档面向想要**二次开发、二次创建部署、扩展功能**的开发者。
> 快速部署请看 [README.md](README.md)；本文聚焦：代码怎么读、怎么改、怎么加东西、怎么排查。

---

## 目录

1. [整体架构一图流](#1-整体架构一图流)
2. [目录结构精讲](#2-目录结构精讲)
3. [网关核心代码导读（lib.rs）](#3-网关核心代码导读librs)
4. [前端代码导读（index.html）](#4-前端代码导读indexhtml)
5. [常见扩展任务实操](#5-常见扩展任务实操)
   - 5.1 [新增一个模型](#51-新增一个模型)
   - 5.2 [新增一个上游供应商](#52-新增一个上游供应商)
   - 5.3 [新增一个工具 API（网关侧）](#53-新增一个工具-api网关侧)
   - 5.4 [新增一个网页工具/游戏（前端侧）](#54-新增一个网页工具游戏前端侧)
   - 5.5 [修改鉴权行为](#55-修改鉴权行为)
6. [本地开发与调试流程](#6-本地开发与调试流程)
7. [部署流水线详解](#7-部署流水线详解)
8. [错误码与排障手册](#8-错误码与排障手册)
9. [二次部署检查清单](#9-二次部署检查清单)

---

## 1. 整体架构一图流

```
 用户 / OpenAI SDK 客户端
        │  任意密钥（open 模式）
        ▼
┌─────────────────────────────────────────────┐
│  前台 Worker (frontend)                      │
│  · 单文件 SPA（public/index.html）            │
│  · 纯静态，模型列表由 JS 调网关获取             │
└──────────────────┬──────────────────────────┘
                   │  GATEWAY 自动探测（acu. → api.）
                   ▼
┌─────────────────────────────────────────────┐
│  网关 Worker (gateway, Rust → Wasm)          │
│                                              │
│  main() ─► CORS/鉴权 ─► Router ─► handler    │
│                              │               │
│   ┌──────────────────────────┼──────────┐    │
│   ▼                ▼         ▼          ▼    │
│ NvKeyPool DO   WaiBudget DO  AcuLimit DO   │
│ (密钥池轮询)    (日额度账本)   (并发闸)        │
│   │                │         │              │
│   ▼                ▼         ▼              │
│  Nvidia        Workers AI   自定义上游        │
│   │                                                │
│   ├── Gitee AI / SiliconFlow / 智谱 / 星火（直连固定密钥）
│   ├── D1 (LOGS_DB)：请求日志 + 模型健康评分
│   ├── KV (MODEL_CACHE)：模型列表 30s 缓存
│   └── R2 (IMAGES_BUCKET)：生成图片缓存
└─────────────────────────────────────────────┘
```

**核心设计原则**

| 原则 | 体现 |
|---|---|
| 零隐私 | 代码里没有任何真实密钥/域名/资源 ID，全部环境变量注入 |
| 供应商隔离 | 每个上游独立配置，一个挂了不影响其他通道 |
| 有状态调度用 DO | Durable Object 单线程模型，天然无竞态，不用锁 |
| 无状态转发用 Worker | 纯转发逻辑毫秒级，冷启动零 |
| 错误可读化 | 所有错误返回中文 message + help 引导字段 |

---

## 2. 目录结构精讲

```
aqua-worker/
├── gateway/                        # ★ 网关核心
│   └── src/
│       ├── lib.rs                  #   主文件：路由/鉴权/转发/工具 API/健康评分（~1500 行）
│       ├── keypool.rs              #   NvKeyPool DO：Nvidia 密钥池状态机
│       ├── keys.rs                 #   NVIDIA_KEYS 分片合并（绕过 CF 5.1KB 限制）
│       ├── workers_ai.rs           #   WaiBudget DO：Workers AI 日额度账本
│       └── acu_limit.rs            #   AcuConcurrency DO：自定义上游并发闸
├── frontend/
│   ├── src/lib.rs                  # 前台 Worker：静态文件服务（30 行）
│   └── public/index.html           # ★ 前端全部内容：单文件 SPA（HTML+CSS+JS）
├── scripts/
│   ├── build.ps1                   # 构建流水线：cargo → wasm-bindgen → esbuild
│   └── shim.legacy.template.js     # Wasm 加载 shim 模板
├── .cargo/config.toml              # 国内 rsproxy 镜像（可删）
├── vars.example.toml               # 环境变量样例（复制为 vars.toml 填真值）
└── wrangler.toml                   # Workers 配置（含占位符）
```

**文件职责边界**：想改 API 行为/路由/上游 → `gateway/src/lib.rs`；想改页面/工具/游戏 → `frontend/public/index.html`；想改构建 → `scripts/build.ps1`。三个文件之外基本不用碰。

---

## 3. 网关核心代码导读（lib.rs）

按阅读顺序讲解 `gateway/src/lib.rs` 的六大块：

### 3.1 静态模型目录 `MODEL_CATALOG`

```rust
const MODEL_CATALOG: &[(&str, &str)] = &[
    ("acu/deepseek-v4-flash", "acu"),   // (模型 ID, 供应商标签)
    ...
];
```

- 编译期打进 Wasm 二进制，`/v1/models` 直接输出，不请求上游，所以列表永远是即时返回
- `owned_by` 标签决定无前缀模型的路由归属；带前缀的模型（`zhipu/xxx`）不需要登记在这里

### 3.2 供应商配置 `provider_cfg`

```rust
fn provider_cfg(env: &Env, provider: &str) -> Option<ProviderCfg> {
    // 每个 provider 对应：BASE 环境变量、公开默认值、KEY 环境变量
    "gitee" => ("GITEE_BASE", GITEE_BASE_DEFAULT, "GITEE_KEY"),
}
```

- Base URL 有公开默认值（不泄密），**Key 只从环境变量来**，没配置 → 该通道自动禁用（返回 502）
- 占位符 `REPLACE_WITH_REAL_KEY` 被视为未配置（防止你忘了改占位符导致假部署）

### 3.3 路由判别 `provider_of`

判定顺序：**显式前缀**（`zhipu/`、`acu/` 等）→ **静态目录查 owned_by** → **兜底 Nvidia**。

### 3.4 三大 Durable Object（有状态调度）

| DO | 文件 | 职责 | 关键参数 |
|---|---|---|---|
| `NvKeyPool` | keypool.rs | Nvidia 密钥池：轮询/限速/冷却/封禁 | 38 次/分/Key，429 冷却 60s，3 连败隔离 12h |
| `WaiBudget` | workers_ai.rs | Workers AI 日额度账本，跨天自动清零 | 每账号 `WAI_CAP_GLOBAL` 神经元/日 |
| `AcuConcurrency` | acu_limit.rs | 自定义上游全局并发闸 | `ACU_MAX_CONCURRENT`（默认 8） |

DO 的套路都一样：`load()` 从 storage 读状态 → 处理命令 → `save()` 写回。单线程所以不用考虑并发写冲突。

### 3.5 转发工具函数

```rust
build_upstream_req(...)  // 构造上游 Request；注意 GET 请求不能带 body（Workers 运行时会 500）
passthrough(res)         // 透传响应并保留 Content-Type（SSE 流式的命根子）
forward(req, timeout)    // 发送 + 透传
direct_forward(...)      // 按 provider 读 env 配置直连转发（gitee/zhipu 等）
```

### 3.6 请求主流程 `main()`

```
OPTIONS → 直接回 CORS
    ↓
路径是否公开（/ 与 /v1/models）
    ↓ 否
extract_key（Bearer / x-api-key）→ auth_mode 判定（open 全放 / key 查白名单）
    ↓
Router 分发到各 handler
```

---

## 4. 前端代码导读（index.html）

单文件 SPA，按区块阅读：

| 区块 | 内容 |
|---|---|
| `<style>` | 全部 CSS。CSS 变量双主题（`:root` 暗 / `[data-theme="light"]` 亮） |
| 导航 + 页面骨架 | 8 个 `<section class="page">`，hash 路由切换 |
| **GATEWAY 探测** | `var GATEWAY = (function(){...})()`：跨域常量 → acu.→api. 推导 → 同源 `/v1` 兜底 |
| **模型元数据** | `fallbackModels` 兜底清单 + `classify()` 平台/类型推断 + `TYPES` 表 |
| **模型列表/能力页** | `render()` / `renderCapabilities()` / `renderModelDetail()` |
| **Playground** | SSE 流式对话（`pgSend` 的 pump 循环） |
| **★ 工具基建** | `apiFetch` / `apiChat` / `toolShell` / `TOOL_REGISTRY`（见下） |
| **工具与游戏** | 10 个 `renderToolXxx(el)` 函数，每个自包含 |
| `route()` | hash → 页面显示，`#/tools/{name}` 走注册表 |

### 前端三个核心设施（二开必读）

**① 统一网关请求 `apiFetch`** —— 自动带鉴权，非 2xx 自动解析网关错误结构并抛出**中文原因**：

```js
apiFetch("/moderations", { method: "POST", body: JSON.stringify({...}) })
  .then(render)
  .catch(function (err) { box.innerHTML = err.message; });  // 直接就是中文
```

**② 非流式对话 `apiChat`** —— 工具/游戏调 LLM 的统一入口，自动选最优可用模型：

```js
apiChat([{ role: "user", content: "..." }], { temperature: 0.2, max_tokens: 30 })
  .then(function (out) { /* out = 模型回复文本 */ });
```

**③ 工具注册表 `TOOL_REGISTRY`** —— 新工具接入路由零成本：

```js
var TOOL_REGISTRY = {
  ip: renderToolIp,
  moderation: renderToolModeration,
  // 在这里加一行 → #/tools/你的名字 自动生效
};
```

---

## 5. 常见扩展任务实操

### 5.1 新增一个模型

**场景**：上游平台新出了模型，或你想接入自己部署的模型。

1. 打开 `gateway/src/lib.rs`，在 `MODEL_CATALOG` 追加一行：

```rust
("my-model-id", "gitee-ai"),   // 走 Gitee 通道；Nvidia 通道写 "nvidia"
```

2. 如果模型带自定义前缀（如 `myplat/xxx`），还需在 `provider_of()` 加前缀匹配 + 在 `provider_cfg()` 注册通道（见 5.2）。

3. 前端 `frontend/public/index.html` 的 `fallbackModels` 数组可同步加一条（仅在网关 `/v1/models` 不可达时兜底显示）。

4. 重新构建部署：

```powershell
powershell -File scripts/build.ps1 gateway
powershell -File scripts/deploy.ps1   # 或你自己的部署命令
```

**验证**：`GET /v1/models` 能看到新模型 → `POST /v1/chat/completions` 指定它试试。

### 5.2 新增一个上游供应商

**场景**：接入一个新的 OpenAI 兼容上游（比如新的免费平台）。

1. `lib.rs` 顶部加默认 Base：

```rust
const MYPLAT_BASE_DEFAULT: &str = "https://api.myplat.com/v1";
```

2. `provider_cfg()` 加分支：

```rust
"myplat" => ("MYPLAT_BASE", MYPLAT_BASE_DEFAULT, "MYPLAT_KEY"),
```

3. `provider_of()` 加前缀匹配：

```rust
if model.starts_with("myplat/") { return "myplat"; }
```

4. `handle_chat()` 的 match 加转发分支：

```rust
"myplat" => match provider_cfg(&env, "myplat") {
    Some(cfg) => proxy_direct(&cfg.base, &cfg.key, None, &body_bytes).await,
    None => err_res(502, "该模型的上游通道暂不可用，请稍后重试"),
},
```

5. `gateway/vars.example.toml` 加样例行；`gateway/wrangler.toml` 的 vars 里加占位符；本地 `vars.toml` 填真值。

6. 前端 `PLATFORM_LABEL` 加显示名，`classify()` 加平台识别。

### 5.3 新增一个工具 API（网关侧）

**场景**：像 `/v1/tools/uuid` 一样，加一个纯算法端点。

1. `lib.rs` 写 handler（纯算法参考 `handle_tool_uuid`；收 JSON body 用 `read_json_body`）：

```rust
/// POST /v1/tools/reverse  {"text": "..."} → 倒序文本
async fn handle_tool_reverse(mut req: Request) -> Result<Response> {
    let body = match read_json_body(req).await {
        Ok(v) => v,
        Err(res) => return Ok(res),           // read_json_body 已构造好 400 响应
    };
    let text = body.get("text").and_then(|v| v.as_str()).unwrap_or("");
    let rev: String = text.chars().rev().collect();
    json_res(&serde_json::json!({ "result": rev }))
}
```

2. `main()` 的 router 注册：

```rust
.post_async("/v1/tools/reverse", |req, _ctx| async move {
    handle_tool_reverse(req).await
})
```

3. README 的端点表加一行。**注意**：错误一律用 `err_res(400, "中文原因")`，前端会原样展示。

### 5.4 新增一个网页工具/游戏（前端侧）

**场景**：给工具箱加一个新页面（工具或游戏均可）。

1. 在 `index.html` 的 `<script>` 区写渲染函数（骨架模板）：

```js
/* ---- 工具：我的新工具 ---- */
function renderToolMytool(el) {
  el.innerHTML = toolShell("我的新工具",
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="width:22px;height:22px;vertical-align:-4px;color:var(--accent);"><circle cx="12" cy="12" r="9"/></svg>',
    "/v1/tools/xxx",                                    // 端点标签
    '<p class="tool-intro">功能一句话介绍。对应 API：<code>POST /v1/tools/xxx</code>。</p>' +
    '<div class="tool-io"><textarea id="my-input" rows="4" placeholder="输入…"></textarea>' +
    '<div class="tool-btns"><button class="btn tool-run" id="my-run">运行</button></div></div>' +
    '<div id="my-result" class="tool-result"></div>');
  document.getElementById("my-run").addEventListener("click", function () {
    var box = document.getElementById("my-result");
    box.innerHTML = '<div class="tool-loading">处理中…</div>';
    apiFetch("/tools/xxx", { method: "POST", body: JSON.stringify({ text: document.getElementById("my-input").value }) })
      .then(function (j) { box.innerHTML = '<div class="tool-ai-box"><b>结果</b><div class="tool-ai-text">' + esc(j.result) + "</div></div>"; })
      .catch(function (err) { box.innerHTML = '<div class="tool-empty">失败：' + esc(err.message) + "</div>"; });
  });
}
```

2. 注册进注册表（一行）：

```js
var TOOL_REGISTRY = {
  ...
  mytool: renderToolMytool,   // ← 加这行
};
```

3. 工具箱页面（`#page-tools` 的 grid 里）加卡片：

```html
<a class="tool-card" href="#/tools/mytool">
  <span class="tool-ic">…图标…</span>
  <b>我的新工具</b>
  <span class="tool-desc">一句话描述</span>
  <span class="tool-tag">/v1/tools/xxx</span>
</a>
```

完成。路由、页面显示、返回按钮、错误展示全部自动生效。

**调 LLM 的工具**：把第 1 步的 `apiFetch` 换成 `apiChat(messages, opts)` 即可，参考 AI 摘要的实现。

### 5.5 修改鉴权行为

| 需求 | 做法 |
|---|---|
| 关闭公开访问，只给指定密钥 | `AUTH_MODE="key"`，`GATEWAY_KEYS="key1,key2"`，重新部署 |
| 恢复任意密钥可用 | `AUTH_MODE="open"` 或删除该变量 |
| 改默认行为 | `auth_mode()` 的 match：`_ => "open"` 改掉 |
| 加新鉴权方式（如 IP 白名单） | 在 `main()` 的 `!is_public` 分支里加逻辑 |

前端演示密钥在 `index.html` 的 `DEMO_KEY` 常量——如果你部署为 `key` 模式，把它改成你白名单里的一把，工具箱/Playground 才能用。

---

## 6. 本地开发与调试流程

### 环境准备

```bash
rustup target add wasm32-unknown-unknown   # Wasm 编译目标
node --version                              # ≥ 18
npm install -g wrangler && wrangler login
```

### 日常改代码 → 验证循环

```powershell
# 1. 语法/类型检查（秒级，改一行验一行）
cd gateway ; cargo check

# 2. 本地起网关（true 本地模拟 D1/KV/DO）
wrangler dev --env production

# 3. 另开终端打请求
curl http://localhost:8787/v1/models
curl http://localhost:8787/v1/chat/completions -H "Authorization: Bearer test" -H "Content-Type: application/json" -d "{\"model\":\"acu/deepseek-v4-flash\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}]}"

# 4. 前端联调：直接浏览器打开 frontend/public/index.html？
#    不行——GATEWAY 探测会打到 localhost/v1。两个办法：
#    a) 临时把 GATEWAY 探测函数里 explicit 填 "http://localhost:8787/v1"
#    b) 或部署到测试 Worker 后用线上前端调
```

### 常改常验证的三个点

| 改动 | 验证命令 |
|---|---|
| 网关路由/handler | `cargo check` → `wrangler dev` → curl 对应路径 |
| 模型目录 | `cargo check` → 部署后 GET /v1/models |
| 前端 JS | 浏览器 F12 Console 看报错；Network 面板看请求 |

---

## 7. 部署流水线详解

### build.ps1 干了什么

```
cargo build --release --target wasm32-unknown-unknown   # Rust → Wasm
   ↓
wasm-bindgen 生成 JS 胶水（.tools/wasm-bindgen 本地工具链）
   ↓
esbuild 打包 legacy shim（兼容旧浏览器）
   ↓
输出到 {crate}/build/worker/（shim.mjs + xxx.wasm）
```

### 部署方式

**方式 A：官方姿势（开源用户）** —— 密钥走 `wrangler secret` 或手填 `wrangler.toml`：

```bash
wrangler secret put NVIDIA_KEYS
wrangler deploy --env production
```

**方式 B：站长私有脚本（本仓库维护者）** —— `scripts/deploy.ps1`：

```
读 vars.toml + wrangler.local.toml（两份本地文件，均不入 Git）
   → 把真实值注入 wrangler.toml 占位符
   → wrangler deploy
   → finally 还原占位符（保证 Git 工作区永远干净）
```

> ⚠️ `deploy.ps1`/`vars.toml`/`wrangler.local.toml` 永远不要提交。`.gitignore` 已排除，但请自觉。

### 部署后自检

```bash
curl https://你的网关域名/v1/models | head -c 200     # 模型列表
curl -X POST https://你的网关域名/v1/chat/completions \
  -H "Authorization: Bearer any" -H "Content-Type: application/json" \
  -d '{"model":"acu/deepseek-v4-flash","messages":[{"role":"user","content":"hi"}]}'
```

---

## 8. 错误码与排障手册

### 错误响应结构（全部端点统一）

```json
{
  "error": {
    "message": "中文原因描述",
    "type": "api_error",
    "code": 400,
    "help": {
      "site": "https://官网",
      "qq_guild": "频道号",
      "qq_guild_url": "频道链接",
      "qq_group": 1103667832
    }
  }
}
```

### 状态码速查

| 码 | 含义 | 常见原因 | 排查 |
|---|---|---|---|
| 400 | 请求参数错误 | JSON 不合法 / 缺 model 字段 / 审核模型用错 | 看 message 具体说明；审核必须用 `Security-semantic-filtering` |
| 401 | 密钥无效 | `AUTH_MODE=key` 且密钥不在白名单 | 换白名单内密钥，或改回 open |
| 404 | 路由不存在 | 路径打错 / 工具未注册 | 对照 README 端点表 |
| 429 | 限流/额度尽 | Workers AI 日额度尽 / 上游限流 / 模型被封禁 | 看 message；等 00:00 UTC 或换模型 |
| 502 | 上游不可用 | 对应通道密钥未配置 / 上游挂了 | 检查 vars 里该通道 KEY 是否注入 |
| 503 | 全部密钥忙 | Nvidia 池本分钟配额用尽 | 稍后重试 |

### 前端排障三板斧

1. **F12 Console**：JS 报错一目了然
2. **F12 Network**：看失败请求的状态码与响应体（错误 message 就在里面）
3. **确认 GATEWAY**：Console 里敲 `GATEWAY`，确认指向你的网关域名（含 `/v1`）

---

## 9. 二次部署检查清单

从零把 AQUA 部署成你自己的服务，按顺序勾：

- [ ] Rust + wasm32 target + Node 18 + wrangler 就绪
- [ ] `gateway/wrangler.toml`：KV ID / D1 ID / 路由域名占位符全部替换
- [ ] `frontend/wrangler.toml`：前台路由域名替换
- [ ] `cp vars.example.toml vars.toml`，填入至少一个通道的密钥
- [ ] Workers AI 想用就配 `WAI_ACCOUNT_ID`/`WAI_API_TOKEN`（不用可跳过，通道自动禁用）
- [ ] `scripts/build.ps1 gateway` 与 `frontend` 构建通过
- [ ] 部署网关 → 部署前台
- [ ] 前台 `index.html` 的 `GATEWAY` 探测符合你的域名结构（同域部署不用改；`acu.`/`api.` 约定或改 `explicit`）
- [ ] `curl /v1/models` 200，`/chat/completions` 200
- [ ] 打开前台：模型列表加载 → Playground 对话 → 工具箱各工具
- [ ] `AUTH_MODE` 按需选择 open / key
- [ ] **确认 Git 里没有任何真实密钥/域名/资源 ID 再 push**

---

## 附：给贡献者的约定

- 所有用户可见的错误信息一律**中文**，并说明「怎么办」而不只是「出错了」
- 新增端点必须：`err_res` 统一错误结构 + README 端点表登记
- 新增前端工具必须走 `TOOL_REGISTRY` + `apiFetch`/`apiChat`，不要裸 fetch
- 提交信息格式：`feat|fix|style|docs(scope): 中文描述`
- 每个小功能独立提交，保证可回溯

有疑问欢迎到 [QQ 频道](https://pd.qq.com/s/e4ktxw1b8) 交流，或提 [Gitee Issue](https://gitee.com/xiaosu4610/aqua-rust-workers/issues)。
