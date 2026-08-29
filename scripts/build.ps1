# AQUA Worker 打包脚本（复刻 worker-build legacy 流程，适配 worker 0.4.x，无需编译 worker-build）
# 用法: powershell -File scripts\build.ps1 gateway
# 参数: gateway | frontend
param([Parameter(Mandatory=$true)][ValidateSet("gateway","frontend")][string]$Target)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Crate = Join-Path $Root $Target
$BuildDir = Join-Path $Crate "build"
$Staging = Join-Path $BuildDir "staging"
$WorkerDir = Join-Path $BuildDir "worker"
$Wbg = Join-Path $Root ".tools\wasm-bindgen\wasm-bindgen-0.2.127-x86_64-pc-windows-msvc\wasm-bindgen.exe"

# 1. 编译（WASM_BINDGEN_USE_JS_SYS=1 是 worker-rs 运行所必需的）
$env:WASM_BINDGEN_USE_JS_SYS = "1"
Push-Location $Crate
cargo +stable-x86_64-pc-windows-gnu build --target wasm32-unknown-unknown --release
if ($LASTEXITCODE -ne 0) { throw "cargo build failed" }
$Wasm = Get-ChildItem "target\wasm32-unknown-unknown\release\*.wasm" | Where-Object { $_.Name -notlike "*.d.*" } | Select-Object -First 1
if (-not $Wasm) { throw "wasm artifact not found" }
Pop-Location

# 2. wasm-bindgen 生成 JS 胶水（--target bundler，legacy 风格）
Remove-Item $Staging -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $Staging -Force | Out-Null
& $Wbg $Wasm.FullName --no-typescript --target bundler --out-name index --out-dir $Staging
if ($LASTEXITCODE -ne 0) { throw "wasm-bindgen failed" }

# 3. 组装 worker/ 目录（胶水 index_bg.js + 重命名的 index.wasm）
Remove-Item $WorkerDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $WorkerDir -Force | Out-Null
Copy-Item (Join-Path $Staging "index_bg.js") $WorkerDir
Copy-Item (Join-Path $Staging "index_bg.wasm") (Join-Path $WorkerDir "index.wasm")
if (Test-Path (Join-Path $Staging "snippets")) {
    Copy-Item (Join-Path $Staging "snippets") $WorkerDir -Recurse
}

# 4. 从模板生成 shim.js（worker 0.4 legacy；本项目无 snippets）
$tmpl = Get-Content -Raw (Join-Path $PSScriptRoot "shim.legacy.template.js")
$shim = $tmpl.Replace('$WAIT_UNTIL_RESPONSE', '').Replace('$SNIPPET_JS_IMPORTS', '').Replace('$SNIPPET_WASM_IMPORTS', '')
$shim | Set-Content -Path (Join-Path $WorkerDir "shim.js") -Encoding UTF8

# 5. esbuild 打包为 shim.mjs
$esb = Join-Path $Root "node_modules\esbuild\bin\esbuild"
Push-Location $WorkerDir
& node $esb --external:./index.wasm --external:cloudflare:email --external:cloudflare:sockets --external:cloudflare:workers --format=esm --bundle ./shim.js --outfile=shim.mjs --allow-overwrite --minify
if ($LASTEXITCODE -ne 0) { throw "esbuild failed" }
Pop-Location

# 6. 清理临时文件
Remove-Item (Join-Path $WorkerDir "shim.js") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $WorkerDir "index_bg.js") -Force -ErrorAction SilentlyContinue
Remove-Item $Staging -Recurse -Force -ErrorAction SilentlyContinue

$kb = [math]::Round((Get-Item (Join-Path $WorkerDir "shim.mjs")).Length / 1KB)
$wkb = [math]::Round((Get-Item (Join-Path $WorkerDir "index.wasm")).Length / 1KB)
Write-Host "[$Target] OK -> $WorkerDir (shim.mjs ${kb}KB, wasm ${wkb}KB)"