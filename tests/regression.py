# -*- coding: utf-8 -*-
import json, os, shutil, subprocess, sys, time
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
    add("models 列表", c.get("/v1/models").status_code == 200)
    add("models/{已知}", c.get("/v1/models/mock-model").status_code == 200)
    add("models/{未知}404", c.get("/v1/models/nope").status_code == 404)
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

    import asyncio

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
            return ok

    async def main():
        return await asyncio.gather(*[w(i) for i in range(20)])

    total_ok = sum(asyncio.run(main()))
    add("20并发x3=60请求", total_ok == 60, "%d/60" % total_ok)

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
