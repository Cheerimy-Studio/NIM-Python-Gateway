# NIM Python Gateway

面向 **NVIDIA NIM**（以及任意 OpenAI 兼容上游）的多账号 API 网关。单文件进程、JSON 存储、无外部依赖，
内置账号池调度、多维限速、分级冷却与熔断、排队保护、完整 OpenAI / Anthropic 协议兼容，以及一个自带的管理后台。

- 纯异步（FastAPI + httpx），单进程可支撑高并发
- 无数据库、无 Redis：状态存于 `data/db.json`，原子写入
- 真流式（SSE 逐块透传），并针对慢速推理模型做了**保活**处理
- 开箱即用的 Web 后台：账号、渠道、限额、日志、排队一屏管理

---

## 特性

### 账号池与调度
- **多账号池**：批量导入邮箱/密码/API Key，自动在渠道间调度
- **LRU + 可行度路由**：优先使用最久未用的健康账号，按渠道成功率加权分配
- **并发保护**：账号级 / 渠道级并发上限，避免同一把 Key 被并行打爆
- **多维限速**：账号 RPM、TPM、日请求上限、日 Token 上限、小时请求上限，以及池级 RPM / 日上限
- **账号预热**：新账号在预热期内按比例限速放开，避免刚导入就被上游风控

### 稳定性
- **错误分级处理**：`channel` / `model` / `429` / `auth` / `timeout` / `conn` / `5xx` / `req` 分类，各自对应不同处置策略
- **分级冷却**：429 / 5xx / 超时 / 连接失败各有独立冷却时长；429 冷却按连续次数**指数递增**（上限 5 分钟）
- **同号快速重试**：瞬态错误（连接失败 / 5xx / 空响应）先用同一账号重试一次，避免误判账号故障
- **429 吸收**：命中限流时在账号池内换号重试，而不是直接把 429 抛给客户端
- **模型熔断**：某模型连续失败达阈值后短暂熔断，期间请求排队等待恢复
- **失败封禁阶梯**：连续失败按阶梯封禁，严重失败直接禁用账号
- **排队保护**：无可用账号时进入 FIFO 队列并行重试，队列等待窗口自动覆盖 429 冷却时长；
  客户端断连立即退出（日志记 `499`），不再为死连接占用账号

### 流式与超时
- **真流式**：`client.send(stream=True)` 逐块透传，首字节延迟≈上游首字节延迟（而非等整包读完）
- **保活帧**：上游推理慢（实测某些模型首字节 100s+）时持续发送合法 `data:` 空 chunk，
  穿透中转网关（New-API / one-api 等）与 CDN，避免长静默被按空闲超时掐断
- **响应头延迟兜底**：上游连响应头都迟迟不返回时，先提交 200 并持续保活，拿到响应头后顺势透传
- **`ttfb_timeout` / `sse_idle_timeout`**：分别约束「首字节」与「流中相邻 chunk 间隔」
- **空响应 / 空流保护**：绝不把空响应或空流透传给下游

### 协议兼容
- `POST /v1/chat/completions`（流式 / 非流式）
- `POST /v1/completions`
- `POST /v1/embeddings`
- `POST /v1/responses`（OpenAI Responses API，含流式事件转换）
- `POST /v1/messages`（Anthropic Messages API，含流式事件转换）
- `GET /v1/models`、`GET /v1/models/{id}`
- CORS 预检、`Authorization: Bearer` 鉴权
- **响应补全**：上游漏 `usage` 时自动补齐 `total_tokens`；流式强制 `text/event-stream`；
  非流式强制 `application/json`（不原样转发上游可能异常的头）
- **思考参数自动降级**：上游报 `Unsupported thinking_effort=...` 时，按渠道配置的默认强度改写或移除该参数后重试
- **请求体类型归一化**：客户端把 `"temperature": "0.95"` 当字符串发时自动转成原生类型

---

## 快速开始

```bash
git clone https://github.com/Cheerimy-Studio/NIM-Python-Gateway.git
cd NIM-Python-Gateway
pip install -r requirements.txt

# 启动（Windows 用 run.bat）
./run.sh
```

启动后访问 `http://127.0.0.1:8080/admin`。

### 首次登录

首次启动会**随机生成管理员密码并打印到控制台/日志**：

```
==============================================================
  首次初始化管理员账号
    用户名: admin
    密  码: xxxxxxxxxxxxxxxx
  请立即登录后台修改密码。
==============================================================
```

如需指定初始密码（例如自动化部署），设置环境变量后首次启动：

```bash
NGW_ADMIN_PASSWORD='your-strong-password' ./run.sh
```

登录后到「后台设置 → 修改密码」改掉即可。

### 环境变量

| 变量 | 说明 | 默认 |
|---|---|---|
| `NGW_DATA_DIR` | 数据目录（`db.json` 所在位置） | 项目根 `data/` |
| `NGW_ADMIN_PASSWORD` | 仅首次初始化时使用的管理员密码 | 随机生成 |
| `PORT` | 监听端口（仅 `run.sh`） | `8080` |

### Docker

```bash
docker build -t nim-gateway .
docker run -d --name nim-gateway -p 8080:8080 -v "$PWD/data:/app/data" nim-gateway
docker logs nim-gateway   # 查看首次生成的密码
```

---

## 配置

所有配置都可在后台 `/admin` 修改，保存在 `data/db.json`。渠道级配置支持三态覆盖：
**`-1` = 不限，`0` = 继承全局，正数 = 覆盖全局**。

### 调度与限额

| 配置项 | 默认 | 说明 |
|---|---|---|
| `rate_limit_per_minute` | `20` | 单账号每分钟请求数上限（`0`/`-1` 不限） |
| `tpm_limit` | `50000` | 单账号每分钟 Token 上限 |
| `hourly_request_limit` | `5` | 单账号每小时请求上限 |
| `daily_request_cap` | `100` | 单账号每日请求上限 |
| `daily_token_limit` | `900000` | 单账号每日 Token 上限 |
| `account_cooldown_ms` | `500` | 同一账号两次使用的间隔（毫秒） |
| `acct_concurrency` | `0` | 单账号并发上限（`0` 不限；建议设 `2` 以内） |
| `total_concurrency` | `0` | 渠道总并发上限 |
| `pool_rpm_cap` / `pool_daily_cap` | `0` | 池级 RPM / 日请求上限 |
| `warmup_seconds` | `300` | 新账号预热时长 |

### 重试、冷却与封禁

| 配置项 | 默认 | 说明 |
|---|---|---|
| `max_retries` | `2` | 最大重试次数 |
| `retry_backoff_base_ms` / `retry_backoff_max_ms` | `500` / `4000` | 重试退避区间 |
| `retry_min_wait_ms` | `0` | 强制最小重试等待 |
| `cool_429_seconds` | `30` | 429 基础冷却（按连续次数指数递增，上限 300s） |
| `cool_5xx_seconds` | `30` | 5xx 冷却 |
| `cool_timeout_seconds` | `45` | 超时冷却 |
| `cool_conn_seconds` | `10` | 连接失败冷却 |
| `ban_step_seconds` / `ban_max_seconds` | `5` / `300` | 失败封禁阶梯 |
| `hard_fail_ban_seconds` / `hard_fail_disable_count` | `600` / `3` | 严重失败封禁 / 禁用阈值 |
| `breaker_enabled` / `breaker_threshold` / `breaker_seconds` | `True` / `3` / `60` | 模型熔断 |

### 超时与排队

| 配置项 | 默认 | 说明 |
|---|---|---|
| `request_timeout` | `120` | 上游读取超时（大 prompt 建议调高到 `300`） |
| `connect_timeout` | `10` | 上游连接超时 |
| `ttfb_timeout` | `60` | **首字节**超时。推理模型首字节可能上百秒，务必按模型实测调高（如 `240`） |
| `sse_idle_timeout` | `60` | 流中相邻 chunk 的最大间隔 |
| `queue_enabled` | `True` | 无可用账号时是否排队 |
| `queue_max_wait` | `15` | 队列最大等待秒数（会自动抬到 `cool_429_seconds + 2`） |
| `queue_poll_ms` | `400` | 队列轮询间隔 |

### 其他

| 配置项 | 默认 | 说明 |
|---|---|---|
| `upstream_base` | `https://integrate.api.nvidia.com/v1` | 默认上游地址 |
| `model_whitelist` / `model_blacklist` | 空 | 对外暴露的模型白/黑名单（支持逐行或逗号分隔） |
| `param_overrides` | 空 | 全局固定参数覆写 |
| `hide_upstream_errors` | `True` | 隐藏上游原始报错细节 |
| `hide_mapped_names` | `True` | 隐藏映射后的真实模型名 |
| `log_enabled` / `log_max` | `True` / `200` | 请求日志开关 / 保留条数 |
| `session_log_max` | `100` | 会话内容审计保留条数（`0` 关闭） |
| `models_cache_ttl` | `600` | 模型列表缓存秒数 |
| `verify_tls` | `True` | 校验上游 TLS 证书 |

---

## ⚠️ 反向代理 / CDN 部署注意

网关前面有 nginx、CDN 或中转网关时，**必须放大空闲超时**，否则慢速推理模型的请求会被静默掐断：
客户端只看到「请求失败」，而网关日志里却是一条 `200`。

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Connection "";

    proxy_buffering off;          # 关键：流式响应必须关闭缓冲
    proxy_read_timeout 600s;      # 关键：默认 60s 会让慢模型请求被掐断
    proxy_send_timeout 600s;
    chunked_transfer_encoding on;
}
```

同样的道理适用于 CDN（腾讯 EdgeOne / Cloudflare 等）：它们的**回源超时默认只有 30~100 秒**，
需要调高，否则后台的「渠道测试」会在界面上显示失败，而实际上服务端早已成功。

---

## 测试

仓库自带两套无外部依赖的测试（会临时拉起 mock 上游 + 独立数据目录，不会碰你的 `data/`）：

```bash
pip install -r requirements-dev.txt

python tests/regression.py   # 19 项：调度、限速、重试、保活、并发
python tests/compat.py       # 17 项：全部接口 + 协议结构兼容性
```

---

## 项目结构

```
server.py            入口：路由、请求代理、流式透传、排队取号
admin_api.py         管理端 API（认证、账号、渠道、日志、设置、配置导入导出）
core/
  store.py           JSON 存储：原子写、写回合并、默认配置与迁移
  pool.py            账号池：导入解析、调度、限速、冷却、熔断
  upstreams.py       渠道管理：CRUD、模型映射、固定参数、可达性预检
  convert.py         协议转换：Responses / Anthropic ↔ Chat Completions
  streams.py         流式状态机：chat chunk → Responses / Anthropic 事件流
  queue.py           FIFO 排队与统计
  util.py            通用工具
web/                 管理后台前端（原生 HTML/JS，无构建步骤）
tests/               回归与兼容性测试
data/                运行时数据（db.json，已 gitignore）
```

---

## 安全说明

- `data/` 内含**真实 API Key、密码哈希与会话密钥**，已加入 `.gitignore`，请勿提交
- 首次启动随机生成管理员密码；请勿使用弱口令
- 建议仅在内网或加鉴权后对外暴露；如需公网部署，请配合 HTTPS 与访问控制

---

## 许可

[MIT](LICENSE)
