# -*- coding: utf-8 -*-
import json, os, shutil, subprocess, sys, threading, time
import httpx

# 允许从任意目录运行：定位到项目根
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(os.environ.get("TEMP", "."), "ngw-reg4")

# 测试实例的管理员凭据：通过 NGW_ADMIN_PASSWORD 注入，避免依赖首次运行随机生成的密码
ADMIN_USER = "admin"
ADMIN_PW = "ngw-test-pass"
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)

mock = """
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
import json, asyncio
app = FastAPI()
_c = {"rl": 0, "flaky": 0}

@app.get("/v1/models")
async def models():
    return {"object":"list","data":[{"id":"mock-model","object":"model","created":0,"owned_by":"t"}]}

@app.post("/fail/v1/chat/completions")
async def chat_fail(request: Request):
    # 只按「渠道」失败的路径：用于验证按模型的可靠性路由
    await request.json()
    return JSONResponse({"error":{"message":"this channel is broken for the model"}}, status_code=500)

@app.post("/v1/chat/completions")
async def chat(request: Request):
    b = await request.json()
    m = b.get("model","")
    eff = b.get("thinking_effort") or b.get("reasoning_effort") or ""
    st = b.get("stream")
    if m == "kimi" and eff and eff != "low":
        return JSONResponse({"error":{"message":"Unsupported thinking_effort"}}, status_code=400)
    if m == "empty":
        return JSONResponse({}, status_code=200)
    if m == "flaky":
        _c["flaky"] += 1
        if _c["flaky"] == 1: return JSONResponse({"error":{"message":"t"}}, status_code=502)
    if m == "rl":
        _c["rl"] += 1
        if _c["rl"] <= 2: return JSONResponse({"error":{"message":"rate limited"}}, status_code=429)
    if m == "chdown":
        return JSONResponse({"error":{"message":"No available channel"}}, status_code=500)
    if m == "nousage":
        # 上游不返回 usage —— 严格客户端(New-API)会因此判渠道测试失败
        return {"id":"c1","object":"chat.completion","model":m,
                "choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}
    if m == "ssejson":
        # 上游用 application/json 声明 SSE 体 —— 原样转发会让客户端按 JSON 解析而失败
        async def g3():
            yield "data: " + json.dumps({"model":m,"choices":[{"delta":{"content":"Hi"}}]}) + "\\n\\n"
            yield "data: [DONE]\\n\\n"
        return StreamingResponse(g3(), media_type="application/json")
    if m == "slowfirst":
        # 模拟推理模型首字节很慢：期间网关必须先发心跳占住连接
        async def g4():
            await asyncio.sleep(9)
            yield "data: " + json.dumps({"model":m,"choices":[{"delta":{"content":"Slow"}}]}) + "\\n\\n"
            yield "data: [DONE]\\n\\n"
        return StreamingResponse(g4(), media_type="text/event-stream")
    if m == "slowhdr":
        # 先等待再构造响应 —— 让上游连「响应头」都迟迟不返回（推理模型排队时的真实表现）
        await asyncio.sleep(20)
        async def g5():
            yield "data: " + json.dumps({"model":m,"choices":[{"delta":{"content":"Hdr"}}]}) + "\\n\\n"
            yield "data: [DONE]\\n\\n"
        return StreamingResponse(g5(), media_type="text/event-stream")
    if m == "brokenstream":
        # 上游下发一部分后突然断开（无 [DONE]）——网关必须显式报错，不能当正常结束
        async def g6():
            yield "data: " + json.dumps({"model": m, "choices": [{"delta": {"content": "part"}}]}) + "\\n\\n"
            raise RuntimeError("upstream died mid-stream")
        return StreamingResponse(g6(), media_type="text/event-stream")
    if m == "hold4":
        await asyncio.sleep(4)   # 占住并发，用于逼出排队
        return {"id":"c1","object":"chat.completion","model":m,
                "choices":[{"index":0,"message":{"role":"assistant","content":"held"},"finish_reason":"stop"}],
                "usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}
    if m == "emptystream":
        # 流式响应但没有任何内容：会走「空流保护」，属于未交接给透传的失败路径
        async def g7():
            if False:
                yield b""
        return StreamingResponse(g7(), media_type="text/event-stream")
    if st:
        async def g():
            for t in ["Hello"," world"]:
                yield "data: " + json.dumps({"model":m,"choices":[{"delta":{"content":t}}]}) + "\\n\\n"
                await asyncio.sleep(0.1)
            yield "data: [DONE]\\n\\n"
        return StreamingResponse(g(), media_type="text/event-stream")
    return {"id":"c1","object":"chat.completion","model":m,
            "choices":[{"index":0,"message":{"role":"assistant","content":"ok"},"finish_reason":"stop"}],
            "usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}
"""
open(os.path.join(TMP, "mock.py"), "w", encoding="utf-8").write(mock)

env = {**os.environ, "NGW_DATA_DIR": TMP, "NGW_ADMIN_PASSWORD": ADMIN_PW}
mk = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "mock:app", "--port", "18212"],
    cwd=TMP,
    env=env,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
gw = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "server:app", "--port", "18213"],
    cwd=ROOT,
    env=env,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
try:
    for _ in range(100):
        try:
            if (
                httpx.get("http://127.0.0.1:18213/", timeout=2).status_code == 200
                and httpx.post(
                    "http://127.0.0.1:18212/v1/chat/completions", json={"model": "x"}, timeout=2
                ).status_code
                == 200
            ):
                break
        except Exception:
            pass
        time.sleep(0.4)
    a = httpx.Client(base_url="http://127.0.0.1:18213", timeout=60)
    r = a.post("/api/login", json={"username": ADMIN_USER, "password": ADMIN_PW})
    a.headers["X-CSRF"] = r.json()["csrf"]
    a.post(
        "/api/settings",
        json={
            "config": {
                "rate_limit_per_minute": 100000,
                "account_cooldown_ms": -1,
                "warmup_seconds": 0,
                "queue_enabled": True,
                "queue_max_wait": 8,
                "queue_poll_ms": 150,
                "max_retries": 2,
                "retry_backoff_base_ms": 10,
                "retry_backoff_max_ms": 20,
                "retry_min_wait_ms": 0,
                "ban_step_seconds": 0,
                "ban_max_seconds": 0,
                "daily_request_cap": 0,
                "daily_token_limit": 0,
                "hourly_request_limit": 0,
                "cool_429_seconds": 1,
                "cool_5xx_seconds": 0,
                "breaker_enabled": False,
                "ttfb_timeout": 30,
                "sse_idle_timeout": 30,
            }
        },
    )
    r = a.post(
        "/api/upstreams",
        json={
            "name": "T",
            "base": "http://127.0.0.1:18212/v1",
            "enabled": True,
            "thinking_defaults": "kimi=low",
        },
    )
    uid = r.json()["upstream"]["id"]
    a.post(
        "/api/keys/import",
        json={
            "text": "u@e.com,p,nvapi-tk12345678\nu2@e.com,p,nvapi-tk22345678\nu3@e.com,p,nvapi-tk32345678",
            "upstream_id": uid,
        },
    )
    toks = a.get("/api/settings").json()["gateway_tokens"]
    c = httpx.Client(
        base_url="http://127.0.0.1:18213", timeout=60, headers={"Authorization": "Bearer " + toks[0]["t"]}
    )

    results = []

    def add(n, ok, d=""):
        results.append((n, bool(ok), d))

    # RPM 时间戳只能记给「真正被使用」的账号。曾经给所有候选账号都打点，导致每个账号的
    # 60 秒窗口被无谓塞满、整池一起撞上单账号上限 → 号池假性枯竭（吞吐被压到约等于单账号
    # RPM，与账号数量无关）。此处号池刚建、窗口为空，正好验证：3 账号 × rpm=4 ⇒ 12 次全过。
    a.post("/api/settings", json={"config": {"rate_limit_per_minute": 4}})
    ok_rpm = 0
    for _ in range(12):
        r = c.post(
            "/v1/chat/completions",
            json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        if r.status_code == 200:
            ok_rpm += 1
    add("RPM 按账号计而非全池", ok_rpm == 12, "rpm=4 × 3 账号，12 次全部成功（%d/12）" % ok_rpm)
    a.post("/api/settings", json={"config": {"rate_limit_per_minute": 100000}})

    r = c.post(
        "/v1/chat/completions", json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]}
    )
    add(
        "chat 非流式",
        r.status_code == 200 and r.json().get("object") == "chat.completion",
        "st=%s" % r.status_code,
    )

    t0 = time.time()
    first = None
    parts = []
    code = 0
    with c.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "mock-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as r:
        for line in r.iter_lines():
            if line and first is None:
                first = time.time() - t0
            parts.append(line)
        code = r.status_code
    body = "|".join(parts)
    total = time.time() - t0
    add(
        "chat 流式(真流式)",
        code == 200 and "Hello" in body and "[DONE]" in body and first is not None and first < total,
        "首字=%.2fs 总=%.2fs" % (first or -1, total),
    )

    r = c.post(
        "/v1/chat/completions",
        json={"model": "kimi", "thinking_effort": "medium", "messages": [{"role": "user", "content": "hi"}]},
    )
    add("thinking_effort 降级", r.status_code == 200, "st=%s" % r.status_code)
    r = c.post(
        "/v1/chat/completions", json={"model": "empty", "messages": [{"role": "user", "content": "hi"}]}
    )
    add("空响应保护", r.status_code >= 400, "st=%s" % r.status_code)
    r = c.post(
        "/v1/chat/completions", json={"model": "flaky", "messages": [{"role": "user", "content": "hi"}]}
    )
    add("同号重试", r.status_code == 200, "st=%s" % r.status_code)
    r = c.post("/v1/chat/completions", json={"model": "rl", "messages": [{"role": "user", "content": "hi"}]})
    add("429 吸收", r.status_code == 200, "st=%s" % r.status_code)
    r = c.post(
        "/v1/chat/completions", json={"model": "chdown", "messages": [{"role": "user", "content": "hi"}]}
    )
    add("渠道级快速失败", r.status_code >= 400, "st=%s" % r.status_code)
    # /v1/models 语义：渠道未配置模型白名单时网关是透传的，任意模型名都放行
    add("models 列表", c.get("/v1/models").status_code == 200)
    add("models/{已知}", c.get("/v1/models/mock-model").status_code == 200)
    add(
        "models/{未知}放行(未配置白名单)",
        c.get("/v1/models/nope").status_code == 200,
        "st=%s" % c.get("/v1/models/nope").status_code,
    )

    # 配置白名单后：/v1/models 只返回网关支持的模型（绝不能泄漏上游真实模型清单），
    # 且未知模型必须 404。上游模型列表刻意不访问，所以这里也验证不发生上游请求。
    allow = "mock-model,kimi,empty,flaky,rl,chdown,nousage,ssejson,slowfirst,slowhdr"
    a.post(
        "/api/upstreams",
        json={"id": uid, "name": "T", "base": "http://127.0.0.1:18212/v1", "enabled": True, "models": allow},
    )
    ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
    add(
        "models 只返回网关配置的模型",
        sorted(ids) == sorted(allow.split(",")),
        "共 %d 个：%s" % (len(ids), ids[:4]),
    )
    add(
        "models/{未知}404(配置白名单后)",
        c.get("/v1/models/nope").status_code == 404,
        "st=%s" % c.get("/v1/models/nope").status_code,
    )
    a.post(
        "/api/upstreams",
        json={
            "id": uid,
            "name": "T",
            "base": "http://127.0.0.1:18212/v1",
            "enabled": True,
            "models": "mock-model,shadow-model",
            "model_map": "my-alias=shadow-model",
            "hide_mapped": 1,
        },
    )
    ids = [m["id"] for m in c.get("/v1/models").json()["data"]]
    add(
        "禁用原名时清单剔除原名",
        "shadow-model" not in ids and "my-alias" in ids and "mock-model" in ids,
        "清单=%s（上游原名 shadow-model 已剔除）" % ids,
    )
    a.post(
        "/api/upstreams",
        json={
            "id": uid,
            "name": "T",
            "base": "http://127.0.0.1:18212/v1",
            "enabled": True,
            "models": "",
            "model_map": "",
            "hide_mapped": 0,
        },
    )

    # 模型清单解析容错：把从 Python/JSON 复制来的 ['a','b'] 粘进输入框，不能整段当成一个
    # 模型名存下来（否则白名单里挂着一个永远匹配不上的名字，该渠道等于废掉）。
    # 同时模型名自带 [free] 之类后缀必须原样保留。
    for raw, want in (
        ("['mock-model']", ["mock-model"]),
        ('["mock-model"]', ["mock-model"]),
        ("mock-model,shadow-model", ["mock-model", "shadow-model"]),
        ("Suffix-Model[free]", ["Suffix-Model[free]"]),
    ):
        a.post(
            "/api/upstreams",
            json={
                "id": uid,
                "name": "T",
                "base": "http://127.0.0.1:18212/v1",
                "enabled": True,
                "models": raw,
            },
        )
        got = next(x for x in (a.get("/api/upstreams").json().get("rows") or []) if x["id"] == uid)["models"]
        add("模型清单解析容错", got == want, "%r -> %s" % (raw, got))
    a.post(
        "/api/upstreams",
        json={"id": uid, "name": "T", "base": "http://127.0.0.1:18212/v1", "enabled": True, "models": ""},
    )

    # 回归线上真实故障：列表页的「启用/禁用」按钮曾把整行对象回传（models 是数组、
    # model_map 是字典），后端按文本解析就把白名单写成了 ["['a']"] —— 该渠道从此对
    # 任何请求都判「渠道模型不匹配」，等于废掉。停用→启用一轮后配置必须原样完好。
    def _up_row():
        return next(x for x in (a.get("/api/upstreams").json().get("rows") or []) if x["id"] == uid)

    a.post(
        "/api/upstreams",
        json={
            "id": uid,
            "name": "T",
            "base": "http://127.0.0.1:18212/v1",
            "enabled": True,
            "models": ["mock-model"],
            "model_map": {"ali": "upstream-x"},
        },
    )
    row = _up_row()
    a.post("/api/upstreams", json={**row, "enabled": False})  # 模拟点击「停用」
    off = _up_row()
    a.post("/api/upstreams", json={**off, "enabled": True})  # 模拟点击「启用」
    on = _up_row()
    add(
        "停用/启用不改写渠道配置",
        off["enabled"] is False
        and on["enabled"] is True
        and off["models"] == ["mock-model"]
        and on["models"] == ["mock-model"]
        and off["model_map"] == {"ali": "upstream-x"}
        and on["model_map"] == {"ali": "upstream-x"},
        "models=%s model_map=%s" % (on["models"], on["model_map"]),
    )
    a.post(
        "/api/upstreams",
        json={
            "id": uid,
            "name": "T",
            "base": "http://127.0.0.1:18212/v1",
            "enabled": True,
            "models": "",
            "model_map": "",
        },
    )
    add("responses", c.post("/v1/responses", json={"model": "mock-model", "input": "hi"}).status_code == 200)
    add(
        "messages",
        c.post(
            "/v1/messages",
            json={"model": "mock-model", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]},
        ).status_code
        == 200,
    )
    add("queue API", a.get("/api/queue").status_code == 200)

    # 上游漏字段时的响应补全：严格客户端(New-API 渠道测试/计费)靠这些字段判定成败
    r = c.post(
        "/v1/chat/completions", json={"model": "nousage", "messages": [{"role": "user", "content": "hi"}]}
    )
    u = (r.json() or {}).get("usage") if r.status_code == 200 else None
    add(
        "缺 usage 自动补齐",
        r.status_code == 200
        and isinstance(u, dict)
        and isinstance(u.get("total_tokens"), int)
        and u["total_tokens"] == u.get("prompt_tokens", 0) + u.get("completion_tokens", 0),
        "usage=%s" % (u,),
    )
    ct = ""
    with c.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "ssejson", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as r:
        ct = r.headers.get("content-type", "")
        sbody = "|".join(l for l in r.iter_lines() if l)
    add("流式强制 text/event-stream", ct.startswith("text/event-stream") and "[DONE]" in sbody, "ct=%s" % ct)

    # 上游首字节很慢时：必须靠保活帧占住连接，否则中间代理空闲超时会先把连接掐断，
    # 客户端只看到「请求失败」而网关日志却记 200（New-API 渠道测试失败的典型成因）。
    # 保活帧必须是合法 data: 空 chunk —— 中转网关(New-API)只转发 data: 行，
    # 注释行会被丢弃，下游那段仍会静默超时并报 client_gone。
    def _is_keepalive(line):
        if not line.startswith("data:"):
            return False
        p = line[5:].strip()
        if not p or p == "[DONE]":
            return False
        try:
            j = json.loads(p)
        except Exception:
            return False
        ch = (j.get("choices") or [{}])[0]
        if "delta" in ch:
            d = ch.get("delta") or {}
            return not (d.get("content") or d.get("reasoning_content"))
        return not ch.get("text")

    t0 = time.time()
    ping_at = data_at = None
    parts = []
    comments = 0
    with c.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "slowfirst", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as r:
        code = r.status_code
        for line in r.iter_lines():
            if not line:
                continue
            if line.startswith(":"):
                comments += 1
            if _is_keepalive(line) and ping_at is None:
                ping_at = time.time() - t0
            if line.startswith("data:") and not _is_keepalive(line) and data_at is None:
                data_at = time.time() - t0
            parts.append(line)
    sbody2 = "|".join(parts)
    add(
        "慢首字节保活(可穿透中转网关)",
        code == 200
        and ping_at is not None
        and data_at is not None
        and ping_at < data_at
        and "Slow" in sbody2
        and "[DONE]" in sbody2
        and comments == 0,
        "保活帧=%.1fs 首数据=%.1fs 注释行=%d 总=%.1fs"
        % (ping_at or -1, data_at or -1, comments, time.time() - t0),
    )

    # 上游连响应头都迟迟不返回（实测 NVIDIA kimi-k3 要 128s）：网关卡在 client.send() 里
    # 发不出任何东西，必须靠兜底保活流占住连接，否则中间 nginx 60s 空闲超时会先掐断
    t0 = time.time()
    hb = []
    dd = []
    parts3 = []
    with c.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "slowhdr", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as r:
        code3 = r.status_code
        for line in r.iter_lines():
            el = time.time() - t0
            if _is_keepalive(line):
                hb.append(round(el, 1))
            elif line.startswith("data:"):
                dd.append(round(el, 1))
                parts3.append(line)
    body3 = "|".join(parts3)
    add(
        "响应头延迟时保活",
        code3 == 200 and hb and dd and hb[0] < dd[0] and "Hdr" in body3 and "[DONE]" in body3,
        "心跳=%s 首数据=%s" % (hb[:3], dd[:1]),
    )

    time.sleep(2)  # 等前面的 429 冷却结束；否则健康号会被集中使用（限流保护，非 bug）
    before = [k.get("total_requests", 0) for k in a.get("/api/keys").json()["rows"]]
    for _ in range(12):
        c.post(
            "/v1/chat/completions",
            json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
        )
    now = [k.get("total_requests", 0) for k in a.get("/api/keys").json()["rows"]]
    delta = sorted([n - b for n, b in zip(now, before)])
    add(
        "账号轮换分散",
        sum(delta) == 12 and delta[-1] - delta[0] <= 2,
        "增量=%s 累计=%s" % (delta, sorted(now)),
    )

    # 断连回收：客户端在「上游还没返回响应头」时放弃，网关必须检测到并立刻释放账号。
    # 注意：必须用原始 socket 真实关闭 TCP —— httpx 的 close() 只是把连接还回连接池，
    # 连接并未断开，服务端不会收到 disconnect，用它测断连会得出错误结论。
    ids = [k["id"] for k in a.get("/api/keys").json()["rows"]]
    for kid in ids[1:]:
        a.post("/api/keys/op", json={"op": "disable", "id": kid})
    a.post("/api/settings", json={"config": {"acct_concurrency": 1}})

    import socket

    def _slow_disconnect():
        CRLF = "\r\n"
        body = json.dumps(
            {"model": "slowhdr", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
        ).encode()
        head = (
            "POST /v1/chat/completions HTTP/1.1"
            + CRLF
            + "Host: 127.0.0.1"
            + CRLF
            + "Authorization: Bearer "
            + toks[0]["t"]
            + CRLF
            + "Content-Type: application/json"
            + CRLF
            + "Content-Length: "
            + str(len(body))
            + CRLF
            + CRLF
        ).encode()
        sk = socket.create_connection(("127.0.0.1", 18213), timeout=10)
        try:
            sk.sendall(head + body)
            time.sleep(3.0)  # 上游 20s 才返回响应头，此刻仍在等待/保活阶段
        finally:
            sk.close()  # 真实关闭 TCP，服务端应收到 disconnect

    th = threading.Thread(target=_slow_disconnect, daemon=True)
    th.start()
    th.join(10)
    t0 = time.time()
    freed = False
    while time.time() - t0 < 20:
        r = c.post(
            "/v1/chat/completions",
            json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        if r.status_code == 200:
            freed = True
            break
        time.sleep(1.0)
    add("断连后账号立即回收", freed, "断开后 %.1fs 内账号可复用" % (time.time() - t0))
    # 恢复
    a.post("/api/settings", json={"config": {"acct_concurrency": 0}})
    for kid in ids[1:]:
        a.post("/api/keys/op", json={"op": "enable", "id": kid})

    # 账户锁定兜底：httpx.InvalidURL 不是 httpx.HTTPError 的子类，非法 base URL
    # （例如 http://[bad，仍能通过 http(s):// 前缀校验）会穿透原有捕获，把账号永久
    # 锁在该请求上。现在 _proxy/_convert 的整个重试循环外层有 try/finally 兜底释放。
    a.post("/api/settings", json={"config": {"acct_concurrency": 1}})
    a.post("/api/upstreams", json={"id": uid, "name": "T", "base": "http://[bad/v1", "enabled": True})
    r = c.post(
        "/v1/chat/completions", json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]}
    )
    bad_st = r.status_code
    a.post(
        "/api/upstreams", json={"id": uid, "name": "T", "base": "http://127.0.0.1:18212/v1", "enabled": True}
    )
    t0 = time.time()
    ok = False
    while time.time() - t0 < 10:
        rr = c.post(
            "/v1/chat/completions",
            json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        if rr.status_code == 200:
            ok = True
            break
        time.sleep(0.5)
    add(
        "异常路径不锁定账号",
        ok and bad_st >= 400,
        "非法 base 时 st=%s；恢复后 %.1fs 内账号可用" % (bad_st, time.time() - t0),
    )
    a.post("/api/settings", json={"config": {"acct_concurrency": 0}})

    # 上游中途断流（无 [DONE]）必须显式报错：静默截断比报错危险得多 ——
    # 下游会把半截内容当成完整回复。网关应补发 error 事件 + [DONE] 收口。
    t0 = time.time()
    lines = []
    with c.stream(
        "POST",
        "/v1/chat/completions",
        json={"model": "brokenstream", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as r:
        code = r.status_code
        for line in r.iter_lines():
            if line:
                lines.append(line)
    body = "|".join(lines)
    add(
        "上游断流显式报错(不静默截断)",
        code == 200 and "上游流中断" in body and "[DONE]" in body,
        "%d 行，%.1fs" % (len(lines), time.time() - t0),
    )

    # 连接泄漏：流式失败路径（空流）曾经不关闭上游响应，连接被一直占住；反复失败会耗尽
    # 共享连接池（max_connections=100），之后所有上游请求都卡在「等连接」直到超时 ——
    # 生产日志就表现为清一色「上游返回 HTTP 0」。这里连打 120 次（超过池上限），
    # 随后普通请求必须仍然可用。
    n_fail = 0
    for _ in range(120):
        rr = c.post(
            "/v1/chat/completions",
            json={"model": "emptystream", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )
        if rr.status_code >= 400:
            n_fail += 1
    t0 = time.time()
    rr = c.post(
        "/v1/chat/completions",
        json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    # 排队：曾经有人反馈「进了队列就不见动静」。实测排队中的请求会持续重试取号，
    # 账号/并发一释放就能拿到。这里用 total_concurrency=1 + 一个占 4 秒的请求逼出排队。
    a.post("/api/settings", json={"config": {"total_concurrency": 1, "queue_max_wait": 15}})
    _qres = {}

    def _qhold():
        cc = httpx.Client(
            base_url="http://127.0.0.1:18213", timeout=60, headers={"Authorization": "Bearer " + toks[0]["t"]}
        )
        cc.post(
            "/v1/chat/completions", json={"model": "hold4", "messages": [{"role": "user", "content": "hold"}]}
        )

    def _qwait(i):
        cc = httpx.Client(
            base_url="http://127.0.0.1:18213", timeout=60, headers={"Authorization": "Bearer " + toks[0]["t"]}
        )
        t = time.time()
        rr = cc.post(
            "/v1/chat/completions",
            json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        _qres[i] = (rr.status_code, round(time.time() - t, 1))

    th0 = threading.Thread(target=_qhold, daemon=True)
    th0.start()
    time.sleep(0.6)  # 等它确实占住并发
    ths = [threading.Thread(target=_qwait, args=(i,), daemon=True) for i in range(2)]
    for t in ths:
        t.start()
    time.sleep(0.8)
    qlen = a.get("/api/queue").json().get("length")
    for t in ths:
        t.join(30)
    add(
        "排队中的请求会持续取号",
        all(v[0] == 200 for v in _qres.values()) and qlen >= 1,
        "排队长度=%s 结果=%s" % (qlen, sorted(_qres.values())),
    )
    a.post("/api/settings", json={"config": {"total_concurrency": 0}})

    add(
        "流式失败不泄漏连接池",
        rr.status_code == 200,
        "120 次空流失败（%d）后普通请求 st=%s（%.1fs）" % (n_fail, rr.status_code, time.time() - t0),
    )

    # 可靠性按「渠道+模型」定：同一模型在一个渠道上一直失败、在另一个渠道正常时，
    # 路由必须学会跳过坏渠道，而不是每次都先撞一次 500 再重试。
    a.post(
        "/api/upstreams",
        json={
            "id": uid,
            "name": "T",
            "base": "http://127.0.0.1:18212/v1",
            "enabled": True,
            "models": "mock-model",
        },
    )
    bad = a.post(
        "/api/upstreams",
        json={"name": "BAD", "base": "http://127.0.0.1:18212/fail/v1", "enabled": True, "models": "mx"},
    ).json()["upstream"]["id"]
    good = a.post(
        "/api/upstreams",
        json={"name": "GOOD", "base": "http://127.0.0.1:18212/v1", "enabled": True, "models": "mx"},
    ).json()["upstream"]["id"]
    a.post(
        "/api/keys/import",
        json={"text": "bad@e.com,p,nvapi-tkbd123456\n" "good@e.com,p,nvapi-tkgd123456", "upstream_id": bad},
    )
    a.post("/api/keys/import", json={"text": "good2@e.com,p,nvapi-tkgd234567", "upstream_id": good})
    a.post("/api/settings", json={"config": {"max_retries": 2, "retry_min_wait_ms": 0}})

    ok_n = 0
    for _ in range(6):
        r = c.post(
            "/v1/chat/completions", json={"model": "mx", "messages": [{"role": "user", "content": "hi"}]}
        )
        if r.status_code == 200:
            ok_n += 1
    # 日志行格式：[t, ep, model, key, status, ms, err, ip, ...] → model 在 idx 2，status 在 idx 4
    fails = sum(1 for x in a.get("/api/logs").json()["rows"] if x[2] == "mx" and x[4] == 500)
    add(
        "按模型学习渠道可靠性",
        ok_n == 6 and 0 < fails <= 3,
        "6/6 成功，坏渠道只被撞 %d 次（撞过即学会跳过）" % fails,
    )

    # 清理：禁用而非删除 —— 渠道下还有账号时删除接口会拒绝，账号会残留在号池里
    a.post(
        "/api/upstreams",
        json={
            "id": bad,
            "name": "BAD",
            "base": "http://127.0.0.1:18212/fail/v1",
            "enabled": False,
            "models": "mx",
        },
    )
    a.post(
        "/api/upstreams",
        json={
            "id": good,
            "name": "GOOD",
            "base": "http://127.0.0.1:18212/v1",
            "enabled": False,
            "models": "mx",
        },
    )
    a.post(
        "/api/upstreams",
        json={"id": uid, "name": "T", "base": "http://127.0.0.1:18212/v1", "enabled": True, "models": ""},
    )

    # 错误分级契约：402 必须按硬失败处理（余额耗尽的账号重试无意义，会被反复选中反复失败），
    # 401/403 归鉴权、429 归限流、0/超时归连接、5xx 归上游故障。
    sys.path.insert(0, ROOT)
    from core import pool as _pool

    want = {
        (402, "payment"),
        (401, "auth"),
        (403, "auth"),
        (429, "429"),
        (500, "5xx"),
        (503, "5xx"),
        (0, "conn"),
    }
    got = {st: _pool._classify(st, 0, "") for st, _ in want}
    wrong = ["%s->%s(期望%s)" % (st, got[st], c) for st, c in want if got[st] != c]
    add("错误分级契约", not wrong, "；".join(wrong) if wrong else "402/401/403/429/5xx/conn 归类正确")

    import asyncio

    first_bad = []

    async def w(_):
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:18213", timeout=60, headers={"Authorization": "Bearer " + toks[0]["t"]}
        ) as cl:
            ok = 0
            for _i in range(3):
                rr = await cl.post(
                    "/v1/chat/completions",
                    json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
                )
                if rr.status_code == 200:
                    ok += 1
                elif len(first_bad) < 2:
                    first_bad.append("%s %s" % (rr.status_code, rr.text[:150]))
            return ok

    async def main():
        return await asyncio.gather(*[w(i) for i in range(20)])

    total_ok = sum(asyncio.run(main()))
    if total_ok != 60:
        kk = a.get("/api/keys").json()["rows"]
        first_bad.append(
            " | 账号状态: "
            + "; ".join(
                "%s ban=%s daily=%s fails=%s"
                % (
                    k["email"][:3],
                    k.get("ban_reason"),
                    sum((v or {}).get("requests", 0) for v in (k.get("daily") or {}).values()),
                    k.get("consecutive_failures"),
                )
                for k in kk
            )
        )
    add("20并发x3=60请求", total_ok == 60, "%d/60 %s" % (total_ok, first_bad))

    # 登录限流：以前只过滤时间戳、从不记录本次尝试，len() 恒为 0 → 限流完全失效，
    # 口令可以无限暴力尝试。这里用错口令连续打满配额，必须被 429 挡住。
    # 本用例会把这个来源 IP 锁 5 分钟，所以放在最后（成功登录会清零配额，用错口令不会）。
    lcodes = []
    for _ in range(12):
        rr = httpx.post(
            "http://127.0.0.1:18213/api/login", json={"username": ADMIN_USER, "password": "definitely-wrong"}
        )
        lcodes.append(rr.status_code)
    add("登录尝试限流生效", 401 in lcodes and 429 in lcodes, "末尾状态码=%s" % lcodes[-4:])

    print("\n===== RESULTS =====")
    all_ok = True
    for n, ok, d in results:
        print(("PASS" if ok else "FAIL"), n, ("| " + d) if d else "")
        if not ok:
            all_ok = False
    print("ALL PASS" if all_ok else "FAILURES")
finally:
    gw.terminate()
    gw.wait()
    mk.terminate()
    mk.wait()
    shutil.rmtree(TMP, ignore_errors=True)
