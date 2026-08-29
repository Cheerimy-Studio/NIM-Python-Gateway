# scripts/ — 构建脚本

只负责**构建**（编译打包），不负责部署。部署统一用 `wrangler deploy`。

## 文件导览

| 文件 | 作用 |
|---|---|
| `build.ps1` | 一键构建脚本：`cargo build`（wasm32）→ `wasm-bindgen` 生成 JS 胶水 → 复制产物 → esbuild 打包为单文件 `shim.mjs` |
| `shim.legacy.template.js` | Worker 运行时 shim 模板（兼容 worker-rs 的 legacy 导出方式），构建时由 build.ps1 填充生成 |

## 用法

```powershell
# 在仓库根目录执行（参数二选一）
powershell -File scripts/build.ps1 gateway    # 构建网关 → gateway/build/worker/shim.mjs
powershell -File scripts/build.ps1 frontend   # 构建前台 → frontend/build/worker/shim.mjs
```

## 构建流程（build.ps1 做了什么）

1. 设置 `WASM_BINDGEN_USE_JS_SYS=1`（worker-rs 必需）
2. `cargo +stable-x86_64-pc-windows-gnu build --target wasm32-unknown-unknown --release`
3. `wasm-bindgen --target bundler` 生成 JS 胶水与 `_bg.wasm`
4. 组装 `build/worker/` 目录（index.wasm + 胶水 + snippets）
5. esbuild 打包 shim.js → `shim.mjs`（wasm 作为 external 资源）
6. 清理中间产物，输出最终体积报告

## 常见问题

- **cargo build failed**：确认安装了 rustup 与 `wasm32-unknown-unknown` target（`rustup target add wasm32-unknown-unknown`）
- **wasm-bindgen failed**：`.tools/` 目录需要有 wasm-bindgen 0.2.127 可执行文件（见根 README 环境准备）
- **构建产物在哪**：`<target>/build/worker/shim.mjs` + `index.wasm`，wrangler.toml 的 `main` 字段指向它们
