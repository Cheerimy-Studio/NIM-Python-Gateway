# frontend/ — 用户前台（Rust Worker + 静态单页）

AQUA 的门面：一个独立的 Cloudflare Worker，托管品牌首页、模型列表、能力矩阵与 API 文档。

## 文件导览

| 文件 | 作用 |
|---|---|
| `src/lib.rs` | Worker 入口：静态资源服务（R2/KV 缓存策略）、SPA 路由回退、favicon 等资产响应 |
| `public/index.html` | **全部前端内容都在这一个文件**（内联 CSS/JS 的单页应用）：首页介绍、Base URL/Key 卡片、QQ 频道与开源仓库入口、模型列表（实时调网关 `/v1/models`）、模型能力矩阵、API 文档与错误码表 |
| `wrangler.toml` | 前台 Worker 配置：域名路由（部署前替换 `your-frontend-domain.example`） |

## 前台功能页

- **首页**：品牌介绍、Base URL 与 API Key 卡片（复制即用）、QQ 频道/群与开源仓库入口、官方自营模型板块
- **模型列表**：自动请求网关 `/v1/models` 实时渲染，按平台筛选，一键复制模型 ID
- **模型能力**：各模型支持的参数矩阵（上下文长度、是否支持流式/函数调用等）
- **API 文档**：每个端点的请求示例（curl/Python/JS）、错误与状态码表、常见问题

## 新手上手

1. 编辑 `wrangler.toml`，把 `your-frontend-domain.example` 换成你的前台域名
2. 想改品牌/文案/配色？直接编辑 `public/index.html` 搜索对应文字即可，无需懂框架
3. 回仓库根目录 `powershell -File scripts/build.ps1 frontend`，然后 `cd frontend && wrangler deploy --env production`

> 注意：前台页面里的 `GATEWAY` 常量（index.html 中搜索 `var GATEWAY`）要改成你的网关地址，模型列表才能连上你的后端。
