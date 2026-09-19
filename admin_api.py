"""管理端 API：认证、概览、账号/渠道/日志/排队/设置/配置同步。"""

from __future__ import annotations

import hmac
import json
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
    resp = JSONResponse({"ok": True, "csrf": csrf})
    resp.set_cookie("ngw_session", session, httponly=True, samesite="lax")
    resp.set_cookie(CSRF_COOKIE, csrf, httponly=False, samesite="lax")
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
    minute = time.strftime("%Y%m%d%H", time.gmtime(now))
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    counts = {"total": 0, "enabled": 0, "banned": 0, "invalid": 0, "manual_disabled": 0}
    daily_req = daily_tok = 0
    risky = []
    for k in db["keys"]:
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
    rpm = sum((b.get(minute) or 0) for b in db.get("buckets", {}).values() if isinstance(b, dict))
    today = db["stats"].get(day) or {"total": 0, "success": 0, "fail": 0, "models": {}}
    models = sorted(today["models"].items(), key=lambda x: -x[1])[:10]
    recent_errors = [r for r in db.get("logs", []) if (r[4] if len(r) > 4 else 0) >= 400][:8]
    return {
        "keys": counts,
        "rpm": rpm,
        "rpm_limit_total": (
            -1
            if int(cfg.get("rate_limit_per_minute") or 0) == -1
            else counts["enabled"] * max(1, int(cfg.get("rate_limit_per_minute") or 20))
        ),
        "daily": {"requests": daily_req, "tokens": daily_tok},
        "queue": len(db.get("queue", [])),
        "today": {
            "total": today["total"],
            "success": today["success"],
            "fail": today["fail"],
            "rate": round(today["success"] * 100 / today["total"]) if today["total"] else None,
        },
        "models": [{"model": m, "count": c} for m, c in models],
        "recent_errors": recent_errors,
        "risky": risky[:10],
        "server_time": now,
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
    page = max(1, int(request.query_params.get("page") or 1))
    per = 20
    minute = time.strftime("%Y%m%d%H", time.gmtime())
    now = int(time.time())
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    up_names = {u["id"]: u["name"] for u in db.get("upstreams", [])}
    rows = []
    for k in reversed(db["keys"]):
        if q and q not in k["email"].lower() and q not in k["apikey"].lower():
            continue
        enabled = bool(k.get("enabled"))
        banned = (k.get("banned_until") or 0) > now
        if status == "active" and (not enabled or banned):
            continue
        if status == "disabled" and enabled and not banned:
            continue
        k = dict(k)
        k["rpm_used"] = len([t for t in (db.get("buckets", {}).get(k["id"]) or []) if now - t < 60])
        k["fail_ratio"] = round(k["total_fail"] * 100 / k["total_requests"]) if k.get("total_requests") else 0
        k["today"] = (k.get("daily") or {}).get(day) or {"requests": 0, "tokens": 0}
        k["upstream_name"] = up_names.get(k.get("upstream_id"), "-")
        rows.append(k)
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
    for k in db["keys"]:
        if k["id"] == kid:
            now = int(time.time())
            day = time.strftime("%Y-%m-%d", time.localtime(now))
            k = dict(k)
            k["rpm_used"] = len([t for t in (db.get("buckets", {}).get(kid) or []) if now - t < 60])
            k["today"] = (k.get("daily") or {}).get(day) or {"requests": 0, "tokens": 0}
            k["rate_limit"] = int(db["config"]["rate_limit_per_minute"])
            for u in db.get("upstreams", []):
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
    for k in db["keys"]:
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
    body = await request.json()
    op = str(body.get("op") or "")
    kid = str(body.get("id") or "")
    try:
        if op == "test":
            return {"ok": True, "test": pool.test_key(kid)}
        fn = {
            "enable": lambda: pool.set_enabled(kid, True),
            "disable": lambda: pool.set_enabled(kid, False),
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
    body = await request.json()
    op = str(body.get("op") or "")
    ids = [str(i) for i in (body.get("ids") or []) if str(i)]
    if not ids:
        return JSONResponse({"error": {"message": "未选择账号"}}, status_code=400)
    if op == "test":
        results = {}
        for i in ids[:20]:
            try:
                results[i] = pool.test_key(i)
            except Exception as e:
                results[i] = {"ok": False, "error": str(e)}
        return {"ok": True, "results": results}
    results: dict = {}
    if op == "enable":
        for i in ids:
            results[i] = pool.set_enabled(i, True)
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
    body = await request.json()
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
    minute = time.strftime("%Y%m%d%H", time.gmtime(now))
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
    body = await request.json()
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
    body = await request.json()
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
    return {"rows": db.get("logs", []), "enabled": bool(db["config"].get("log_enabled", True))}


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
    "request_timeout",
    "connect_timeout",
    "log_max",
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
}
BOOL_SETTINGS = {
    "log_enabled",
    "verify_tls",
    "queue_enabled",
    "hide_upstream_errors",
    "hide_mapped_names",
    "breaker_enabled",
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
    body = await request.json()
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
    body = await request.json()
    cfg = STORE.load()["config"]
    if not verify_password(str(body.get("old") or ""), str(cfg.get("admin_password_hash") or "")):
        return JSONResponse({"error": {"message": "当前密码错误"}}, status_code=400)
    new = str(body.get("new") or "")
    if len(new) < 6:
        return JSONResponse({"error": {"message": "新密码至少 6 位"}}, status_code=400)

    def _fn(db: dict):
        from core.store import _hash_password

        db["config"]["admin_password_hash"] = _hash_password(new)

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
