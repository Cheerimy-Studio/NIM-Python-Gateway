"""NVIDIA NIM API 网关（FastAPI + httpx 全异步）。

端点：POST /v1/chat/completions | /v1/completions | /v1/embeddings | /v1/responses | /v1/messages
      GET  /v1/models
管理：/admin 页面 + /api/* 接口
"""

from __future__ import annotations

import asyncio
import hmac
import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, AsyncGenerator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from core import convert, pool, queue, upstreams
from core.convert import flatten_content
from core.streams import AnthropicStream, ResponsesStream
from core.store import STORE, csrf_token as _csrf_token, session_cookie as _session_cookie
from core.util import (
    sub_dict,
    estimate_output_tokens,
    estimate_request_tokens,
    mask_email,
    parse_model_list,
    rand_id,
    upstream_snippet,
)

FALLBACK_MODELS = [
    "deepseek-ai/deepseek-r1",
    "deepseek-ai/deepseek-v3",
    "meta/llama-3.3-70b-instruct",
    "meta/llama-3.1-70b-instruct",
    "qwen/qwen2.5-coder-32b-instruct",
    "microsoft/phi-4",
    "nvidia/llama-3.1-nemotron-70b-instruct",
]
MAX_BODY = 20 * 1024 * 1024
WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="NVIDIA Gateway", docs_url=None, redoc_url=None, openapi_url=None)


@app.on_event("shutdown")
async def _shutdown_http():
    await close_http()
    STORE.flush()  # 关闭前落盘


# 后台定期落盘：高频变更合并为批量写盘（每 2 秒）
_flush_task: asyncio.Task | None = None


@app.on_event("startup")
async def _start_flush():
    global _flush_task

    async def _loop():
        while True:
            await asyncio.sleep(2)
            try:
                STORE.flush()
            except Exception:
                pass

    _flush_task = asyncio.create_task(_loop())
    # 启动时确保存在可用渠道：无渠道、或账号绑定了不存在的渠道时自动建默认 NVIDIA 渠道，
    # 否则导入的账号会因没有归属渠道而永远无法被调度
    try:
        upstreams.ensure_default()
        STORE.flush()
    except Exception:
        pass


from admin_api import router as admin_router  # noqa: E402

app.include_router(admin_router)


@app.exception_handler(Exception)
async def global_error_handler(request: Request, exc: Exception):
    return JSONResponse(
        {"error": {"message": f"内部错误：{exc}", "type": "internal_error"}}, status_code=500, headers=_cors()
    )


@app.exception_handler(httpx.HTTPError)
async def http_error_handler(request: Request, exc: httpx.HTTPError):
    return JSONResponse(
        {"error": {"message": f"上游连接错误：{exc}", "type": "upstream_error"}},
        status_code=502,
        headers=_cors(),
    )


# ============================================================ 通用

# 进程级共享 HTTP 客户端（连接池复用 TLS 连接，避免每请求新建握手）
_shared_http: httpx.AsyncClient | None = None


def get_http(cfg: dict | None = None) -> httpx.AsyncClient:
    """返回共享 AsyncClient；timeout/verify 由每次请求自行覆盖。"""
    global _shared_http
    if _shared_http is None:
        verify = bool((cfg or STORE.load()["config"]).get("verify_tls", True))
        _shared_http = httpx.AsyncClient(
            verify=verify,
            timeout=httpx.Timeout(connect=10, read=120, write=30, pool=10),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )
    return _shared_http


async def close_http() -> None:
    global _shared_http
    if _shared_http is not None:
        await _shared_http.aclose()
        _shared_http = None


# 静态页缓存：内存保留，避免每请求读盘
_page_cache: dict[str, str] = {}


# 后台页面/脚本一律不缓存：否则升级后浏览器仍用旧界面（新功能会"打不开"）
_NOCACHE = {"Cache-Control": "no-cache, must-revalidate"}


def _page(name: str, csrf: str = "", version: str = "1.6.0") -> str:
    html = _page_cache.get(name)
    if html is None:
        html = (WEB_DIR / name).read_text(encoding="utf-8")
        _page_cache[name] = html
    html = html.replace("{{CSRF}}", csrf).replace("{{VERSION}}", version)
    html = html.replace("CSRF_FROM_COOKIE", csrf)
    return html


_cfg_cache: tuple[float, dict] = (0.0, {})


def _cfgint(cfg: dict, name: str, default: int) -> int:
    """读全局整型配置：字段存在就用其值（0 有效），仅缺失/非法才用默认值。"""
    v = cfg.get(name)
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def cfg_all() -> dict:
    """配置读取缓存：1 秒内多次调用直接命中内存，避免每请求多次 stat+锁。"""
    global _cfg_cache
    now = time.monotonic()
    if now - _cfg_cache[0] < 1.0 and _cfg_cache[1]:
        return _cfg_cache[1]
    cfg = STORE.load()["config"]
    _cfg_cache = (now, cfg)
    return cfg


def _cors() -> dict:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Authorization, Content-Type, X-API-Key, X-Request-Id, anthropic-version",
        "Access-Control-Max-Age": "86400",
        "Cache-Control": "no-store",
    }


def _client_ip(request: Request) -> str:
    for k in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
        v = request.headers.get(k)
        if v:
            return v.split(",")[0].strip()
    return request.client.host if request.client else "-"


def _bearer(request: Request) -> str | None:
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key")


def _has_auth(cfg: dict) -> bool:
    """是否需要鉴权：有网关令牌。"""
    return bool(cfg.get("gateway_tokens"))


def _token_entry(request: Request, cfg: dict) -> dict | None:
    tokens = cfg.get("gateway_tokens") or []
    given = _bearer(request)
    if not tokens:
        return {}  # 完全无鉴权配置 → 放行
    for t in tokens:
        t = {"t": t, "m": []} if isinstance(t, str) else dict(t)
        if given and hmac.compare_digest(str(t.get("t") or ""), given):
            return t
    return None


class ModelPolicy:
    @staticmethod
    def allowed(model: str, token_entry: dict | None, cfg: dict) -> bool:
        if not model:
            return True
        wl = parse_model_list(str(cfg.get("model_whitelist") or ""))
        if wl and model not in wl:
            return False
        bl = parse_model_list(str(cfg.get("model_blacklist") or ""))
        if bl and model in bl:
            return False
        tm = (token_entry or {}).get("m") or []
        if tm and model not in tm:
            return False
        return True


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _error(
    status: int,
    message: str,
    type_: str = "invalid_request_error",
    code: str | None = None,
    anthropic: bool = False,
) -> JSONResponse:
    if anthropic:
        t = {
            "404": "not_found_error",
            "400": "invalid_request_error",
            "401": "authentication_error",
            "429": "rate_limit_error",
        }.get(str(status), "api_error")
        return JSONResponse({"type": "error", "error": {"type": t, "message": message}}, status_code=status)
    return JSONResponse(
        {"error": {"message": message, "type": type_, "param": None, "code": code}}, status_code=status
    )


def _check_model(model: str, entry: dict | None, cfg: dict, anthropic: bool = False) -> JSONResponse | None:
    if ModelPolicy.allowed(model, entry, cfg) and upstreams.model_routable(
        model, bool(cfg.get("hide_mapped_names", True))
    ):
        return None
    msg = f"The model '{model}' does not exist or is not available for this token."
    return _error(404, msg, "invalid_request_error", "model_not_found", anthropic=anthropic)


# ============================================================ 排队与取号


async def take_account(request: Request, ep: str, model: str, est_tokens: int, cfg: dict) -> dict:
    max_wait = _cfgint(cfg, "queue_max_wait", 30)
    # 队列等待必须覆盖 429 冷却，否则账号还没到解禁时间队列就先放弃了，
    # 请求会以「暂无可用账号」凭空失败（虽然再等几秒本来就能成功）。
    cool429 = _cfgint(cfg, "cool_429_seconds", 30)
    if max_wait > 0 and cool429 > 0:
        max_wait = max(max_wait, min(cool429 + 2, 600))
    # 客户端已断开：不再取号/排队，避免为死连接占用账号或排队名额
    try:
        if await request.is_disconnected():
            return {"ok": False, "key": None, "status": 499, "message": "客户端已断开"}
    except Exception:
        pass
    br = pool.breaker_open(model)
    # 熔断打开：队列关闭则立即 503；队列开启则进入排队等待熔断恢复
    if br and (not cfg.get("queue_enabled", True) or max_wait <= 0):
        return {
            "ok": False,
            "key": None,
            "status": 503,
            "message": f"模型 {model} 此前连续失败 {br['fails']} 次，熔断中，约 {br['left']} 秒后自动恢复探测",
        }

    result_holder: dict = {}

    def _do_acquire(db: dict):
        result_holder.update(pool.acquire(db, est_tokens, model))

    # 熔断打开时不尝试取号，直接进排队等待恢复
    acq: dict = {"result": "none", "key": None, "reason": "", "total": 0}
    if not br:
        await STORE.aupdate(_do_acquire)
        acq = result_holder
    if acq["result"] == "ok":
        return {"ok": True, "key": acq["key"], "status": 0, "message": ""}
    if acq.get("total", 0) == 0 and not br:
        return {"ok": False, "key": None, "status": 503, "message": "密钥池为空，请先在后台导入密钥"}

    # 解析阻塞原因：区分「真正不可恢复」和「暂时不可用（值得排队等恢复）」。
    # 真正不可恢复：渠道模型不匹配、原名禁用（模型在这个渠道上就是不存在，排队也没用）。
    # 暂时不可用：上游停用、日限、封禁、冷却、限流、并发满——都会恢复，排队等待。
    reason = acq.get("reason", "")
    permanent = bool(acq.get("permanent"))
    if not cfg.get("queue_enabled", True) or max_wait <= 0 or permanent:
        if permanent:
            return {
                "ok": False,
                "key": None,
                "status": 404,
                "message": f"模型 {model} 在所有渠道均不可用：{reason}",
            }
        return {"ok": False, "key": None, "status": 429, "message": f"暂无可用账号：{reason}"}

    # 排队过长：直接快速失败，而不是让请求无限堆积（堆积只会把存储锁和内存拖垮）
    if _waiting.get(model, 0) >= QUEUE_MAX_WAITING:
        return {
            "ok": False,
            "key": None,
            "status": 503,
            "message": f"排队已满（{_waiting.get(model, 0)} 个请求在等账号），请稍后重试",
        }
    qid = queue.add(ep, model, _client_ip(request))
    deadline = time.time() + max_wait
    poll = max(0.05, _cfgint(cfg, "queue_poll_ms", 400) / 1000)
    # 熔断打开时先不取号，排队等恢复；非熔断则正常取号+排队
    waiting_breaker = br is not None
    backoff = poll
    _waiting[model] = _waiting.get(model, 0) + 1
    try:
        while time.time() < deadline:
            try:
                if await request.is_disconnected():
                    return {"ok": False, "key": None, "status": 499, "message": "客户端已断开"}
            except Exception:
                pass
            # 熔断中则等恢复（opened_until 过期即恢复），恢复后才尝试取号；
            # 非熔断则直接尝试取号。所有等待者并行重试，不做严格队首串行。
            hint = 0.0
            if not waiting_breaker or pool.breaker_open(model) is None:
                waiting_breaker = False
                holder: dict = {}
                await STORE.aupdate(lambda db: holder.update(pool.acquire(db, est_tokens, model)))
                if holder.get("result") == "ok":
                    return {"ok": True, "key": holder["key"], "status": 0, "message": ""}
                hint = float(holder.get("wait_hint") or 0)
                # 只在「排队的请求多」时才拉长退避：人多时几百个等待者一起重试会把
                # 存储锁打满；人少时保持最小间隔，账号一释放就能立刻抢到（否则明明
                # 空出来了还要再等一个退避周期，白白增加延迟）。
                crowded = _waiting.get(model, 0) > 4
                backoff = poll if (hint > 0 or not crowded) else min(backoff * 2, QUEUE_POLL_MAX)
            else:
                hint = float((pool.breaker_open(model) or {}).get("left") or 0)
            # 关键：加上抖动。否则几百个等待者会在同一时刻一起重试、把存储锁打满
            wait = max(poll, hint if hint > 0 else backoff)
            await asyncio.sleep(wait * (0.7 + random.random() * 0.6))
    finally:
        left = _waiting.get(model, 1) - 1
        if left > 0:
            _waiting[model] = left
        else:
            _waiting.pop(model, None)
        queue.remove(qid)
    if br:
        return {
            "ok": False,
            "key": None,
            "status": 503,
            "message": f"模型 {model} 此前连续失败 {br['fails']} 次，熔断中，已等待 {max_wait} 秒仍未恢复",
        }
    return {
        "ok": False,
        "key": None,
        "status": 429,
        "message": f"排队等待 {max_wait} 秒后超时，暂无可用账号：{acq.get('reason', '未知原因')}",
    }


def _backoff_ms(cfg: dict, attempt: int, key: dict | None) -> int:
    """失败重试等待（毫秒）。退避算出的值会被 retry_min_wait_ms 兜底抬高。"""

    def eff(field: str, default: int) -> int:
        up = upstreams.get_upstream(str((key or {}).get("upstream_id") or ""))
        v = int((up or {}).get(field) or 0)
        return v if v > 0 else default

    base = max(0, eff("retry_backoff_base_ms", _cfgint(cfg, "retry_backoff_base_ms", 500)))
    cap = max(base, eff("retry_backoff_max_ms", _cfgint(cfg, "retry_backoff_max_ms", 8000)))
    target = min(cap, base * (2 ** max(0, attempt - 1)))
    wait = random.randint((target + 1) // 2, target) if target > 0 else 0
    # 最小重试等待：用户可强制“一定要等够多久才重试”
    min_wait = max(0, eff("retry_min_wait_ms", _cfgint(cfg, "retry_min_wait_ms", 0)))
    return max(wait, min_wait)


def _full_usage(u: Any) -> dict:
    """补全 total_tokens。OpenAI 规范要求该字段，New-API 等严格客户端靠它计费，
    缺失会被判为「渠道测试失败」而不管 HTTP 状态码是 200。"""
    if not isinstance(u, dict):
        u = {}
    out = dict(u)
    p = int(u.get("prompt_tokens") or 0)
    c = int(u.get("completion_tokens") or 0)
    out["prompt_tokens"] = p
    out["completion_tokens"] = c
    if out.get("total_tokens") is None:
        out["total_tokens"] = p + c
    return out


def build_usage(res: dict, req_body: str) -> dict:
    if isinstance(res.get("usage"), dict) and res["usage"].get("prompt_tokens") is not None:
        return _full_usage(res["usage"])
    if not res.get("streamed"):
        try:
            j = json.loads(res.get("body") or "")
            if isinstance(j.get("usage"), dict) and j["usage"].get("prompt_tokens") is not None:
                return _full_usage(j["usage"])
        except Exception:
            pass
    return _full_usage(
        {
            "prompt_tokens": -(-len(req_body) // 3),
            "completion_tokens": estimate_output_tokens(int(res.get("out_bytes") or 0)),
        }
    )


async def log_session(
    cfg: dict, model: str, key_email: str, req_messages: Any, resp_content: str, status: int, ip: str
) -> None:
    """记录完整会话内容（审计用），只保留最近 N 条。

    走异步更新：会话日志是审计用途且只保留最近 N 条，没必要为它在事件循环线程里
    同步抢存储锁 —— 那会和取号/释放的线程锁互相阻塞，高并发下把请求串行化。
    """
    max_n = max(0, _cfgint(cfg, "session_log_max", 100))
    if max_n == 0:
        return

    def _fn(db: dict):
        sessions = db.setdefault("sessions", [])
        sessions.insert(
            0,
            {
                "t": int(time.time()),
                "model": model[:80],
                "key": mask_email(key_email)[:40],
                "status": status,
                "req": json.dumps(req_messages, ensure_ascii=False, default=str)[:2000],
                "resp": resp_content[:2000],
            },
        )
        del sessions[max_n:]

    await STORE.aupdate(_fn)


async def _extract_conv_log(cfg: dict, model: str, email: str, req: dict, chat: dict, ip: str) -> None:
    """从 chat 响应中提取文本并记录会话。"""
    msg = (chat.get("choices") or [{}])[0].get("message") or {}
    resp_text = msg.get("content") or ""
    if isinstance(req.get("input"), str):
        req_msgs = [{"role": "user", "content": req["input"]}]
    elif isinstance(req.get("messages"), list):
        req_msgs = req["messages"]
    elif isinstance(req.get("input"), list):
        req_msgs = [
            {"role": i.get("role", "user"), "content": flatten_content(i.get("content"))}
            for i in req["input"]
            if isinstance(i, dict)
        ]
    else:
        req_msgs = [{"role": "user", "content": str(req.get("input") or "")}]
    await log_session(cfg, model, email, req_msgs, resp_text, 200, ip)


def _conn_reason(e: Exception) -> str:
    """把连接层异常翻译成人能看懂的原因并附上根因。

    日志里光有「上游返回 HTTP 0」没法定位：可能是 DNS 挂了、出网被拦、TLS 失败，
    也可能是连接池被泄漏的连接占满。把异常类型、__cause__ 与判断结论一起写进去。
    """
    cause = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
    txt = f"{type(e).__name__}: {e}"
    if cause is not None:
        txt += f" | 根因: {type(cause).__name__}: {cause}"
    low = txt.lower()
    if any(
        k in low
        for k in (
            "getaddrinfo",
            "nodename",
            "name resolution",
            "name or service not known",
            "dns",
            "temporary failure",
        )
    ):
        hint = "（DNS 解析失败：无法解析上游域名）"
    elif isinstance(e, httpx.PoolTimeout):
        hint = "（连接池耗尽：上游连接未被释放，检查连接泄漏）"
    elif any(k in low for k in ("ssl", "certificate", "tls", "handshake")):
        hint = "（TLS 握手失败：证书/时间/出网被拦）"
    elif isinstance(e, httpx.ConnectTimeout):
        hint = "（TCP 连接超时：出网被阻断或上游不可达）"
    elif isinstance(e, httpx.ReadTimeout):
        hint = "（读取超时：已连上但上游迟迟不返回）"
    else:
        hint = ""
    return (txt + hint)[:300]


def _upstream_fail(key: dict | None, res: dict | None, cfg: dict, anthropic: bool = False) -> JSONResponse:
    status = int((res or {}).get("status") or 0)
    code = status if status >= 400 else 502
    hide = bool(cfg.get("hide_upstream_errors", True))
    if key:
        hide = upstreams.flag_for(str(key.get("upstream_id") or ""), "hide_errors", hide)
    if not hide:
        body = str((res or {}).get("body") or "").strip()
        if status >= 400 and body:
            return Response(content=body, status_code=status, media_type="application/json")
        msg = str((res or {}).get("error") or "") or "上游请求失败"
        return _error(code, msg, "upstream_error", anthropic=anthropic)
    # rstatus=0（连接层异常）以前统一落到「渠道暂不可用」，看不出是超时还是连不上。
    # 按错误分级给出具体原因，客户端和管理员都能据此判断。
    cls = pool._classify(status, 0, str((res or {}).get("error") or ""))
    specific = {"timeout": "上游响应超时，请稍后重试", "conn": "无法连接上游，请稍后重试"}.get(cls)
    msg = specific or {
        "429": "渠道限流，请稍后重试",
        "401": "渠道鉴权失败，请联系管理员",
        "403": "渠道鉴权失败，请联系管理员",
        "404": "渠道不支持该请求或模型",
        "400": "渠道拒绝了请求参数，原因见管理后台日志",
    }.get(str(code), "渠道暂不可用，请稍后重试" if code >= 500 else f"请求未被渠道接受，HTTP {code}，原因见管理后台日志")
    return _error(code, msg, "upstream_error", f"upstream_{code}", anthropic=anthropic)


# ============================================================ /v1 网关


@app.options("/v1/{rest:path}")
async def v1_options(rest: str) -> Response:
    return Response(status_code=204, headers=_cors())


@app.get("/v1/models")
async def v1_models(request: Request):
    cfg = cfg_all()
    entry = _token_entry(request, cfg)
    if _has_auth(cfg) and entry is None:
        return _error(401, "访问令牌无效", "invalid_request_error", "invalid_api_key")
    out = [
        {"id": m, "object": "model", "created": 0, "owned_by": "gateway"}
        for m in gateway_model_ids()
        if ModelPolicy.allowed(m, entry, cfg)
    ]
    return JSONResponse({"object": "list", "data": out}, headers=_cors())


def gateway_model_ids() -> list[str]:
    """网关对外支持的模型清单。

    只由渠道配置决定：各启用渠道显式配置的 models 白名单 + model_map 的客户端别名。
    刻意不访问上游 —— 上游真实模型清单对网关使用者没有意义（其中大部分并未被渠道
    放行），而且每次拉取都要占用一个账号额度，还引入缓存、TTL 与超时一整套开销。
    渠道都没配白名单时无从枚举，退回 model_map 别名 / 内置清单兜底。
    """
    names = upstreams.curated_models()
    if names:
        return names
    aliases: list[str] = []
    for u in upstreams.all_upstreams():
        if not u.get("enabled"):
            continue
        for k in u.get("model_map") or {}:
            if k not in aliases:
                aliases.append(k)
    return aliases or list(FALLBACK_MODELS)


def _release_log(
    ep: str,
    model: str,
    status: int,
    ms: int,
    err: str,
    attempt: int,
    key: dict | None,
    ip: str,
    up_model: str = "",
    stream: bool = False,
    ttfb_ms: int = 0,
    in_tok: int = 0,
    out_tok: int = 0,
) -> dict:
    return {
        "t": int(time.time()),
        "ep": ep,
        "model": model,
        "key": mask_email(str(key.get("email"))) if key else "-",
        "st": status,
        "ms": ms,
        "err": err[:140],
        "ip": ip,
        "att": attempt,
        "up_model": up_model,
        "stream": stream,
        "ttfb": ttfb_ms,
        "in_tok": in_tok,
        "out_tok": out_tok,
    }


# ============================================================ 透传端点


async def _proxy(request: Request, endpoint: str, ep_tag: str) -> JSONResponse | Response:
    cfg = cfg_all()
    entry = _token_entry(request, cfg)
    if _has_auth(cfg) and entry is None:
        return _error(
            401, "访问令牌无效。请在后台「系统设置」中配置访问令牌，并以 Authorization: Bearer <令牌> 调用。"
        )
    body_text = (await request.body()).decode("utf-8", "replace")
    if len(body_text) > MAX_BODY:
        return _error(413, "请求体过大，上限 20MB")
    try:
        req = json.loads(body_text)
    except Exception:
        return _error(400, "请求体不是合法 JSON")
    if not isinstance(req, dict):
        return _error(400, "请求体不是合法 JSON")
    model = str(req.get("model") or "")
    stream = bool(req.get("stream"))
    est = estimate_request_tokens(body_text, req)
    bad = _check_model(model, entry, cfg)
    if bad:
        return bad
    max_attempts = max(1, _cfgint(cfg, "max_retries", 3) + 1)
    ip = _client_ip(request)
    attempt = 0
    last: dict | None = None
    last_key: dict | None = None
    downgraded = False
    reuse_key: dict | None = None  # 同号重试：复用上一个账号，不走取号（不惩罚账号）
    # 本请求当前持有的账号：正常释放/交接后置空；异常时由 finally 兜底释放
    hold: dict = {"key": None}
    up_model = model
    t0 = time.time()
    same_key_tried: set = set()  # 已做过同号重试的账号 id
    rl_left = max(1, _cfgint(cfg, "max_retries", 2))  # 429 额外重试预算

    try:
        while attempt < max_attempts:
            attempt += 1
            if reuse_key is not None:
                taken = {"ok": True, "key": reuse_key, "status": 0, "message": ""}
                reuse_key = None
            else:
                taken = await take_account(request, ep_tag, model, est, cfg)
            if not taken["ok"]:
                await pool.arelease(
                    "",
                    False,
                    taken["status"],
                    taken["message"],
                    None,
                    _release_log(
                        ep_tag,
                        model,
                        taken["status"],
                        0,
                        taken["message"],
                        attempt,
                        None,
                        ip,
                        up_model=model,
                        stream=stream,
                    ),
                )
                return _error(taken["status"], taken["message"])
            key = taken["key"]
            hold["key"] = key
            max_attempts = max(
                attempt,
                max(1, upstreams.override_for(key, "max_retries", _cfgint(cfg, "max_retries", 3)) + 1),
            )
            up_model = upstreams.map_model_for(key, model)
            # 每次尝试都重写为当前渠道的映射名：换渠道重试时若新渠道无映射，自动回退为原始名
            req["model"] = up_model
            req = upstreams.apply_param_overrides(key, model, req)
            # 客户端可能传字符串数字/布尔（如 "temperature":"0.95"），统一归一化为 JSON 原生类型
            convert.normalize_body_types(req)
            body = json.dumps(req, ensure_ascii=False, separators=(",", ":"))
            t0 = time.time()
            rerr = ""
            rstatus = 0
            ctype = ""
            rbody = ""
            client = get_http(cfg)
            timeout = httpx.Timeout(
                connect=upstreams.override_for(key, "connect_timeout", int(cfg.get("connect_timeout") or 10)),
                read=upstreams.override_for(key, "request_timeout", int(cfg.get("request_timeout") or 300)),
                write=30,
                pool=10,
            )
            r: httpx.Response | None = None
            _hdr = {
                "Accept": "text/event-stream" if stream else "application/json",
                "Authorization": "Bearer " + key["apikey"],
            }
            _url = upstreams.base_for(key) + "/" + endpoint
            try:
                if stream:
                    # 关键：必须 stream=True 才是真流式；client.post() 会把整个响应体读完再返回，
                    # 导致首字节=总耗时（长响应直接撞超时、下游毫无实时性）
                    _req = client.build_request(
                        "POST", _url, content=body.encode(), headers=_hdr, timeout=timeout
                    )
                    # 上游响应头可能几十秒才返回（推理模型实测 128s），这段时间网关卡在 send() 里
                    # 什么都发不出去，中间代理会因长时间无数据先掐断连接（客户端只看到失败）。
                    # 因此限时等一小段；等不到就转入「边发心跳边等响应头」的兜底流。
                    # shield 保证超时不会取消上游请求本身，兜底流可以继续等它。
                    send_task = asyncio.create_task(client.send(_req, stream=True))
                    _ttfb_cfg = float(int(cfg.get("ttfb_timeout") or 0))
                    commit_after = 12.0 if _ttfb_cfg <= 0 else min(12.0, _ttfb_cfg)
                    # 分片等待响应头：期间轮询客户端断连。断连则取消上游请求并释放账号，
                    # 否则为死连接白占账号（配合 acct_concurrency 会把账号卡死）。
                    r = None
                    waited = 0.0
                    while waited < commit_after:
                        try:
                            if await request.is_disconnected():
                                send_task.cancel()
                                try:
                                    ms = int((time.time() - t0) * 1000)
                                    await pool.arelease(
                                        key["id"],
                                        True,
                                        499,
                                        "客户端已断开",
                                        {"prompt_tokens": -(-len(body) // 3), "completion_tokens": 0},
                                        _release_log(
                                            ep_tag,
                                            model,
                                            499,
                                            ms,
                                            "客户端已断开",
                                            attempt,
                                            key,
                                            ip,
                                            up_model=up_model,
                                            stream=True,
                                            ttfb_ms=ms,
                                        ),
                                    )
                                except Exception:
                                    pass
                                return Response(status_code=499)
                        except Exception:
                            pass
                        try:
                            r = await asyncio.wait_for(asyncio.shield(send_task), timeout=0.5)
                            break
                        except asyncio.TimeoutError:
                            waited += 0.5
                    if r is None:
                        return _slow_stream_response(
                            send_task,
                            client,
                            key,
                            ep_tag,
                            model,
                            up_model,
                            body,
                            ip,
                            attempt,
                            t0,
                            request,
                            _ttfb_cfg,
                        )
                else:
                    r = await client.post(_url, content=body.encode(), headers=_hdr, timeout=timeout)
                rstatus = r.status_code
                ctype = r.headers.get("content-type", "")
            except (httpx.HTTPError, OSError) as e:
                rerr = _conn_reason(e)
                rstatus = 0
            if stream and 200 <= rstatus < 400:
                # 流式请求：只读第一个 chunk 判断是否为 SSE 错误事件（错误事件通常很小）。
                # 是错误则降级重试，不是则把首帧传给 _proxy_stream 正常透传。
                if r is not None:
                    ait = r.aiter_bytes()
                    # 独立读取任务：外层预读超时时不会取消到上游读取本身，
                    # 之后 _proxy_stream 复用同一个队列继续取数据。
                    sp = _StreamPump(ait)
                    first_chunk = b""
                    ttfb_to = int(cfg.get("ttfb_timeout") or 0)
                    # 预读首帧只为「识别立即返回的 SSE 错误事件」以便换号/降级重试。
                    # 因此只等一小段：上游推理慢时立刻转入心跳透传，绝不能把客户端
                    # 干等几十秒（代理空闲超时会先断开，客户端只看到失败）。
                    pre_to = 8.0 if ttfb_to <= 0 else min(8.0, float(ttfb_to))
                    try:
                        item = await asyncio.wait_for(sp.q.get(), timeout=pre_to)
                        if item is not sp.done:
                            first_chunk = item
                    except asyncio.TimeoutError:
                        # 上游迟迟不吐数据：已无法再改状态码，转为「带心跳的流式透传」，
                        # 让连接保活到上游真正开始输出为止。
                        return _proxy_stream(
                            client,
                            r,
                            key,
                            ep_tag,
                            model,
                            up_model,
                            body,
                            ip,
                            attempt,
                            t0,
                            rstatus,
                            ctype,
                            first_chunk=b"",
                            ait=ait,
                            request=request,
                            heartbeat=True,
                            ttfb_deadline=float(ttfb_to),
                            pump=sp,
                        )
                    except (httpx.HTTPError, OSError) as e:
                        rerr = _conn_reason(e)
                    text = first_chunk.decode("utf-8", "replace")
                    is_sse_error = text.lstrip().startswith(("event: error", 'data: {"error"')) or (
                        '"error"' in text[:500]
                        and ("thinking" in text.lower() or "unsupported" in text.lower())
                    )
                    # 空流保护：上游 200 但流为空/无内容 → 视为失败，避免下游收到空
                    is_empty_stream = not text.strip()
                    if is_sse_error and not downgraded:
                        downgraded = True
                        rstatus = 400
                        tdefs = convert.parse_thinking_defaults(
                            upstreams.upstream_value(key, "thinking_defaults", "")
                        )
                        if convert.downgrade_thinking(req, up_model, tdefs):
                            sp.task.cancel()
                            await r.aclose()
                            body = json.dumps(req, ensure_ascii=False, separators=(",", ":"))
                            continue
                        rbody = text
                        rstatus = 400
                    elif is_empty_stream:
                        # 空流：标记 502 走错误路径（换号重试；尝试耗尽后返回 502），
                        # 绝不给下游透传空流
                        rstatus = 502
                        rerr = "上游返回空流"
                        rbody = ""
                    else:
                        return _proxy_stream(
                            client,
                            r,
                            key,
                            ep_tag,
                            model,
                            up_model,
                            body,
                            ip,
                            attempt,
                            t0,
                            rstatus,
                            ctype,
                            first_chunk=first_chunk,
                            ait=ait,
                            request=request,
                            pump=sp,
                        )
            rbody = ""
            if r is not None:
                if stream:
                    # 流式响应没能交接给透传（空流 / SSE 错误且降级失败等），必须显式关闭：
                    # 否则这条上游连接会一直被占住，反复失败会耗尽共享连接池，
                    # 之后所有上游请求都卡在「等连接」直到超时 —— 日志表现为清一色
                    # 「上游返回 HTTP 0」（10s = pool/connect 超时）。同时这里也不能读
                    # r.text：对流式响应读全文会把整条 SSE 拖进来，甚至挂住。
                    try:
                        await r.aclose()
                    except Exception:
                        pass
                else:
                    try:
                        rbody = r.text
                    except Exception:
                        pass
            ms = int((time.time() - t0) * 1000)
            success = 200 <= rstatus < 400
            # 空响应/伪成功保护：上游 200 但 body 为空、或 body 里带 error 字段，
            # 视为失败（避免下游收到空内容或把错误当成功透传）
            if success:
                _st = rbody.strip()
                if not _st or _st == "{}":
                    success = False
                    rerr = "上游返回空响应"
                    rstatus = 502
                elif _st.startswith("{") and '"error"' in _st[:200]:
                    try:
                        _j = json.loads(_st)
                        if isinstance(_j, dict) and _j.get("error"):
                            success = False
                            rerr = upstream_snippet({"status": 200, "body": _st, "error": ""})
                            rstatus = 502
                    except Exception:
                        pass
            err = "" if success else upstream_snippet({"status": rstatus, "body": rbody, "error": rerr})
            # 瞬态错误（连接失败/超时/5xx/空响应）先同号快速重试一次：
            # 上游抖动往往重试即成功，避免误判账号故障而冷却/封禁。只重试一次且不 release（不惩罚）。
            if (
                not success
                and key["id"] not in same_key_tried
                and (rstatus == 0 or rstatus >= 500)
                and attempt < max_attempts
            ):
                same_key_tried.add(key["id"])
                reuse_key = key
                await asyncio.sleep(_backoff_ms(cfg, attempt, key) / 1000)
                continue
            usage = build_usage(
                {"status": rstatus, "body": rbody, "streamed": False, "out_bytes": 0, "usage": None}, body
            )
            hold["key"] = None  # 已正常释放
            await pool.arelease(
                key["id"],
                success,
                rstatus,
                err,
                usage,
                _release_log(
                    ep_tag,
                    model,
                    rstatus,
                    ms,
                    err,
                    attempt,
                    key,
                    ip,
                    up_model=up_model,
                    stream=stream,
                    ttfb_ms=ms,
                    in_tok=int(usage.get("prompt_tokens") or 0),
                    out_tok=int(usage.get("completion_tokens") or 0),
                ),
            )
            if success:
                out_body = rbody
                # 统一改写 model 并补全 usage：部分上游不返回 usage，严格客户端
                # （New-API 渠道测试 / 计费）会因缺字段判定失败，与 HTTP 200 无关。
                try:
                    j = json.loads(rbody)
                    if isinstance(j, dict):
                        if up_model != model:
                            j["model"] = model
                        if not isinstance(j.get("usage"), dict):
                            j["usage"] = _full_usage(usage)
                        else:
                            j["usage"] = _full_usage(j["usage"])
                        out_body = json.dumps(j, ensure_ascii=False, separators=(",", ":"))
                except Exception:
                    pass
                try:
                    resp_msg = json.loads(out_body)
                    resp_text = (resp_msg.get("choices") or [{}])[0].get("message", {}).get("content", "")
                    await log_session(
                        cfg, model, key["email"], req.get("messages", []), resp_text, rstatus, ip
                    )
                except Exception:
                    pass
                if stream:
                    pass  # 流式已在上方 return
                # 非流式一律声明 application/json：不能原样转发上游的 Content-Type，
                # 否则上游给出异常头时严格客户端会按错的格式解析。
                return Response(content=out_body, status_code=rstatus, media_type="application/json")
            last = {"status": rstatus, "body": rbody, "error": rerr, "content_type": ctype}
            last_key = key
            # 401/403 为账号级鉴权失败：立即返回（账号已被硬封禁，重试只会得到 429 封禁掩码）
            if rstatus in (401, 403):
                return _upstream_fail(key, last, cfg)
            # 400 且报思考参数不兼容：自动降级（去除或改用默认强度）后原渠道重试一次
            if rstatus == 400 and convert.thinking_unsupported(rbody, rstatus) and not downgraded:
                downgraded = True
                tdefs = convert.parse_thinking_defaults(
                    upstreams.upstream_value(key, "thinking_defaults", "")
                )
                if convert.downgrade_thinking(req, up_model, tdefs):
                    body = json.dumps(req, ensure_ascii=False, separators=(",", ":"))
                    continue
            # 400 且报 JSON 反序列化/类型错误（字符串数字被上游拒绝）：激进转换后重试一次
            if rstatus == 400 and convert.is_deserialize_error(rbody, rstatus) and not downgraded:
                downgraded = True
                if convert.coerce_all_types(req):
                    body = json.dumps(req, ensure_ascii=False, separators=(",", ":"))
                    continue
            # 400 且报「不支持的参数」（如 enable_thinking）：移除被点名参数后重试一次
            if rstatus == 400 and convert.is_unsupported_param_error(rbody, rstatus) and not downgraded:
                downgraded = True
                if convert.strip_unsupported_params(req, rbody):
                    body = json.dumps(req, ensure_ascii=False, separators=(",", ":"))
                    continue
            # 渠道级不可用（no available channel）：同一渠道所有账号共享渠道池，
            # 换号/重试都注定失败 → 快速失败，不浪费重试
            if convert.is_channel_exhausted(rbody):
                return _upstream_fail(key, last, cfg)
            if not (rstatus in (0, 429) or rstatus >= 500):
                return _upstream_fail(key, last, cfg)
            # 429 吸收：上游限流是暂时的，延长重试预算让它走排队等账号冷却，
            # 尽量不把 429 透传给下游（下游 429 往往直接失败或降级）
            if rstatus == 429 and rl_left > 0 and cfg.get("queue_enabled", True):
                rl_left -= 1
                max_attempts += 1
                await asyncio.sleep(_backoff_ms(cfg, attempt, key) / 1000)
                continue
            await asyncio.sleep(_backoff_ms(cfg, attempt, key) / 1000)
    finally:
        # 异常兜底：任何没走到正常释放的路径（非 httpx 异常、序列化失败、
        # 上游地址非法等）都必须把账号还回去，否则会被永久锁定在这个请求上。
        k_held = hold["key"]
        if k_held is not None:
            hold["key"] = None
            # 把真实异常写进日志：只记一句固定文案，等于查不到原因
            _exc = sys.exc_info()[1]
            _note = "请求处理异常，兜底释放账号：%s" % (
                ("%s: %s" % (type(_exc).__name__, _exc))[:180] if _exc else "未走到正常释放"
            )
            try:
                await pool.arelease(
                    k_held["id"],
                    True,
                    500,
                    _note,
                    None,
                    _release_log(
                        ep_tag,
                        model,
                        500,
                        int((time.time() - t0) * 1000),
                        _note,
                        attempt,
                        k_held,
                        ip,
                        up_model=up_model,
                        stream=stream,
                    ),
                )
            except Exception:
                pass
    return _upstream_fail(last_key, last, cfg)


# 排队退避上限与在排队人数上限。
# 等待者必须抖动 + 指数退避：否则几百个等待者会在同一时刻一起去抢存储锁
# （每次 acquire 都要遍历整个号池），把锁打满、连正在服务的请求也拖慢 ——
# 结果是越等越慢，账号空出来反而抢不到。排队过长则直接快速失败，避免无限堆积。
QUEUE_POLL_MAX = 3.0
QUEUE_MAX_WAITING = 200
_waiting: dict[str, int] = {}


class _StreamPump:
    """独立任务读取上游 SSE。

    关键：绝不能用 asyncio.wait_for 直接等 ait.__anext__()——超时会取消该协程，
    把 httpx 的流打断，之后再迭代 ait 会立刻结束（表现为「发完心跳流就没了」）。
    """

    def __init__(self, ait) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.done = object()
        self.error = ""  # 上游中断原因（连接被掐断/读超时等），供下游判断截断
        self._ait = ait
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            async for ch in self._ait:
                await self.q.put(ch)
        except Exception as e:
            # 不能静默吞掉：上游中途断流若被当作正常结束，下游会把截断内容当完整回复
            self.error = f"{type(e).__name__}: {e}"
        finally:
            await self.q.put(self.done)


def _proxy_stream(
    client: httpx.AsyncClient,
    r: httpx.Response,
    key: dict,
    ep_tag: str,
    model: str,
    up_model: str,
    body: str,
    ip: str,
    attempt: int,
    t0: float,
    status: int,
    ctype: str,
    first_chunk: bytes = b"",
    ait=None,
    request: Request | None = None,
    heartbeat: bool = False,
    ttfb_deadline: float = 0.0,
    pump: "_StreamPump | None" = None,
) -> StreamingResponse:
    """SSE 透传：模型改名回写 + 心跳保活 + 断连检测 + 流结束后统计释放。

    first_chunk/ait 为外层预读的首帧和迭代器；heartbeat=True 表示上游首字节太慢，
    已改为「先发注释帧占住连接」的透传模式（此时放弃换号重试机会）。
    """
    rewrite = up_model != model
    up_bytes = 0
    buf = b""
    model_re = re.compile(rb'"model"\s*:\s*"[^"]*"')
    model_to = b'"model":"' + model.encode() + b'"'
    first_chunk_at = 0.0
    idle_to = int(STORE.load()["config"].get("sse_idle_timeout") or 0)
    # 心跳间隔必须远小于中间代理的空闲超时（nginx proxy_read_timeout 默认 60s），
    # 否则上游推理期间长时间无数据，代理会先断开，客户端只看到失败。
    HB = 10.0
    # 首字节前的等待上限用 ttfb_timeout（推理模型可能几十秒才吐第一个字），
    # 未设置则退回 sse_idle_timeout，都没有则 300s 兜底，避免无限挂起。
    byte_limit = ttfb_deadline if ttfb_deadline > 0 else (float(idle_to) if idle_to > 0 else 300.0)

    async def gen() -> AsyncGenerator[bytes, None]:
        nonlocal up_bytes, buf, first_chunk_at
        # 复用外层已启动的读取任务（否则对同一 ait 二次迭代必然立刻结束）。
        # 注意用独立局部名：在嵌套函数里给 pump 赋值会把它变成局部变量，
        # 之后 `pump is None` 这个读取会抛 UnboundLocalError（响应头已发出 → 客户端只看到截断）。
        p = pump if pump is not None else _StreamPump(ait if ait is not None else r.aiter_bytes())
        q, sentinel, task = p.q, p.done, p.task
        idle = 0.0
        started = False
        outcome = ""
        truncated = ""  # 已开始下发后上游异常收尾（断流/空闲超时），需显式告知下游
        try:
            # 先发外层预读的首帧
            if first_chunk:
                up_bytes += len(first_chunk)
                first_chunk_at = time.time()
                started = True
                if rewrite:
                    buf = first_chunk
                    cut = buf.rfind(b"\n")
                    if cut != -1:
                        out, buf = buf[: cut + 1], buf[cut + 1 :]
                        yield model_re.sub(model_to, out)
                else:
                    yield first_chunk
            if heartbeat:
                yield _keepalive_frame(ep_tag, model, first=True)
            while True:
                # 客户端已经走了就别再占着账号和上游连接
                if request is not None:
                    try:
                        if await request.is_disconnected():
                            outcome = "客户端已断开"
                            break
                    except Exception:
                        pass
                try:
                    item = await asyncio.wait_for(q.get(), timeout=HB)
                except asyncio.TimeoutError:
                    idle += HB
                    # 首字节前按 ttfb_timeout 等（推理模型慢），之后按 sse_idle_timeout
                    limit = float(idle_to) if started else byte_limit
                    if limit > 0 and idle >= limit:
                        if not started:
                            outcome = f"上游首字节超时（{int(limit)}s）"
                        else:
                            truncated = f"上游空闲超时（{int(limit)}s）"
                        break
                    yield _keepalive_frame(ep_tag, model)
                    continue
                if item is sentinel:
                    if p.error and started:
                        truncated = f"上游流中断：{p.error}"
                    break
                idle = 0.0
                started = True
                up_bytes += len(item)
                if not first_chunk_at:
                    first_chunk_at = time.time()
                if not rewrite:
                    yield item
                    continue
                buf += item
                # 只改写完整行（model 字段不会跨行），避免匹配被分块截断
                cut = buf.rfind(b"\n")
                if cut == -1:
                    continue
                out, buf = buf[: cut + 1], buf[cut + 1 :]
                yield model_re.sub(model_to, out)
            # 上游异常收尾：以下发 error 事件 + [DONE] 收口，让下游 SDK 识别到截断，
            # 而不是把半截内容当成完整回复（静默截断比显式报错危险得多）
            if truncated and not outcome:
                yield _sse_error_event(truncated)
        except GeneratorExit:
            outcome = "客户端已断开"
            raise
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            if rewrite and buf and not outcome:
                yield model_re.sub(model_to, buf)
            ms = int((time.time() - t0) * 1000)
            ttfb = int((first_chunk_at - t0) * 1000) if first_chunk_at else ms
            usage = {
                "prompt_tokens": -(-len(body) // 3),
                "completion_tokens": estimate_output_tokens(up_bytes),
            }
            st = 499 if outcome == "客户端已断开" else status
            note = outcome or truncated  # 截断原因同样要落进日志，便于排查上游可用性
            await pool.arelease(
                key["id"],
                True,
                st,
                note,
                usage,
                _release_log(
                    ep_tag,
                    model,
                    st,
                    ms,
                    note,
                    attempt,
                    key,
                    ip,
                    up_model=up_model,
                    stream=True,
                    ttfb_ms=ttfb,
                    in_tok=int(usage["prompt_tokens"]),
                    out_tok=int(usage["completion_tokens"]),
                ),
            )
            try:
                await r.aclose()  # stream=True 必须显式关闭，否则连接泄漏
            except Exception:
                pass

    # 流式固定声明 SSE：OpenAI 流式响应必须是 text/event-stream，
    # 上游偶尔用 application/json 声明 SSE 体，原样转发会让客户端按 JSON 解析而失败。
    return StreamingResponse(gen(), media_type="text/event-stream", headers=_cors())


def _keepalive_frame(ep_tag: str, model: str, first: bool = False) -> bytes:
    """保活帧：必须是合法的 data: 事件，不能用 SSE 注释行（`: ping`）。

    中转网关（New-API / one-api 等）的流扫描器只处理 `data:` 行，注释行会被丢弃、
    不转发给下游。于是「New-API → 客户端」这一段仍然长时间静默，被中间的 nginx
    按空闲超时（proxy_read_timeout 默认 60s）掐断，客户端侧就表现为 client_gone。
    发一个空 delta 的合法 chunk，保活才能穿透整条链路。
    """
    created = int(time.time())
    if ep_tag == "cmpl":
        obj = {
            "id": "cmpl-keepalive",
            "object": "text_completion",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "text": "", "finish_reason": None}],
        }
    else:
        obj = {
            "id": "chatcmpl-keepalive",
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [
                {"index": 0, "delta": ({"role": "assistant"} if first else {}), "finish_reason": None}
            ],
        }
    return b"data: " + json.dumps(obj, ensure_ascii=False).encode() + b"\n\n"


def _sse_error_event(msg: str) -> bytes:
    """流内错误事件：响应头已发出（200）后无法再改状态码，只能以 SSE error 事件告知下游。"""
    return (
        b"data: "
        + json.dumps({"error": {"message": msg, "type": "upstream_error"}}, ensure_ascii=False).encode()
        + b"\n\ndata: [DONE]\n\n"
    )


def _slow_stream_response(
    send_task,
    client: httpx.AsyncClient,
    key: dict,
    ep_tag: str,
    model: str,
    up_model: str,
    body: str,
    ip: str,
    attempt: int,
    t0: float,
    request: Request | None,
    ttfb_deadline: float,
) -> StreamingResponse:
    """上游响应头迟迟不返回时的兜底流：先提交 200 并持续发心跳占住连接，拿到响应头后顺势透传。

    上游慢（推理模型实测 128s 才返回响应头）时，网关卡在 client.send() 里什么都发不出去，
    中间的 nginx（proxy_read_timeout 默认 60s）会因长时间无数据先掐断连接 —— 客户端只看到
    「请求失败」，而网关日志却记着 200。这里用心跳消除那段静默。

    代价：响应头一旦发出就不能再改状态码、也无法换号重试，因此上游报错改为流内 error 事件
    （客户端仍能识别为失败），但账号仍按真实状态码释放，冷却/封禁逻辑不受影响。
    """
    HB = 10.0
    deadline = ttfb_deadline if ttfb_deadline > 0 else 300.0

    def _est() -> dict:
        return {"prompt_tokens": -(-len(body) // 3), "completion_tokens": 0}

    async def gen() -> AsyncGenerator[bytes, None]:
        r: httpx.Response | None = None
        outcome = ""
        released = False
        handed = False
        inner_it = None

        async def _fail(status: int, err: str) -> None:
            # 失败释放：记入冷却/封禁，仍按真实状态码归类
            nonlocal released
            released = True
            ms = int((time.time() - t0) * 1000)
            await pool.arelease(
                key["id"],
                False,
                status,
                err,
                _est(),
                _release_log(
                    ep_tag,
                    model,
                    status,
                    ms,
                    err,
                    attempt,
                    key,
                    ip,
                    up_model=up_model,
                    stream=True,
                    ttfb_ms=ms,
                ),
            )

        try:
            # 进入兜底流时已经静默了 commit_after 秒，立刻先发一个心跳占住连接，
            # 不能等满一个 HB 间隔（否则代理仍有被掐断的窗口）
            yield _keepalive_frame(ep_tag, model, first=True)
            waited = 0.0
            # 阶段一：等响应头，期间每 HB 秒发一个心跳帧
            while r is None:
                if request is not None:
                    try:
                        if await request.is_disconnected():
                            outcome = "客户端已断开"
                            break
                    except Exception:
                        pass
                try:
                    r = await asyncio.wait_for(asyncio.shield(send_task), timeout=HB)
                except asyncio.TimeoutError:
                    waited += HB
                    if waited >= deadline:
                        outcome = f"上游首字节超时（{int(deadline)}s）"
                        break
                    yield _keepalive_frame(ep_tag, model)
                except (httpx.HTTPError, OSError) as e:
                    outcome = str(e)
                    break
            if r is None:
                # 失败/超时：断连时连接已关闭，无需再发错误帧
                if outcome != "客户端已断开":
                    yield _sse_error_event(outcome or "上游无响应")
                await _fail(499 if outcome == "客户端已断开" else 504, outcome or "上游无响应")
                return
            status = r.status_code
            ctype = r.headers.get("content-type", "")
            if not (200 <= status < 400):
                # 上游报错：读错误体 -> 流内报错，并按真实状态码释放（仍触发冷却/封禁）
                try:
                    raw = await r.aread()
                    err = upstream_snippet(
                        {"status": status, "body": raw.decode("utf-8", "replace"), "error": ""}
                    )
                except Exception as e:
                    err = str(e)
                yield _sse_error_event(err or f"上游返回 HTTP {status}")
                await _fail(status, err)
                try:
                    await r.aclose()
                except Exception:
                    pass
                return
            # 阶段二：2xx，交给 _proxy_stream 正常透传（复用改名/心跳/统计/释放逻辑）
            inner = _proxy_stream(
                client,
                r,
                key,
                ep_tag,
                model,
                up_model,
                body,
                ip,
                attempt,
                t0,
                status,
                ctype,
                first_chunk=b"",
                ait=None,
                request=request,
                heartbeat=False,
                ttfb_deadline=deadline,
            )
            inner_it = inner.body_iterator
            handed = True  # 账号释放移交给 _proxy_stream
            try:
                async for chunk in inner_it:
                    yield chunk
            finally:
                # 外层被关闭（断连）时，内层生成器不会自动结束，必须显式关闭它，
                # 否则 _proxy_stream 的 finally（释放账号的那一段）永远不执行 → 账号泄漏。
                try:
                    await inner_it.aclose()
                except Exception:
                    pass
        except GeneratorExit:
            outcome = "客户端已断开"
            raise
        finally:
            send_task.cancel()
            if not released and not handed:
                # 兜底：断连/异常等一切未交接、未释放的情况，确保账号被回收，
                # 否则配合 acct_concurrency 会把账号永久卡死在满载。
                ms = int((time.time() - t0) * 1000)
                st = 499 if outcome == "客户端已断开" else 504
                await pool.arelease(
                    key["id"],
                    False,
                    st,
                    outcome,
                    _est(),
                    _release_log(
                        ep_tag,
                        model,
                        st,
                        ms,
                        outcome,
                        attempt,
                        key,
                        ip,
                        up_model=up_model,
                        stream=True,
                        ttfb_ms=ms,
                    ),
                )
            if r is not None:
                try:
                    await r.aclose()
                except Exception:
                    pass

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_cors())


@app.post("/v1/responses")
async def v1_responses(request: Request):
    return await _convert(request, "responses", anthropic=False)


@app.post("/v1/messages")
async def v1_messages(request: Request):
    return await _convert(request, "messages", anthropic=True)


@app.post("/v1/chat/completions")
async def v1_chat_completions(request: Request):
    return await _proxy(request, "chat/completions", "chat")


@app.post("/v1/completions")
async def v1_completions(request: Request):
    return await _proxy(request, "completions", "cmpl")


@app.post("/v1/embeddings")
async def v1_embeddings(request: Request):
    return await _proxy(request, "embeddings", "emb")


async def _convert(request: Request, protocol: str, anthropic: bool):
    """POST /v1/responses、/v1/messages：请求转换 → 上游 chat/completions → 响应转回。"""
    ep = "resp" if protocol == "responses" else "msg"
    cfg = cfg_all()
    entry = _token_entry(request, cfg)
    if _has_auth(cfg) and entry is None:
        if anthropic:
            return _error(401, "invalid x-api-key", anthropic=True)
        return _error(
            401, "访问令牌无效。请在后台「系统设置」中配置访问令牌，并以 Authorization: Bearer <令牌> 调用。"
        )
    body_text = (await request.body()).decode("utf-8", "replace")
    if len(body_text) > MAX_BODY:
        return _error(413, "请求体过大，上限 20MB", anthropic=anthropic)
    try:
        req = json.loads(body_text)
    except Exception:
        return _error(400, "请求体不是合法 JSON", anthropic=anthropic)
    if not isinstance(req, dict):
        return _error(400, "请求体不是合法 JSON", anthropic=anthropic)
    try:
        chat_req = convert.anthropic_to_chat(req) if anthropic else convert.responses_to_chat(req)
    except ValueError as e:
        return _error(400, str(e), anthropic=anthropic)
    model = str(req.get("model") or "")
    stream = bool(req.get("stream"))
    est = estimate_request_tokens(body_text, req)
    meta = {
        "temperature": req.get("temperature"),
        "top_p": req.get("top_p"),
        "max_output_tokens": req.get("max_output_tokens"),
        # reasoning 可能是对象，也可能被客户端简写成字符串（"high"）——
        # 之前直接 .get 会抛 AttributeError 变成 500
        "reasoning_effort": (
            sub_dict(req.get("reasoning")).get("effort")
            or (req.get("reasoning") if isinstance(req.get("reasoning"), str) else None)
        ),
    }
    bad = _check_model(model, entry, cfg, anthropic=anthropic)
    if bad:
        return bad
    max_attempts = max(1, _cfgint(cfg, "max_retries", 3) + 1)
    ip = _client_ip(request)
    attempt = 0
    last: dict | None = None
    last_key: dict | None = None
    downgraded = False
    reuse_key: dict | None = None  # 同号重试
    # 本请求当前持有的账号：正常释放/交接后置空；异常时由 finally 兜底释放
    hold: dict = {"key": None}
    up_model = model
    t0 = time.time()
    same_key_tried: set = set()
    rl_left = max(1, _cfgint(cfg, "max_retries", 2))  # 429 额外重试预算

    try:
        while attempt < max_attempts:
            attempt += 1
            if reuse_key is not None:
                taken = {"ok": True, "key": reuse_key, "status": 0, "message": ""}
                reuse_key = None
            else:
                taken = await take_account(request, ep, model, est, cfg)
            if not taken["ok"]:
                await pool.arelease(
                    "",
                    False,
                    taken["status"],
                    taken["message"],
                    None,
                    _release_log(
                        ep,
                        model,
                        taken["status"],
                        0,
                        taken["message"],
                        attempt,
                        None,
                        ip,
                        up_model=model,
                        stream=stream,
                    ),
                )
                return _error(taken["status"], taken["message"], anthropic=anthropic)
            key = taken["key"]
            hold["key"] = key
            max_attempts = max(
                attempt,
                max(1, upstreams.override_for(key, "max_retries", _cfgint(cfg, "max_retries", 3)) + 1),
            )
            up_model = upstreams.map_model_for(key, model)
            chat_req["model"] = up_model
            chat_req = upstreams.apply_param_overrides(key, model, chat_req)
            # 客户端可能传字符串数字/布尔（如 "temperature":"0.95"），统一归一化为 JSON 原生类型
            convert.normalize_body_types(chat_req)
            raw = json.dumps(chat_req, ensure_ascii=False, separators=(",", ":"))
            t0 = time.time()
            rerr = ""
            rstatus = 0
            ctype = ""
            rbody = ""
            out_bytes = 0
            try:
                timeout = httpx.Timeout(
                    connect=upstreams.override_for(
                        key, "connect_timeout", int(cfg.get("connect_timeout") or 10)
                    ),
                    read=upstreams.override_for(
                        key, "request_timeout", int(cfg.get("request_timeout") or 300)
                    ),
                    write=30,
                    pool=10,
                )
                client = get_http(cfg)
                _hdr2 = {
                    "Accept": "text/event-stream" if stream else "application/json",
                    "Authorization": "Bearer " + key["apikey"],
                }
                _url2 = upstreams.base_for(key) + "/chat/completions"
                if stream:
                    _req2 = client.build_request(
                        "POST", _url2, content=raw.encode(), headers=_hdr2, timeout=timeout
                    )
                    r = await client.send(_req2, stream=True)
                else:
                    r = await client.post(_url2, content=raw.encode(), headers=_hdr2, timeout=timeout)
                rstatus = r.status_code
                ctype = r.headers.get("content-type", "")
                if stream and 200 <= rstatus < 400:
                    # 流式请求：先读第一个 chunk 判断是否为 SSE 错误事件
                    ait = r.aiter_bytes()
                    first_chunk = b""
                    ttfb_to = int(cfg.get("ttfb_timeout") or 0)
                    try:
                        if ttfb_to > 0:
                            first_chunk = await asyncio.wait_for(ait.__anext__(), timeout=ttfb_to)
                        else:
                            first_chunk = await ait.__anext__()
                    except StopAsyncIteration:
                        pass
                    except asyncio.TimeoutError:
                        rerr = f"上游首字节超时（{ttfb_to}s）"
                        rstatus = 504
                    text = first_chunk.decode("utf-8", "replace")
                    is_sse_error = text.lstrip().startswith(("event: error", 'data: {"error"')) or (
                        '"error"' in text[:500]
                        and ("thinking" in text.lower() or "unsupported" in text.lower())
                    )
                    if is_sse_error and not downgraded:
                        downgraded = True
                        rstatus = 400
                        rbody = text
                        tdefs = convert.parse_thinking_defaults(
                            upstreams.upstream_value(key, "thinking_defaults", "")
                        )
                        if convert.downgrade_thinking(chat_req, up_model, tdefs):
                            await r.aclose()
                            raw = json.dumps(chat_req, ensure_ascii=False, separators=(",", ":"))
                            continue
                    elif not text.strip():
                        # 空流保护：上游 200 但流为空 → 标错误走重试，不透传空流
                        rstatus = 502
                        rerr = "上游返回空流"
                    else:
                        return await _stream_convert(
                            request,
                            client,
                            r,
                            key,
                            ep,
                            model,
                            cfg,
                            up_model,
                            raw,
                            ip,
                            attempt,
                            t0,
                            rstatus,
                            ctype,
                            protocol,
                            first_chunk=first_chunk,
                            ait=ait,
                        )
                if stream:
                    # 流式响应没能交接给转换透传（空流 / SSE 错误且降级失败等）必须显式关闭，
                    # 否则连接被占住，反复失败会耗尽共享连接池（日志表现为清一色 HTTP 0）。
                    try:
                        await r.aclose()
                    except Exception:
                        pass
                else:
                    try:
                        rbody = r.text
                    except Exception:
                        rbody = ""
            except (httpx.HTTPError, OSError) as e:
                rerr = _conn_reason(e)
                rstatus = 0
            ms = int((time.time() - t0) * 1000)
            success = 200 <= rstatus < 400
            # 空响应/伪成功保护：上游 200 但 body 为空、或 body 里带 error 字段，
            # 视为失败（避免下游收到空内容或把错误当成功透传）
            if success:
                _st = rbody.strip()
                if not _st or _st == "{}":
                    success = False
                    rerr = "上游返回空响应"
                    rstatus = 502
                elif _st.startswith("{") and '"error"' in _st[:200]:
                    try:
                        _j = json.loads(_st)
                        if isinstance(_j, dict) and _j.get("error"):
                            success = False
                            rerr = upstream_snippet({"status": 200, "body": _st, "error": ""})
                            rstatus = 502
                    except Exception:
                        pass
            err = "" if success else upstream_snippet({"status": rstatus, "body": rbody, "error": rerr})
            # 瞬态错误先同号快速重试一次（不惩罚账号）
            if (
                not success
                and key["id"] not in same_key_tried
                and (rstatus == 0 or rstatus >= 500)
                and attempt < max_attempts
            ):
                same_key_tried.add(key["id"])
                reuse_key = key
                await asyncio.sleep(_backoff_ms(cfg, attempt, key) / 1000)
                continue
            usage = build_usage(
                {"status": rstatus, "body": rbody, "streamed": False, "out_bytes": out_bytes, "usage": None},
                raw,
            )
            hold["key"] = None  # 已正常释放
            await pool.arelease(
                key["id"],
                success,
                rstatus,
                err,
                usage,
                _release_log(
                    ep,
                    model,
                    rstatus,
                    ms,
                    err,
                    attempt,
                    key,
                    ip,
                    up_model=up_model,
                    stream=stream,
                    ttfb_ms=ms,
                    in_tok=int(usage.get("prompt_tokens") or 0),
                    out_tok=int(usage.get("completion_tokens") or 0),
                ),
            )
            if success:
                try:
                    chat = json.loads(rbody)
                except Exception:
                    chat = None
                if not isinstance(chat, dict) or "choices" not in chat:
                    return _error(502, "上游返回了无法解析的响应", anthropic=anthropic)
                chat["model"] = model
                try:
                    await _extract_conv_log(cfg, model, key["email"], req, chat, ip)
                except Exception:
                    pass
                if anthropic:
                    return Response(
                        content=json.dumps(
                            convert.chat_to_anthropic(chat), ensure_ascii=False, separators=(",", ":")
                        ),
                        media_type="application/json",
                    )
                return Response(
                    content=json.dumps(
                        convert.chat_to_responses(chat, meta), ensure_ascii=False, separators=(",", ":")
                    ),
                    media_type="application/json",
                )
            last = {"status": rstatus, "body": rbody, "error": rerr, "content_type": ctype}
            last_key = key
            # 401/403 为账号级鉴权失败：立即返回（账号已被硬封禁，重试只会得到 429 封禁掩码）
            if rstatus in (401, 403):
                return _upstream_fail(key, last, cfg, anthropic=anthropic)
            # 400 且报思考参数不兼容：自动降级（去思考或改用模型默认强度）后重试一次
            if rstatus == 400 and convert.thinking_unsupported(rbody, rstatus) and not downgraded:
                downgraded = True
                tdefs = convert.parse_thinking_defaults(
                    upstreams.upstream_value(key, "thinking_defaults", "")
                )
                if convert.downgrade_thinking(chat_req, up_model, tdefs):
                    raw = json.dumps(chat_req, ensure_ascii=False, separators=(",", ":"))
                    continue
            # 400 且报 JSON 反序列化/类型错误（字符串数字被上游拒绝）：激进转换后重试一次
            if rstatus == 400 and convert.is_deserialize_error(rbody, rstatus) and not downgraded:
                downgraded = True
                if convert.coerce_all_types(chat_req):
                    raw = json.dumps(chat_req, ensure_ascii=False, separators=(",", ":"))
                    continue
            # 400 且报「不支持的参数」（如 enable_thinking）：移除被点名参数后重试一次
            if rstatus == 400 and convert.is_unsupported_param_error(rbody, rstatus) and not downgraded:
                downgraded = True
                if convert.strip_unsupported_params(chat_req, rbody):
                    raw = json.dumps(chat_req, ensure_ascii=False, separators=(",", ":"))
                    continue
            # 渠道级不可用：快速失败，不浪费重试
            if convert.is_channel_exhausted(rbody):
                return _upstream_fail(key, last, cfg, anthropic=anthropic)
            if not (rstatus in (0, 429) or rstatus >= 500):
                return _upstream_fail(key, last, cfg, anthropic=anthropic)
            # 429 吸收：延长重试预算，尽量不把 429 透传给下游
            if rstatus == 429 and rl_left > 0 and cfg.get("queue_enabled", True):
                rl_left -= 1
                max_attempts += 1
                await asyncio.sleep(_backoff_ms(cfg, attempt, key) / 1000)
                continue
            await asyncio.sleep(_backoff_ms(cfg, attempt, key) / 1000)
    finally:
        # 异常兜底：任何没走到正常释放的路径（非 httpx 异常、序列化失败、
        # 上游地址非法等）都必须把账号还回去，否则会被永久锁定在这个请求上。
        k_held = hold["key"]
        if k_held is not None:
            hold["key"] = None
            _exc = sys.exc_info()[1]
            _note = "请求处理异常，兜底释放账号：%s" % (
                ("%s: %s" % (type(_exc).__name__, _exc))[:180] if _exc else "未走到正常释放"
            )
            try:
                await pool.arelease(
                    k_held["id"],
                    True,
                    500,
                    _note,
                    None,
                    _release_log(
                        ep,
                        model,
                        500,
                        int((time.time() - t0) * 1000),
                        _note,
                        attempt,
                        k_held,
                        ip,
                        up_model=up_model,
                        stream=stream,
                    ),
                )
            except Exception:
                pass
    return _upstream_fail(last_key, last, cfg, anthropic=anthropic)


async def _stream_convert(
    request: Request,
    client: httpx.AsyncClient,
    r: httpx.Response,
    key: dict,
    ep: str,
    model: str,
    cfg: dict,
    up_model: str,
    raw: str,
    ip: str,
    attempt: int,
    t0: float,
    status: int,
    ctype: str,
    protocol: str,
    first_chunk: bytes = b"",
    ait=None,
):
    """SSE 转换：上游 chunk 流 → Responses / Anthropic 事件流。first_chunk/ait 为外层预读的首帧。"""
    pend: list = []
    up_bytes = 0
    if protocol == "responses":
        conv = ResponsesStream(lambda ev, data: pend.append(_sse(ev, data)), model)
    else:
        conv = AnthropicStream(
            lambda ev, data: pend.append(_sse(ev, data)), model, estimate_request_tokens(raw)
        )

    idle_to = int(cfg.get("sse_idle_timeout") or 0)

    async def gen() -> AsyncGenerator[bytes, None]:
        nonlocal up_bytes
        first_chunk_at = 0.0
        try:
            # 先发外层预读的首帧
            if first_chunk:
                up_bytes += len(first_chunk)
                first_chunk_at = time.time()
                conv.feed(first_chunk.decode("utf-8", "replace"))
                if pend:
                    yield "".join(pend).encode()
                    pend.clear()
            stream = ait if ait is not None else r.aiter_bytes()
            while True:
                try:
                    if idle_to > 0:
                        chunk = await asyncio.wait_for(stream.__anext__(), timeout=idle_to)
                    else:
                        chunk = await stream.__anext__()
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    break  # 空闲超时：结束流，避免无限挂起
                up_bytes += len(chunk)
                if not first_chunk_at:
                    first_chunk_at = time.time()
                conv.feed(chunk.decode("utf-8", "replace"))
                if pend:
                    yield "".join(pend).encode()
                    pend.clear()
        finally:
            ms = int((time.time() - t0) * 1000)
            ttfb = int((first_chunk_at - t0) * 1000) if first_chunk_at else ms
            usage = {
                "prompt_tokens": -(-len(raw) // 3),
                "completion_tokens": estimate_output_tokens(up_bytes),
            }
            await pool.arelease(
                key["id"],
                True,
                status,
                "",
                usage,
                _release_log(
                    ep,
                    model,
                    status,
                    ms,
                    "",
                    attempt,
                    key,
                    ip,
                    up_model=up_model,
                    stream=True,
                    ttfb_ms=ttfb,
                    in_tok=int(usage["prompt_tokens"]),
                    out_tok=int(usage["completion_tokens"]),
                ),
            )
            try:
                await r.aclose()  # stream=True 必须显式关闭
            except Exception:
                pass

    return StreamingResponse(gen(), media_type="text/event-stream", headers=_cors())


@app.get("/v1/models/{model_id:path}")
async def v1_model_retrieve(request: Request, model_id: str):
    """OpenAI 兼容：检索单个模型。"""
    cfg = cfg_all()
    entry = _token_entry(request, cfg)
    if _has_auth(cfg) and entry is None:
        return _error(401, "访问令牌无效", "invalid_request_error", "invalid_api_key")
    if _check_model(model_id, entry, cfg):
        return _error(
            404, f"The model '{model_id}' does not exist", "invalid_request_error", "model_not_found"
        )
    # 只有渠道显式配置了模型白名单时才能判定「不支持」；未配置时网关支持任意模型名，放行
    known = set(upstreams.curated_models())
    if known and model_id not in known:
        return _error(
            404, f"The model '{model_id}' does not exist", "invalid_request_error", "model_not_found"
        )
    return JSONResponse(
        {"id": model_id, "object": "model", "created": 0, "owned_by": "gateway"}, headers=_cors()
    )


@app.get("/v1/{rest:path}")
async def v1_get(rest: str):
    return _error(404, f"Invalid URL (GET /v1/{rest})")


@app.post("/v1/{rest:path}")
async def v1_post(request: Request, rest: str):
    return _error(404, f"Invalid URL (POST /v1/{rest})")


# ============================================================ 管理页面与静态资源


@app.get("/admin")
async def admin_page(request: Request):
    cfg = STORE.load()["config"]
    session = request.cookies.get("ngw_session") or ""
    if not (session and hmac.compare_digest(session, _session_cookie(cfg))):
        return Response(_page("login.html"), media_type="text/html", headers=_NOCACHE)
    csrf = _csrf_token(cfg)
    return Response(_page("admin.html", csrf), media_type="text/html", headers=_NOCACHE)


@app.get("/")
async def root():
    return Response(_page("landing.html"), media_type="text/html", headers=_NOCACHE)


@app.get("/queue")
async def queue_page():
    return Response((WEB_DIR / "queue.html").read_text(encoding="utf-8"), media_type="text/html")


@app.get("/api/presets")
async def presets_public():
    return JSONResponse({"presets": STORE.load().get("channel_presets", {})}, headers=_cors())


@app.get("/api/queue/public")
async def queue_public():
    from core import queue as qm

    return JSONResponse(qm.stats(public=True), headers=_cors())


@app.get("/assets/admin.js")
async def admin_js():
    # 禁止缓存：否则升级后浏览器仍用旧脚本（新功能会"打不开"）
    return Response(
        (WEB_DIR / "admin.js").read_text(encoding="utf-8"),
        media_type="application/javascript",
        headers=_NOCACHE,
    )
