//! AQUA 前台 — Cloudflare Worker (Rust → Wasm)
//!
//! 页面为纯静态单文件（public/index.html，含内联 CSS/JS），
//! 编译时通过 include_str! 打入 Wasm 二进制，零外部依赖。
//! 模型列表由页面 JS 动态调用网关 /v1/models 获取（保证实时准确）。

use worker::*;

#[event(fetch)]
pub async fn main(req: Request, _env: Env, _ctx: Context) -> Result<Response> {
    console_error_panic_hook::set_once();

    let path = req.path().to_string();
    // 品牌 favicon（嵌入 Wasm 二进制，浏览器标签页图标）
    if path == "/favicon.ico" {
        let mut res = Response::from_body(ResponseBody::Body(include_bytes!("../public/favicon.ico").to_vec()))?;
        res.headers_mut().set("Content-Type", "image/x-icon")?;
        res.headers_mut().set("Cache-Control", "public, max-age=604800, immutable")?;
        return Ok(res);
    }

    let html: &str = include_str!("../public/index.html");
    let mut res = Response::ok(html.to_string())?;
    // 覆盖默认 text/plain 为 text/html
    res.headers_mut().set("Content-Type", "text/html; charset=utf-8")?;
    res.headers_mut().set("Cache-Control", "public, max-age=300")?;
    Ok(res)
}