"""管理端 API：认证、概览、账号/渠道/日志/排队/设置/配置同步。"""

from __future__ import annotations

import hmac
import json
import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from core import pool, upstreams
from core.store import STORE, verify_password, session_cookie as _session_cookie, csrf_token as _csrf_token
from core.util import as_bool

router = APIRouter(prefix="/api")

CSRF_COOKIE = "ngw_csrf"


# ============================================================ 认证


def authed(request: Request) -> bool:
    cfg = STORE.load()["config"]
    expected = _session_cookie(cfg)
    got = request.cookies.get("ngw_session") or ""
    return bool(got) and hmac.compare_digest(got, expected)


_login_rate: dict[str, list[float]] = {}


def _login_rate_ok(ip: str) -> bool:
    """登录尝试限流：同一来源 5 分钟内最多 10 次，用于挡口令暴力尝试。

    这里以前只对时间戳做过滤、从不记录本次尝试，于是 len() 恒为 0，限流完全失效
    （那句 429 成了死代码）；同时每个来源都会在字典里留下一条永不回收的空记录。
    """
    now = time.time()
    attempts = [t for t in _login_rate.get(ip, []) if now - t < 300]
    if len(attempts) >= 10:
        _login_rate[ip] = attempts  # 维持限流状态
        return False
    attempts.append(now)
    _login_rate[ip] = attempts
    if len(_login_rate) > 1000:  # 顺手回收过期来源，避免字典无限增长
        for k in [k for k, v in _login_rate.items() if not v or now - v[-1] >= 300]:
            _login_rate.pop(k, None)
    return True


def _require(request: Request) -> JSONResponse | None:
    if not authed(request):
        return JSONResponse({"error": {"message": "未登录或会话已过期", "type": "auth"}}, status_code=401)
    cfg = STORE.load()["config"]
    expected_csrf = _csrf_token(cfg)
    got = request.headers.get("x-csrf") or request.cookies.get(CSRF_COOKIE) or ""
    if request.method == "POST" and not hmac.compare_digest(got, expected_csrf):
        return JSONResponse(
            {"error": {"message": "CSRF 校验失败，请刷新页面", "type": "auth"}}, status_code=403
        )
    return None


async def _json_dict(request: Request) -> dict | None:
    """读 JSON body;非 dict(数组/字符串/解析失败)返回 None,调用方回 400。"""
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


@router.post("/login")
async def login(request: Request):
    ip = request.client.host if request.client else "-"
    if not _login_rate_ok(ip):
        return JSONResponse(
            {"error": {"message": "尝试过于频繁，请 5 分钟后重试", "type": "auth"}}, status_code=429
        )
    try:
        body = await request.json()
    except Exception:
        body = dict(await request.form())
    cfg = STORE.load()["config"]
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    ok_user = username.lower() == str(cfg.get("admin_username") or "").lower()
    ok_pass = verify_password(password, str(cfg.get("admin_password_hash") or ""))
    if not (ok_user and ok_pass):
        return JSONResponse({"error": {"message": "账号或密码错误", "type": "auth"}}, status_code=401)
    # 登录成功即清零：限流是挡暴力尝试的，不该把正常登录也算进去
    _login_rate.pop(ip, None)
    session = _session_cookie(cfg)
    csrf = _csrf_token(cfg)
    # https 部署下会话 Cookie 必须带 secure（仅当确为 https 时才设，
    # 否则反代成 http 的开发/内网部署会无法登录）
    secure = request.url.scheme == "https"
    resp = JSONResponse({"ok": True, "csrf": csrf})
    resp.set_cookie("ngw_session", session, httponly=True, samesite="lax", secure=secure)
    resp.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="lax", secure=secure)
    return resp


@router.post("/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("ngw_session")
    resp.delete_cookie(CSRF_COOKIE)
    return resp


# ============================================================ 概览


@router.get("/overview")
async def overview(request: Request):
    bad = _require(request)
    if bad:
        return bad
    db = STORE.load()
    cfg = db["config"]
    now = int(time.time())
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    counts = {"total": 0, "enabled": 0, "banned": 0, "invalid": 0, "manual_disabled": 0}
    daily_req = daily_tok = 0
    risky = []
    # 快照遍历：executor 线程可能在遍历中改 dict/list(新增账号/写桶)，
    # 直接遍历共享结构会抛 RuntimeError: dictionary changed size during iteration
    for k in list(db["keys"]):
        counts["total"] += 1
        if k.get("enabled"):
            counts["enabled"] += 1
            if (k.get("banned_until") or 0) > now:
                counts["banned"] += 1
            if k.get("status") == "invalid":
                counts["invalid"] += 1
        else:
            counts["manual_disabled"] += 1
        d = (k.get("daily") or {}).get(day) or {}
        daily_req += int(d.get("requests") or 0)
        daily_tok += int(d.get("tokens") or 0)
        ratio = (k.get("total_fail") or 0) / k["total_requests"] if k.get("total_requests") else 0
        if (ratio >= 0.4 and (k.get("total_fail") or 0) >= 3) or (k.get("consecutive_failures") or 0) >= 2:
            risky.append(
                {
                    "id": k["id"],
                    "email": k["email"],
                    "fail_ratio": round(ratio * 100),
                    "consecutive": k.get("consecutive_failures") or 0,
                    "total_fail": k.get("total_fail") or 0,
                    "last_error": (k.get("last_error") or "")[:80],
                }
            )
    risky.sort(key=lambda x: (x["consecutive"], x["fail_ratio"]), reverse=True)
    # buckets[kid] 是「最近 60s 请求时间戳列表」（见 pool.acquire），不是 dict。
    # 以前按 dict+小时键去 get,永远得到 0。
    rpm = 0
    for b in list((db.get("buckets") or {}).values()):
        if isinstance(b, list):
            rpm += sum(1 for t in b if now - t < 60)
    today = db["stats"].get(day) or {"total": 0, "success": 0, "fail": 0, "models": {}}
    models = sorted(today["models"].items(), key=lambda x: -x[1])[:10]
    recent_errors = [r for r in db.get("logs", []) if (r[4] if len(r) > 4 else 0) >= 400][:8]
    # 与队列面板同口径:过滤过期条目 —— 历史僵尸(进程重启时死掉的等待请求)
    # 不过滤的话,仪表盘会虚报「排队中 N」而队列面板实际为空
    try:
        _mw = int(db["config"].get("queue_max_wait"))
    except (TypeError, ValueError):
        _mw = 15
    _mw = 0 if _mw == 0 else max(5, _mw)
    _qcut = now - _mw * 2
    queue_now = len([e for e in (db.get("queue") or []) if isinstance(e, dict) and e.get("t", 0) >= _qcut])
    return {
        "keys": counts,
        "rpm": rpm,
        "rpm_limit_total": (
            -1
            if int(cfg.get("rate_limit_per_minute") or 0) <= 0
            else counts["enabled"] * max(1, int(cfg.get("rate_limit_per_minute") or 20))
        ),
        "daily": {"requests": daily_req, "tokens": daily_tok},
        "queue": queue_now,
        "today": {
            "total": today["total"],
            "success": today["success"],
            "fail": today["fail"],
            "rate": round(today["success"] * 100 / today["total"]) if today["total"] else None,
        },
        "models": [{"model": m, "count": c} for m, c in models],
        "recent_errors": recent_errors,
        "risky": risky[:10],
        "pool": _pool_state(),
        "server_time": now,
    }


def _pool_state() -> dict:
    """上游连接池与号池并发的实时状态(概览用)。

    在途 = 正在跑的上游请求数;若在途长期贴着池上限,就是容量问题(调大池),
    而若池上限远大于在途却仍出现 PoolTimeout,才是连接泄漏。
    """
    import server as _srv

    return {
        "max_connections": int(getattr(_srv, "_pool_max_conn", 0) or 0),
        "inflight": pool.inflight_total(),
        "odd_releases": pool.inflight_odd_releases(),
    }


# ============================================================ 账号


@router.get("/keys")
async def keys(request: Request):
    bad = _require(request)
    if bad:
        return bad
    db = STORE.load()
    cfg = db["config"]
    from urllib.parse import unquote

    q = unquote(str(request.query_params.get("q") or "")).strip().lower()
    status = request.query_params.get("status") or "all"
    try:
        page = max(1, int(request.query_params.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    per = 20
    now = int(time.time())
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    up_names = {u["id"]: u["name"] for u in list(db.get("upstreams", []))}
    rows = []
    for k in reversed(list(db["keys"])):
        if q and q not in k["email"].lower() and q not in k["apikey"].lower():
            continue
        enabled = bool(k.get("enabled"))
        banned = (k.get("banned_until") or 0) > now
        if status == "active" and (not enabled or banned):
            continue
        if status == "disabled" and enabled and not banned:
            continue
        if status == "banned" and not banned:
            continue
        k = dict(k)
        k["rpm_used"] = len([t for t in (db.get("buckets", {}).get(k["id"]) or []) if now - t < 60])
        k["fail_ratio"] = round(k["total_fail"] * 100 / k["total_requests"]) if k.get("total_requests") else 0
        k["today"] = (k.get("daily") or {}).get(day) or {"requests": 0, "tokens": 0}
        k["upstream_name"] = up_names.get(k.get("upstream_id"), "-")
        row = dict(k)
        # 在途是瞬时值,给响应副本即可,不要写回存储
        row["inflight"] = pool.inflight_of(k["id"])
        rows.append(row)
    total = len(rows)
    return {
        "total": total,
        "page": page,
        "pages": max(1, -(-total // per)),
        "rows": rows[(page - 1) * per : page * per],
        "rate_limit": int(cfg["rate_limit_per_minute"]),
        "daily_cap": int(cfg.get("daily_request_cap") or 0),
    }


@router.get("/keydetail")
async def keydetail(request: Request):
    bad = _require(request)
    if bad:
        return bad
    from urllib.parse import unquote

    kid = request.query_params.get("id") or ""
    db = STORE.load()
    for k in list(db["keys"]):
        if k["id"] == kid:
            now = int(time.time())
            day = time.strftime("%Y-%m-%d", time.localtime(now))
            k = dict(k)
            k["rpm_used"] = len([t for t in (db.get("buckets", {}).get(kid) or []) if now - t < 60])
            k["today"] = (k.get("daily") or {}).get(day) or {"requests": 0, "tokens": 0}
            k["rate_limit"] = int(db["config"]["rate_limit_per_minute"])
            for u in list(db.get("upstreams", [])):
                if u["id"] == k.get("upstream_id"):
                    k["upstream_name"] = u["name"]
            return {"key": k, "recent": (k.get("recent") or [])[:10]}
    return JSONResponse({"error": {"message": "密钥不存在", "type": "not_found"}}, status_code=404)


@router.post("/keys/import")
async def keys_import(request: Request):
    bad = _require(request)
    if bad:
        return bad
    text = ""
    file_text = ""
    upstream_id = ""
    content_type = request.headers.get("content-type", "")
    if "multipart" in content_type:
        form = await request.form()
        up = form.get("file")
        if up is not None:
            data = await up.read()
            if len(data) > 5 * 1024 * 1024:
                return JSONResponse({"error": {"message": "文件过大（>5MB）"}}, status_code=400)
            file_text = data.decode("utf-8", "replace")
        text = str(form.get("text") or "")
        upstream_id = str(form.get("upstream_id") or "")
    else:
        body = await request.json()
        text = str(body.get("text") or "")
        upstream_id = str(body.get("upstream_id") or "")
    if not text.strip() and not file_text.strip():
        return JSONResponse({"error": {"message": "请粘贴账户数据或选择 CSV 文件"}}, status_code=400)
    valid_ids = [u["id"] for u in upstreams.all_upstreams()]
    if upstream_id not in valid_ids:
        enabled = [u["id"] for u in upstreams.all_upstreams() if u.get("enabled")]
        upstream_id = enabled[0] if enabled else (valid_ids[0] if valid_ids else "")
    try:
        res = pool.import_accounts(text, upstream_id, loose_text=file_text)
        return {"ok": True, **res}
    except Exception as e:
        return JSONResponse({"error": {"message": f"导入失败：{e}"}}, status_code=500)


@router.get("/keys/export")
async def keys_export(request: Request):
    bad = _require(request)
    if bad:
        return bad
    db = STORE.load()
    lines = ["email,password,apikey"]
    for k in list(db["keys"]):
        lines.append(
            ",".join([_csv_escape(k["email"]), _csv_escape(k["password"]), _csv_escape(k["apikey"])])
        )
    return Response(
        "\n".join(lines),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=keys.csv"},
    )


def _csv_escape(v: str) -> str:
    v = str(v or "")
    if any(c in v for c in ',"\n'):
        return '"' + v.replace('"', '""') + '"'
    return v


@router.post("/keys/op")
async def keys_op(request: Request):
    bad = _require(request)
    if bad:
        return bad
    body = await _json_dict(request)
    if body is None:
        return JSONResponse({"error": {"message": "请求体格式错误"}}, status_code=400)
    op = str(body.get("op") or "")
    kid = str(body.get("id") or "")
    try:
        if op == "test":
            # test_key 内部是同步 httpx 调用(最长 20s),直接在 async 路由里跑
            # 会冻结整个事件循环——期间所有代理/流式请求全部停摆
            import asyncio

            result = await asyncio.get_event_loop().run_in_executor(None, pool.test_key, kid)
            return {"ok": True, "test": result}
        fn = {
            "enable": lambda: pool.set_enabled(kid, True),
            "disable": lambda: pool.set_enabled(kid, False),
            "unban": lambda: pool.unban(kid),
            "delete": lambda: pool.delete_key(kid),
            "reset": lambda: pool.reset_stats(kid),
        }.get(op)
        if fn is None:
            return JSONResponse({"error": {"message": "未知操作"}}, status_code=400)
        return {"ok": fn()}
    except Exception as e:
        return JSONResponse({"error": {"message": str(e)}}, status_code=500)


@router.post("/keys/batch")
async def keys_batch(request: Request):
    bad = _require(request)
    if bad:
        return bad
    body = await _json_dict(request)
    if body is None:
        return JSONResponse({"error": {"message": "请求体格式错误"}}, status_code=400)
    op = str(body.get("op") or "")
    ids = [str(i) for i in (body.get("ids") or []) if str(i)]
    if not ids:
        return JSONResponse({"error": {"message": "未选择账号"}}, status_code=400)
    if op == "test":
        # 批量测试同样走 executor:20 个账号串行同步测试最坏会阻塞事件循环 ~400 秒
        import asyncio

        loop = asyncio.get_event_loop()
        results = {}
        for i in ids[:20]:
            try:
                results[i] = await loop.run_in_executor(None, pool.test_key, i)
            except Exception as e:
                results[i] = {"ok": False, "error": str(e)}
        return {"ok": True, "results": results}
    results: dict = {}
    if op == "enable":
        for i in ids:
            results[i] = pool.set_enabled(i, True)
    elif op == "unban":
        # 解封与启用分离:只清封禁/冷却/退避,不动「手动停用」状态
        for i in ids:
            results[i] = pool.unban(i)
    elif op == "disable":
        for i in ids:
            results[i] = pool.set_enabled(i, False)
    elif op == "reset":
        for i in ids:
            results[i] = pool.reset_stats(i)
    elif op == "delete":
        for i in ids:
            results[i] = pool.delete_key(i)
    elif op == "move":
        target = str(body.get("upstream_id") or "")
        if target not in [u["id"] for u in upstreams.all_upstreams()]:
            return JSONResponse({"error": {"message": "目标渠道不存在"}}, status_code=400)

        def _fn(db: dict):
            for k in db["keys"]:
                if k["id"] in ids:
                    k["upstream_id"] = target

        await STORE.aupdate(_fn)
        STORE.flush()
        return {"ok": True, "moved": len(ids)}
    else:
        return JSONResponse({"error": {"message": "未知操作"}}, status_code=400)
    return {"ok": True, "results": results}


@router.post("/keys/clear-all")
async def keys_clear_all(request: Request):
    bad = _require(request)
    if bad:
        return bad
    body = await _json_dict(request)
    if body is None:
        return JSONResponse({"error": {"message": "请求体格式错误"}}, status_code=400)
    if body.get("confirm") != "yes":
        return JSONResponse({"error": {"message": "缺少确认参数"}}, status_code=400)
    return {"ok": True, "removed": pool.clear_all()}


@router.get("/queue")
async def queue_list(request: Request):
    bad = _require(request)
    if bad:
        return bad
    from core.queue import stats

    return stats()


@router.post("/queue")
async def queue_clear(request: Request):
    bad = _require(request)
    if bad:
        return bad
    from core.queue import clear

    clear()
    return {"ok": True}


# ============================================================ 上游


def _upstream_row(db: dict, u: dict) -> dict:
    now = int(time.time())
    # pool_buckets 的键是分钟精度 %Y%m%d%H%M(见 pool.acquire),
    # 以前这里按小时精度去查,永远 get 不到 → minute_used 恒为 0
    minute = time.strftime("%Y%m%d%H%M", time.gmtime(now))
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    keys = [k for k in db["keys"] if k.get("upstream_id") == u["id"]]
    today_req = sum(int(((k.get("daily") or {}).get(day) or {}).get("requests") or 0) for k in keys)
    rec = db.get("up_recent", {}).get(u["id"]) or []
    ok_n = sum(1 for v in rec if v)
    score = round((ok_n + 5) / (len(rec) + 10) * 100)
    return {
        **u,
        "keys": len(keys),
        "enabled_keys": sum(1 for k in keys if k.get("enabled")),
        "today_requests": today_req,
        "minute_used": (
            sum(1 for t in (db.get("pool_buckets", {}).get(u["id"]) or []) if now - t < 60)
            if isinstance(db.get("pool_buckets", {}).get(u["id"]), list)
            else int((db.get("pool_buckets", {}).get(u["id"], {}).get(minute)) or 0)
        ),
        "feasibility": score,
        "recent": len(rec),
        "model_map_count": len(u.get("model_map") or {}),
        "hide_errors_global": bool(db["config"].get("hide_upstream_errors", True)),
        "hide_mapped_global": bool(db["config"].get("hide_mapped_names", True)),
    }


@router.get("/upstreams")
async def upstreams_list(request: Request):
    bad = _require(request)
    if bad:
        return bad
    db = STORE.load()
    return {"rows": [_upstream_row(db, u) for u in db.get("upstreams", [])]}


@router.post("/upstreams")
async def upstreams_save(request: Request):
    bad = _require(request)
    if bad:
        return bad
    body = await _json_dict(request)
    if body is None:
        return JSONResponse({"error": {"message": "请求体格式错误"}}, status_code=400)
    row, err = upstreams.save(body)
    if row is None:
        return JSONResponse({"error": {"message": err}}, status_code=400)
    db = STORE.load()
    return {"ok": True, "upstream": row, "rows": [_upstream_row(db, u) for u in db.get("upstreams", [])]}


@router.post("/upstreams/delete")
async def upstreams_delete(request: Request):
    bad = _require(request)
    if bad:
        return bad
    body = await _json_dict(request)
    if body is None:
        return JSONResponse({"error": {"message": "请求体格式错误"}}, status_code=400)
    ok, err = upstreams.delete(str(body.get("id") or ""))
    if not ok:
        return JSONResponse({"error": {"message": err}}, status_code=400)
    db = STORE.load()
    return {"ok": True, "rows": [_upstream_row(db, u) for u in db.get("upstreams", [])]}


# ============================================================ 日志 / 排队


@router.get("/logs")
async def logs(request: Request):
    bad = _require(request)
    if bad:
        return bad
    db = STORE.load()
    # 快照:与其它端点一致 —— 活引用列表在序列化期间可能被 executor 线程并发插入
    return {"rows": list(db.get("logs") or []), "enabled": bool(db["config"].get("log_enabled", True))}


@router.post("/logs/clear")
async def logs_clear(request: Request):
    bad = _require(request)
    if bad:
        return bad

    def _fn(db: dict):
        db["logs"] = []

    await STORE.aupdate(_fn)
    STORE.flush()
    return {"ok": True}


# ============================================================ 设置

INT_SETTINGS = {
    "rate_limit_per_minute",
    "tpm_limit",
    "account_cooldown_ms",
    "max_retries",
    "retry_backoff_base_ms",
    "retry_backoff_max_ms",
    "retry_min_wait_ms",
    "ban_step_seconds",
    "ban_max_seconds",
    "hard_fail_ban_seconds",
    "hard_fail_disable_count",
    "daily_request_cap",
    "daily_token_limit",
    "hourly_request_limit",
    "queue_max_wait",
    "queue_poll_ms",
    "cool_429_seconds",
    "cool_5xx_seconds",
    "cool_timeout_seconds",
    "cool_conn_seconds",
    "breaker_threshold",
    "breaker_seconds",
    "ttfb_timeout",
    "sse_idle_timeout",
    "pool_max_connections",
    "restart_interval_hours",
    "request_timeout",
    "connect_timeout",
    "log_max",
    "session_log_max",
    "training_log_max",
    "training_min_chars",
    "watchdog_minutes",
    "acct_concurrency",
    "total_concurrency",
    "pool_rpm_cap",
    "pool_daily_cap",
    "warmup_seconds",
}
STR_SETTINGS = {
    "upstream_base",
    "timezone",
    "model_whitelist",
    "model_blacklist",
    "param_overrides",
    "update_token",
}
BOOL_SETTINGS = {
    "log_enabled",
    "verify_tls",
    "queue_enabled",
    "update_enabled",
    "hide_upstream_errors",
    "hide_mapped_names",
    "breaker_enabled",
    "watchdog_enabled",
}


@router.get("/settings")
async def settings_get(request: Request):
    bad = _require(request)
    if bad:
        return bad
    cfg = dict(STORE.load()["config"])
    cfg.pop("admin_password_hash", None)
    cfg.pop("session_secret", None)
    return cfg


@router.post("/settings")
async def settings_save(request: Request):
    bad = _require(request)
    if bad:
        return bad
    body = await _json_dict(request)
    if body is None:
        return JSONResponse({"error": {"message": "请求体格式错误"}}, status_code=400)
    incoming = body.get("config") or {}

    def _fn(db: dict):
        cfg = db["config"]
        for k in INT_SETTINGS:
            if k in incoming and str(incoming[k]) != "":
                try:
                    cfg[k] = int(incoming[k])
                except (TypeError, ValueError):
                    pass
        for k in STR_SETTINGS:
            if k in incoming and isinstance(incoming[k], str):
                cfg[k] = incoming[k].strip()
        for k in BOOL_SETTINGS:
            if k in incoming:
                # bool("false") 是 True：前端可能把开关序列化成字符串，必须按语义解析
                cfg[k] = as_bool(incoming[k], bool(cfg.get(k)))
        if "gateway_tokens" in incoming and isinstance(incoming["gateway_tokens"], str):
            struct = []
            for line in incoming["gateway_tokens"].splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = [p.strip() for p in line.split("|", 1)]
                if len(parts[0]) < 8:
                    continue
                models = (
                    []
                    if len(parts) == 1 or parts[1] == "*"
                    else [m.strip() for m in parts[1].replace(";", ",").split(",") if m.strip()]
                )
                struct.append({"t": parts[0], "m": models})
            if struct:
                cfg["gateway_tokens"] = struct
        if "admin_username" in incoming and str(incoming["admin_username"]).strip():
            cfg["admin_username"] = str(incoming["admin_username"]).strip()[:32]
        base = str(cfg.get("upstream_base") or "")
        if base and not base.startswith("http"):
            cfg["upstream_base"] = "https://integrate.api.nvidia.com/v1"

    await STORE.aupdate(_fn)
    STORE.flush()
    cfg = dict(STORE.load()["config"])
    cfg.pop("admin_password_hash", None)
    cfg.pop("session_secret", None)
    return cfg


@router.post("/password")
async def password_change(request: Request):
    bad = _require(request)
    if bad:
        return bad
    body = await _json_dict(request)
    if body is None:
        return JSONResponse({"error": {"message": "请求体格式错误"}}, status_code=400)
    cfg = STORE.load()["config"]
    if not verify_password(str(body.get("old") or ""), str(cfg.get("admin_password_hash") or "")):
        return JSONResponse({"error": {"message": "当前密码错误"}}, status_code=400)
    new = str(body.get("new") or "")
    if len(new) < 6:
        return JSONResponse({"error": {"message": "新密码至少 6 位"}}, status_code=400)

    def _fn(db: dict):
        import os

        from core.store import _hash_password

        db["config"]["admin_password_hash"] = _hash_password(new)
        # 轮换会话密钥：让改密前的所有登录会话(含已泄漏的 cookie)全部失效。
        # 会话凭证是 HMAC(secret) 的静态值,secret 不变则旧 cookie 永远有效。
        db["config"]["session_secret"] = os.urandom(24).hex()

    await STORE.aupdate(_fn)
    STORE.flush()
    return {"ok": True}


@router.post("/stats/reset")
async def stats_reset(request: Request):
    bad = _require(request)
    if bad:
        return bad
    pool.reset_all_stats()
    return {"ok": True}


# ============================================================ 访问令牌


def _tokens_rows(cfg: dict) -> list:
    out = []
    for t in cfg.get("gateway_tokens") or []:
        if isinstance(t, str):
            out.append({"t": t, "m": [], "last_ip": "", "last_at": 0})
        elif isinstance(t, dict):
            out.append({
                "t": str(t.get("t") or ""),
                "m": [str(x) for x in (t.get("m") or [])],
                "last_ip": str(t.get("last_ip") or ""),
                "last_at": int(t.get("last_at") or 0),
            })
    return out


# ============================================================ 号池热力图


@router.get("/poolmap")
async def poolmap(request: Request):
    """号池热力图:按渠道分组返回每个账号的实时状态方块。

    s: 0=可用(绿) 1=繁忙(黄,并发占用中) 2=不可用(红,封禁/冷却/停用/失效)
    """
    bad = _require(request)
    if bad:
        return bad
    db = STORE.load()
    cfg = db["config"]
    now = int(time.time())
    conc = int(cfg.get("acct_concurrency") or 0)
    up_names = {u["id"]: u["name"] for u in list(db.get("upstreams", []))}
    groups: dict = {}
    for k in list(db.get("keys", [])):
        if not isinstance(k, dict):
            continue
        gname = up_names.get(k.get("upstream_id"), "未分组")
        g = groups.setdefault(gname, [])
        if not k.get("enabled"):
            s, why = 2, "停用"
        elif (k.get("status") or "") == "invalid":
            s, why = 2, "密钥失效"
        elif int(k.get("banned_until") or 0) > now:
            s, why = 2, (k.get("ban_reason") or "封禁")
        elif int(k.get("cooldown_until") or 0) > now:
            s, why = 1, "冷却中"
        elif conc > 0 and pool._inflight.get(k["id"], 0) >= conc:
            s, why = 1, "并发占用中"
        else:
            s, why = 0, "可用"
        g.append({"id": k["id"], "s": s, "w": why})
    return {
        "groups": [
            {"name": gname, "cells": cells, "total": len(cells),
             "ok": sum(1 for c in cells if c["s"] == 0),
             "busy": sum(1 for c in cells if c["s"] == 1),
             "bad": sum(1 for c in cells if c["s"] == 2)}
            for gname, cells in groups.items()
        ]
    }


@router.get("/tokens")
async def tokens_list(request: Request):
    bad = _require(request)
    if bad:
        return bad
    return {"rows": _tokens_rows(STORE.load()["config"])}


@router.post("/tokens")
async def tokens_add(request: Request):
    bad = _require(request)
    if bad:
        return bad
    body = await _json_dict(request)
    if body is None:
        return JSONResponse({"error": {"message": "请求体格式错误"}}, status_code=400)
    t = str(body.get("t") or "").strip()
    m = body.get("m")
    if not t:
        import os

        t = "sk-gw-" + os.urandom(16).hex()
    if len(t) < 8 or len(t) > 200:
        return JSONResponse({"error": {"message": "令牌长度需在 8-200 之间"}}, status_code=400)
    if isinstance(m, str):
        m = [x.strip() for x in m.replace("，", ",").split(",")]
    elif not isinstance(m, (list, tuple)):
        m = [m] if m else []
    mlist = list(dict.fromkeys(str(x).strip() for x in (m or []) if str(x).strip()))

    state = {"dup": False}

    def _fn(db: dict):
        toks = db["config"].setdefault("gateway_tokens", [])
        for x in toks:
            xt = x.get("t") if isinstance(x, dict) else x
            if xt == t:
                state["dup"] = True
                return
        toks.append({"t": t, "m": mlist})

    await STORE.aupdate(_fn)
    STORE.flush()
    if state["dup"]:
        return JSONResponse({"error": {"message": "令牌已存在"}}, status_code=400)
    return {"ok": True, "token": t, "m": mlist}


@router.post("/tokens/update")
async def tokens_update(request: Request):
    bad = _require(request)
    if bad:
        return bad
    body = await _json_dict(request)
    if body is None:
        return JSONResponse({"error": {"message": "请求体格式错误"}}, status_code=400)
    t = str(body.get("t") or "")
    m = body.get("m")
    if isinstance(m, str):
        m = [x.strip() for x in m.replace("，", ",").split(",")]
    mlist = list(dict.fromkeys(str(x).strip() for x in (m or []) if str(x).strip()))
    found = [False]

    def _fn(db: dict):
        toks = db["config"].get("gateway_tokens") or []
        for i, x in enumerate(toks):
            xt = x.get("t") if isinstance(x, dict) else x
            if xt == t:
                db["config"]["gateway_tokens"][i] = {"t": t, "m": mlist}
                found[0] = True
                return

    await STORE.aupdate(_fn)
    STORE.flush()
    if not found[0]:
        return JSONResponse({"error": {"message": "令牌不存在"}}, status_code=404)
    return {"ok": True}


@router.post("/tokens/delete")
async def tokens_delete(request: Request):
    bad = _require(request)
    if bad:
        return bad
    body = await _json_dict(request)
    if body is None:
        return JSONResponse({"error": {"message": "请求体格式错误"}}, status_code=400)
    t = str(body.get("t") or "")
    removed = [False]

    def _fn(db: dict):
        toks = db["config"].get("gateway_tokens") or []
        new = [x for x in toks if (x.get("t") if isinstance(x, dict) else x) != t]
        if len(new) != len(toks):
            removed[0] = True
        db["config"]["gateway_tokens"] = new

    await STORE.aupdate(_fn)
    STORE.flush()
    if not removed[0]:
        return JSONResponse({"error": {"message": "令牌不存在"}}, status_code=404)
    return {"ok": True}


@router.post("/update")
async def remote_update(request: Request):
    """远程更新:支持两种鉴权路径。

    路径A(管理令牌):Authorization: Bearer <update_token> —— 无需 admin
    会话/CSRF,供自动化发布流程使用(最小权限:只暴露更新能力)。
    路径B(管理会话):admin 登录 + CSRF(后台手动触发)。
    两条路径都要求 update_enabled 开关打开。
    """
    cfg = STORE.load()["config"]
    token = str(cfg.get("update_token") or "")
    auth = request.headers.get("authorization") or ""
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    token_ok = bool(token and bearer and hmac.compare_digest(token, bearer))
    if not token_ok:
        bad = _require(request)
        if bad:
            return bad
    if not cfg.get("update_enabled"):
        return JSONResponse(
            {"error": {"message": "远程更新未开启(后台「设置 → 排队与其他」打开开关)"}},
            status_code=400,
        )
    import asyncio
    import server as _srv

    loop = asyncio.get_event_loop()
    ok, msg = await loop.run_in_executor(None, _srv._remote_update)
    if not ok:
        return JSONResponse({"error": {"message": msg}}, status_code=500)
    # 正常路径 execv 已在即,立即返回让客户端看到确认;演练时把结果说明带回
    return {"ok": True, "note": msg or "代码已覆盖,网关正在自动重启(数秒)"}


@router.post("/rollback")
async def remote_rollback(request: Request):
    """回滚到上次更新前的版本(代码+数据)。只能回滚一次,用完即清。

    鉴权与更新端点一致:Bearer <update_token> 或 admin 会话+CSRF。
    """
    cfg = STORE.load()["config"]
    token = str(cfg.get("update_token") or "")
    auth = request.headers.get("authorization") or ""
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    token_ok = bool(token and bearer and hmac.compare_digest(token, bearer))
    if not token_ok:
        bad = _require(request)
        if bad:
            return bad
    if not cfg.get("update_enabled"):
        return JSONResponse(
            {"error": {"message": "远程更新未开启,回滚不可用"}},
            status_code=400,
        )
    import asyncio
    import server as _srv

    loop = asyncio.get_event_loop()
    ok, msg = await loop.run_in_executor(None, _srv._remote_rollback)
    if not ok:
        return JSONResponse({"error": {"message": msg}}, status_code=500)
    return {"ok": True, "note": msg or "已回滚,网关正在自动重启(数秒)"}


# ============================================================ 拦截(自定义回复)


@router.get("/intercept")
async def intercept_get(request: Request):
    bad = _require(request)
    if bad:
        return bad
    db = STORE.load()
    cfg = db["config"]
    rules = [r for r in (cfg.get("custom_rules") or []) if isinstance(r, dict)]
    logs = [x for x in (db.get("intercepted") or []) if isinstance(x, dict)][:100]
    return {"enabled": bool(cfg.get("intercept_enabled")), "rules": rules, "logs": logs}


@router.post("/intercept/rules")
async def intercept_rule_add(request: Request):
    bad = _require(request)
    if bad:
        return bad
    body = await _json_dict(request)
    if body is None:
        return JSONResponse({"error": {"message": "请求体格式错误"}}, status_code=400)
    mode = str(body.get("match_mode") or "contains")
    if mode not in ("contains", "equals", "prefix", "suffix", "regex"):
        return JSONResponse({"error": {"message": "未知匹配模式"}}, status_code=400)
    # 先按存储上限截断、再校验:存进库的必须就是被校验过的那一份。
    # 旧写法先校验全文再截断存入,超长正则会存成另一个可能非法的模式(规则静默失效)。
    pattern = str(body.get("pattern") or "").strip()[:120]
    reply = str(body.get("reply") or "")[:2000]
    name = str(body.get("name") or "").strip()[:40]
    if not pattern or not reply:
        return JSONResponse({"error": {"message": "匹配内容与回复内容不能为空"}}, status_code=400)
    if mode == "regex":
        import re as _re

        try:
            _re.compile(pattern)
        except Exception as e:
            return JSONResponse({"error": {"message": f"正则无效: {e}"}}, status_code=400)
        # 嵌套量词((a+)+ / (.*)* / (a+){2,})在长输入上会灾难性回溯。网关是单进程
        # 事件循环,一旦卡住就是全站无响应,所以在添加时就拒掉并给出改法。
        if _re.search(r"\([^()]*[+*][^()]*\)\s*[+*{]", pattern):
            return JSONResponse(
                {"error": {"message": "正则含嵌套量词(如 (a+)+),长输入会灾难性回溯,请改写"}},
                status_code=400,
            )

    def _fn(db: dict):
        db["config"].setdefault("custom_rules", []).append({
            "id": "r_" + os.urandom(4).hex(),
            "name": name,
            "match_mode": mode,
            "pattern": pattern,
            "reply": reply,
        })

    await STORE.aupdate(_fn)
    STORE.flush()
    return {"ok": True}


@router.post("/intercept/rules/delete")
async def intercept_rule_delete(request: Request):
    bad = _require(request)
    if bad:
        return bad
    body = await _json_dict(request)
    if body is None:
        return JSONResponse({"error": {"message": "请求体格式错误"}}, status_code=400)
    rid = str(body.get("id") or "")
    removed = [False]

    def _fn(db: dict):
        rules = db["config"].get("custom_rules") or []
        new = [r for r in rules if not (isinstance(r, dict) and r.get("id") == rid)]
        if len(new) != len(rules):
            removed[0] = True
        db["config"]["custom_rules"] = new

    await STORE.aupdate(_fn)
    STORE.flush()
    return {"ok": removed[0]}


@router.post("/intercept/toggle")
async def intercept_toggle(request: Request):
    bad = _require(request)
    if bad:
        return bad
    body = await _json_dict(request)
    if body is None:
        return JSONResponse({"error": {"message": "请求体格式错误"}}, status_code=400)

    def _fn(db: dict):
        from core.util import as_bool

        db["config"]["intercept_enabled"] = as_bool(body.get("enabled"), False)

    await STORE.aupdate(_fn)
    STORE.flush()
    return {"ok": True}


@router.post("/intercept/clear")
async def intercept_clear(request: Request):
    bad = _require(request)
    if bad:
        return bad

    def _fn(db: dict):
        db["intercepted"] = []

    await STORE.aupdate(_fn)
    STORE.flush()
    return {"ok": True}


# ============================================================ 训练资料


@router.get("/training")
async def training_list(request: Request):
    bad = _require(request)
    if bad:
        return bad
    db = STORE.load()
    max_n = int(db["config"].get("training_log_max") or 0)
    try:
        n = max(1, min(200, int(request.query_params.get("n") or 50)))
    except (TypeError, ValueError):
        n = 50
    rows = list(db.get("training") or [])[:n]
    total = len(db.get("training") or [])
    return {"rows": rows, "total": total, "max": max_n}


@router.post("/training/clear")
async def training_clear(request: Request):
    bad = _require(request)
    if bad:
        return bad

    def _fn(db: dict):
        db["training"] = []

    await STORE.aupdate(_fn)
    STORE.flush()
    return {"ok": True}


@router.get("/training/export")
async def training_export(request: Request):
    """导出 JSONL:每行 {"messages":[...含 assistant 回复],"model":...},OpenAI 微调格式。"""
    bad = _require(request)
    if bad:
        return bad
    db = STORE.load()
    lines = []
    for e in list(db.get("training") or [])[::-1]:  # 时间正序导出
        if not isinstance(e, dict):
            continue
        msgs = [m for m in (e.get("messages") or []) if isinstance(m, dict)]
        msgs.append({"role": "assistant", "content": str(e.get("response") or "")})
        lines.append(json.dumps({"messages": msgs, "model": e.get("model") or ""}, ensure_ascii=False))
    return Response(
        "\n".join(lines) + ("\n" if lines else ""),
        media_type="application/jsonl; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=training.jsonl"},
    )


# ============================================================ 配置同步


@router.get("/sessions")
async def sessions_list(request: Request):
    bad = _require(request)
    if bad:
        return bad
    db = STORE.load()
    max_n = int(db["config"].get("session_log_max") or 100)
    return {"rows": (db.get("sessions") or [])[:max_n], "max": max_n}


@router.post("/sessions/clear")
async def sessions_clear(request: Request):
    bad = _require(request)
    if bad:
        return bad

    def _fn(db: dict):
        db["sessions"] = []

    await STORE.aupdate(_fn)
    STORE.flush()
    return {"ok": True}


@router.get("/config/export")
async def config_export(request: Request):
    bad = _require(request)
    if bad:
        return bad
    cfg = dict(STORE.load()["config"])
    cfg.pop("admin_password_hash", None)
    cfg.pop("session_secret", None)
    return {"_type": "gateway-config", "version": "1.5.0", "config": cfg}


@router.post("/config/import")
async def config_import(request: Request):
    bad = _require(request)
    if bad:
        return bad
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": {"message": "配置文件不是合法 JSON"}}, status_code=400)
    # 顶层不是 dict(数组/字符串等)时 body.get 会直接 500
    if not isinstance(body, dict):
        return JSONResponse({"error": {"message": "配置文件格式错误：顶层必须是对象"}}, status_code=400)
    incoming = body.get("config") if isinstance(body.get("config"), dict) else body
    applied = 0

    def _fn(db: dict):
        nonlocal applied
        cfg = db["config"]
        for k in INT_SETTINGS:
            if k in incoming and str(incoming[k]) != "":
                try:
                    cfg[k] = int(incoming[k])
                    applied += 1
                except (TypeError, ValueError):
                    pass
        for k in STR_SETTINGS:
            if k in incoming and isinstance(incoming[k], str):
                cfg[k] = incoming[k].strip()
                applied += 1
        for k in BOOL_SETTINGS:
            if k in incoming:
                # bool("false") 是 True：前端可能把开关序列化成字符串，必须按语义解析
                cfg[k] = as_bool(incoming[k], bool(cfg.get(k)))
                applied += 1
        gt = incoming.get("gateway_tokens")
        if isinstance(gt, list):
            struct = []
            for t in gt:
                if isinstance(t, str) and t:
                    struct.append({"t": t, "m": []})
                elif isinstance(t, dict) and t.get("t"):
                    struct.append({"t": str(t["t"]), "m": [str(x) for x in t.get("m", [])]})
            if struct:
                cfg["gateway_tokens"] = struct
                applied += 1

    await STORE.aupdate(_fn)
    STORE.flush()
    return {"ok": True, "applied": applied}
