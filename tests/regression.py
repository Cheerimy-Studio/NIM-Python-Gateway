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
    if m == "authfail":
        # 401:网关按鉴权失败硬封禁该账号(hard_fail_ban_seconds)
        return JSONResponse({"error":{"message":"invalid api key"}}, status_code=401)
    if b.get("tool_choice") is not None and not b.get("tools"):
        # 模拟严格校验的上游(FastAPI/vLLM 系):tool_choice 无 tools 直接拒绝 ——
        # 网关必须在转发前清理这种无意义组合
        return JSONResponse(
            {"detail": [{"type": "value_error", "loc": ["body"],
                         "msg": "Value error, When using `tool_choice`, `tools` must be set."}]},
            status_code=400,
        )
    _mt = b.get("max_tokens") or b.get("max_completion_tokens")
    if isinstance(_mt, int) and _mt <= 0:
        # 模拟严格上游:max_tokens 非法值(客户端按上下文窗口算出超大负数)直接拒绝
        return JSONResponse(
            {"error": {"message": "max_tokens must be at least 1, got %s. (parameter=max_tokens)" % _mt}},
            status_code=400,
        )
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
    if m == "nodone":
        # 正常下发内容后直接结束连接（不带 [DONE]）——转换流必须补发终端事件,
        # 否则 Anthropic/Responses 客户端会一直等 message_stop/response.completed 而挂起
        async def g8():
            yield "data: " + json.dumps({"model":m,"choices":[{"delta":{"content":"nodone"}}]}) + "\\n\\n"
            await asyncio.sleep(0.1)
        return StreamingResponse(g8(), media_type="text/event-stream")
    if m == "thinkjunk":
        # 思考退化场景:先输出合法思考,再退化成成片感叹号,另有分隔线(不该被误伤)
        if st:
            async def g10():
                yield "data: " + json.dumps({"model": m, "choices": [{"delta": {"reasoning_content": "让我思考一下。"}}]}, ensure_ascii=False) + "\\n\\n"
                yield "data: " + json.dumps({"model": m, "choices": [{"delta": {"reasoning_content": "----------\\n"}}]}, ensure_ascii=False) + "\\n\\n"
                yield "data: " + json.dumps({"model": m, "choices": [{"delta": {"reasoning_content": "!!!!!!!!!!!!!!!!!!!!!!!!"}}]}, ensure_ascii=False) + "\\n\\n"
                yield "data: " + json.dumps({"model": m, "choices": [{"delta": {"content": "答案"}}]}, ensure_ascii=False) + "\\n\\n"
                yield "data: [DONE]\\n\\n"
            return StreamingResponse(g10(), media_type="text/event-stream")
        return {"id": "c1", "object": "chat.completion", "model": m,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "答案",
                             "reasoning_content": "!!!!!!!!!!!!!!!!!!!!!!!!!!"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}
    if m == "dupfield":
        _hist_rc = any(isinstance(mm, dict) and ("reasoning_content" in mm or "reasoning" in mm)
                       for mm in b.get("messages", []))
        if _hist_rc:
            msg = "Failed to deserialize the JSON body into the target type: duplicate field `reasoning_content` at line 1 column 143731"
            if st:
                async def g11():
                    yield "data: " + json.dumps({"error": {"message": msg}}) + "\\n\\n"
                return StreamingResponse(g11(), media_type="text/event-stream")
            return JSONResponse({"error": {"message": msg}}, status_code=400)
        return {"id": "c1", "object": "chat.completion", "model": m,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}}
    if m == "enablethink" and "enable_thinking" in b:
        msg = "Validation: Unsupported parameter(s): `enable_thinking`"
        if st:
            async def g12():
                yield "data: " + json.dumps({"error": {"message": msg}}) + "\\n\\n"
            return StreamingResponse(g12(), media_type="text/event-stream")
        return JSONResponse({"error": {"message": msg}}, status_code=400)
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
    # reasoning 被客户端简写成字符串（"high"）时不能 500：它按 Responses API 是对象，
    # 但简写很常见；以前 (req.get("reasoning") or {}).get("effort") 会因 str 没有 .get
    # 抛 AttributeError 变成 500。
    r = c.post("/v1/responses", json={"model": "mock-model", "input": "hi", "reasoning": "high"})
    add("reasoning 传字符串不 500", r.status_code == 200, "st=%s" % r.status_code)
    r = c.post("/v1/responses", json={"model": "mock-model", "input": "hi", "reasoning": {"effort": "high"}})
    add("reasoning 传对象正常", r.status_code == 200, "st=%s" % r.status_code)
    # 渠道「思考强度默认值」的取值集合必须覆盖上游实际支持的值（Kimi 文档里就有 max），
    # 否则配了也会被静默丢弃、退化成「删掉思考参数」。
    sys.path.insert(0, ROOT)
    from core.convert import parse_thinking_defaults as _ptd

    got_d = _ptd("\n".join(["kimi=max", "qwen3=low", "glm=nonsense"]))
    add("思考强度配置解析", got_d == {"kimi": "max", "qwen3": "low"}, "%s（非法值应被忽略）" % got_d)

    # 参数覆写：值里带逗号的数组不能被切断（以前按逗号盲拆，stop=["a","b"] 只会剩 ["a"），
    # 且数组/对象要解析成 JSON 原生类型 —— 否则上游按字符串收到数组会直接拒绝。
    from core.upstreams import _parse_param_pairs as _ppp

    pp = _ppp('stop=["a","b"]')
    pp2 = _ppp("top_p=0.9,seed=42")
    add(
        "参数覆写解析",
        pp == {"stop": ["a", "b"]} and pp2 == {"top_p": 0.9, "seed": 42},
        "数组=%s 多组=%s" % (pp, pp2),
    )
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
    # 断连释放必须恰好一次:等响应头期间断连的路径以前 release 后没置空 hold,
    # finally 兜底会再释放一次 → 一条真 499 + 一条假 500「兜底释放」。
    # 「账号可复用」测不出这个(提前释放也"可复用"),必须查日志。
    time.sleep(2)  # 等日志落盘
    _rows = a.get("/api/logs?n=50").json().get("rows") or []
    _since = [x for x in _rows if isinstance(x, list) and len(x) > 6 and "客户端已断开" in str(x[6])]
    _dup = [x for x in _since if "兜底释放" in str(x[6])]
    add(
        "断连释放恰好一次",
        bool(_since) and not _dup,
        "499 断连日志 %d 条,兜底释放 %d 条" % (len(_since), len(_dup)),
    )
    # 恢复
    a.post("/api/settings", json={"config": {"acct_concurrency": 0}})
    for kid in ids[1:]:
        a.post("/api/keys/op", json={"op": "enable", "id": kid})

    # 账户锁定兜底：httpx.InvalidURL 不是 httpx.HTTPError 的子类，非法 base URL
    # （例如 http://[bad，仍能通过 http(s):// 前缀校验）会穿透原有捕获，把账号永久
    # 锁在该请求上。现在 _proxy/_convert 的整个重试循环外层有 try/finally 兜底释放。
    a.post("/api/settings", json={"config": {"acct_concurrency": 1}})
    # 用会真正抛 httpx.InvalidURL 的地址（IPv6 端口写错）：InvalidURL 不是 HTTPError
    # 的子类，会穿透原有 except，只有兜底 finally 能救回账号。
    a.post("/api/upstreams", json={"id": uid, "name": "T", "base": "http://[::1:99999]/v1", "enabled": True})
    r = c.post(
        "/v1/chat/completions", json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]}
    )
    bad_st = r.status_code
    # 兜底日志必须带上真实异常，否则只有一句固定文案、无从定位
    errs = [x[6] for x in a.get("/api/logs").json()["rows"] if x[2] == "mock-model"]
    has_reason = any("InvalidURL" in (e or "") for e in errs)
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
        ok and bad_st >= 400 and has_reason,
        "非法 base 时 st=%s；兜底日志含异常=%s；恢复后 %.1fs 内账号可用"
        % (bad_st, has_reason, time.time() - t0),
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
    # 连接池耗尽必须单列(全局容量问题,不惩罚账号),且不能被 "timeout" 字样误判
    if _pool._classify(0, 0, "PoolTimeout: 连接池耗尽") != "pool_exhausted":
        wrong.append("pooltimeout->%s(期望pool_exhausted)" % _pool._classify(0, 0, "PoolTimeout: 连接池耗尽"))
    if _pool._classify(0, 0, "httpx.PoolTimeout occurred") != "pool_exhausted":
        wrong.append("pooltimeout类名->%s(期望pool_exhausted)" % _pool._classify(0, 0, "httpx.PoolTimeout occurred"))
    # 连接池配置可保存/读取
    a.post("/api/settings", json={"config": {"pool_max_connections": 400}})
    if int(a.get("/api/settings").json().get("pool_max_connections") or 0) != 400:
        wrong.append("pool_max_connections 设置不生效")
    add("错误分级契约", not wrong, "；".join(wrong) if wrong else "402/401/403/429/5xx/conn/pool_exhausted 归类正确")

    # 批量解封:401 触发硬封禁 → 批量 enable 解封 → 账号必须立即可调度
    a.post("/api/settings", json={"config": {"hard_fail_ban_seconds": 600, "hard_fail_disable_count": 99}})
    r = c.post(
        "/v1/chat/completions",
        json={"model": "authfail", "messages": [{"role": "user", "content": "hi"}]},
    )
    now_i = int(time.time())
    rows_all = a.get("/api/keys").json()["rows"]
    banned_ids = [k["id"] for k in rows_all if (k.get("banned_until") or 0) > now_i]
    # 复现断点一:401 是否触发了封禁
    add("401 触发账号封禁", r.status_code == 401 and bool(banned_ids),
        "HTTP %s,封禁账号 %d 个" % (r.status_code, len(banned_ids)))
    if banned_ids:
        rb = a.post("/api/keys/batch", json={"op": "enable", "ids": banned_ids})
        rows2 = a.get("/api/keys").json()["rows"]
        still = [k["id"] for k in rows2 if (k.get("banned_until") or 0) > now_i]
        st_bad = [k["id"] for k in rows2 if k["id"] in banned_ids and k.get("status") != "active"]
        r_ok = c.post(
            "/v1/chat/completions",
            json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        add("批量解封生效(enable)", rb.status_code == 200 and not still and not st_bad and r_ok.status_code == 200,
            "batch=%s 仍封禁=%s status异常=%s 解封后请求=%s" % (rb.status_code, still, st_bad, r_ok.status_code))

    # ---- 训练资料收集 ----
    # 非流式 chat:成功请求必须收集「完整消息 + 回复」(测试先关质量过滤)
    a.post("/api/settings", json={"config": {"training_log_max": 500, "training_min_chars": 0}})
    a.post("/api/training/clear", json={})
    c.post(
        "/v1/chat/completions",
        json={
            "model": "mock-model",
            "messages": [
                {"role": "system", "content": "你是测试"},
                {"role": "user", "content": "你好训练"},
            ],
        },
    )
    tr = a.get("/api/training?n=10").json()
    tr_rows = tr.get("rows") or []
    ent = next((e for e in tr_rows if e.get("model") == "mock-model"), None)
    add(
        "训练资料:非流式 chat 收集",
        ent is not None
        and len(ent.get("messages") or []) == 2
        and ent["messages"][0]["role"] == "system"
        and ent["messages"][1]["content"] == "你好训练"
        and ent.get("response") == "ok",
        "entry=%s" % ("有" if ent else "无"),
    )

    # 流式 chat:SSE 全文提取("Hello world")
    c.post(
        "/v1/chat/completions",
        json={"model": "mock-model", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    tr2 = a.get("/api/training?n=10").json()
    ent2 = next((e for e in (tr2.get("rows") or []) if e.get("response") == "Hello world"), None)
    add("训练资料:流式全文提取", ent2 is not None, "response=%r" % (ent2 or {}).get("response"))

    # /v1/messages 流式(协议转换流):text_acc 全文
    c.post(
        "/v1/messages",
        json={"model": "mock-model", "max_tokens": 32, "stream": True,
              "messages": [{"role": "user", "content": "你好"}]},
    )
    tr3 = a.get("/api/training?n=10").json()
    ent3 = next((e for e in (tr3.get("rows") or []) if (e.get("ep") or "")[:3] == "msg"), None)
    add("训练资料:Messages 流式收集", ent3 is not None and ent3.get("response") == "Hello world",
        "ep=%s resp=%r" % ((ent3 or {}).get("ep"), (ent3 or {}).get("response")))

    # 关闭收集(training_log_max=0)
    a.post("/api/settings", json={"config": {"training_log_max": 0}})
    c.post(
        "/v1/chat/completions",
        json={"model": "mock-model", "messages": [{"role": "user", "content": "不应收集"}]},
    )
    time.sleep(0.6)
    tr4 = a.get("/api/training?n=50").json()
    off_ok = all((e.get("messages") or [{}])[-1].get("content") != "不应收集" for e in (tr4.get("rows") or []))
    add("训练资料:关闭后不收集", off_ok, "total=%s" % tr4.get("total"))
    a.post("/api/settings", json={"config": {"training_log_max": 500}})

    # 导出 JSONL:每行含 assistant 尾条
    ex = a.get("/api/training/export")
    ex_lines = [ln for ln in ex.text.split("\n") if ln.strip()]
    ex_ok = False
    if ex_lines:
        j0 = json.loads(ex_lines[0])
        ex_ok = isinstance(j0.get("messages"), list) and j0["messages"][-1].get("role") == "assistant"
    add("训练资料:JSONL 导出格式", ex_ok, "%d 行,首行尾角色=%s" % (len(ex_lines), json.loads(ex_lines[0])["messages"][-1]["role"] if ex_lines else "-"))

    # 清空
    a.post("/api/training/clear", json={})
    tr5 = a.get("/api/training?n=10").json()
    add("训练资料:清空", (tr5.get("rows") or []) == [] and tr5.get("total") == 0, "total=%s" % tr5.get("total"))

    # 质量过滤:短回复/超短输入的垃圾语料(冒烟测试、模型测试页)不收
    a.post("/api/settings", json={"config": {"training_min_chars": 20}})
    c.post(
        "/v1/chat/completions",
        json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
    )  # 回复 "ok"(2 字符) → 不收
    c.post(
        "/v1/chat/completions",
        json={"model": "nousage", "messages": [{"role": "user", "content": "???"}]},
    )  # 回复 "ok",用户输入 3 字符 → 不收
    time.sleep(0.5)
    trq = a.get("/api/training?n=20").json()
    junk = [e for e in (trq.get("rows") or []) if len(str(e.get("response") or "").strip()) < 20]
    add("训练资料:短/垃圾语料被过滤", (trq.get("total") or 0) == 0 and not junk, "total=%s 垃圾=%d" % (trq.get("total"), len(junk)))
    a.post("/api/settings", json={"config": {"training_min_chars": 0}})

    # 小时限语义:hourly 是「日 token 超标后的限速阀」,不是独立小时硬限 ——
    # 健康账号本小时请求数超 hourly 也必须可调度(被拆成独立闸门时,
    # 线上出现「小时限 137/共 201」大面积误伤)。用独立账号精确验证。
    kids_all = [k["id"] for k in a.get("/api/keys").json()["rows"]]
    a.post("/api/keys/import", json={
        "text": "fresh@t.com,p,nvapi-fresh12345678", "upstream_id": uid,
    })
    kids_fresh = [k["id"] for k in a.get("/api/keys").json()["rows"] if k["email"] == "fresh@t.com"]
    a.post("/api/keys/batch", json={"op": "disable", "ids": kids_all})
    a.post("/api/settings", json={"config": {
        "hourly_request_limit": 5, "daily_token_limit": 0, "daily_request_cap": 0,
        "rate_limit_per_minute": 100000, "acct_concurrency": 0, "warmup_seconds": 0,
    }})
    h_codes = []
    for _i in range(7):  # 7 次 > hourly=5,日 token 未超 → 必须全部放行
        h_codes.append(c.post(
            "/v1/chat/completions",
            json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
        ).status_code)
    add("小时限不独立拦截健康账号", h_codes == [200] * 7,
        "7 连发=%s(hourly=5 且已超,仍全部放行)" % (sorted(set(h_codes)),))
    # 日 token 超标 + 小时达限 → 才触发限速(daily_token_limit=1 使账号立即超标)
    a.post("/api/settings", json={"config": {"daily_token_limit": 1}})
    r_th = c.post(
        "/v1/chat/completions",
        json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    add("日token超标后按小时限速", r_th.status_code == 429, "st=%s(应为限速 429)" % r_th.status_code)
    # 恢复环境:删除 fresh 账号 + 启用全部 + 清累计统计(后续测试回到干净基线)
    a.post("/api/settings", json={"config": {
        "daily_token_limit": 0, "daily_request_cap": 100, "hourly_request_limit": 5,
    }})
    for kid in kids_fresh:
        a.post("/api/keys/op", json={"op": "delete", "id": kid})
    a.post("/api/keys/batch", json={"op": "enable", "ids": kids_all})
    a.post("/api/keys/batch", json={"op": "unban", "ids": kids_all})
    a.post("/api/keys/batch", json={"op": "reset", "ids": kids_all})

    # tool_choice 兼容:客户端/转换器带 tool_choice 而 tools 为空时,严格上游会 400
    # (线上实测:"When using `tool_choice`, `tools` must be set.")。网关须转发前清理。
    r_tc = c.post(
        "/v1/chat/completions",
        json={"model": "mock-model", "tool_choice": "auto",
              "messages": [{"role": "user", "content": "hi"}]},
    )
    add("tool_choice 无 tools 时被清理(chat)", r_tc.status_code == 200, "st=%s" % r_tc.status_code)
    r_tc2 = c.post(
        "/v1/messages",
        json={"model": "mock-model", "max_tokens": 32, "tool_choice": {"type": "auto"},
              "messages": [{"role": "user", "content": "hi"}]},
    )
    add("tool_choice 无 tools 时被清理(Anthropic)", r_tc2.status_code == 200, "st=%s" % r_tc2.status_code)
    r_tc3 = c.post(
        "/v1/responses",
        json={"model": "mock-model", "tool_choice": "auto", "input": "hi"},
    )
    add("tool_choice 无 tools 时被清理(Responses)", r_tc3.status_code == 200, "st=%s" % r_tc3.status_code)
    # 有 tools 时 tool_choice 必须保留(不能误删)
    from core import convert as _cv
    _keep = _cv.sanitize_request(
        {"tool_choice": "auto", "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}]}
    )
    _drop = _cv.sanitize_request({"tool_choice": "auto"})
    _drop2 = _cv.sanitize_request({"tool_choice": {"type": "function", "function": {"name": "x"}}, "tools": []})
    add(
        "tool_choice 清理不误伤",
        _keep.get("tool_choice") == "auto" and "tool_choice" not in _drop and "tool_choice" not in _drop2,
        "keep=%s drop=%s drop2=%s" % ("tool_choice" in _keep, "tool_choice" in _drop, "tool_choice" in _drop2),
    )

    # 访问令牌管理(CRUD):从设置迁移出的独立板块
    r_tok = a.post("/api/tokens", json={})
    new_tok = r_tok.json().get("token") or ""
    add("令牌:自动生成并添加", r_tok.status_code == 200 and new_tok.startswith("sk-gw-"), new_tok[:18])
    r_dup = a.post("/api/tokens", json={"t": new_tok})
    add("令牌:重复添加被拒", r_dup.status_code == 400, "st=%s" % r_dup.status_code)
    r_lim_full = c.get("/v1/models").json()
    full_ids = [x["id"] for x in r_lim_full.get("data") or []]
    add("令牌:清单可读", bool(full_ids), "当前 %d 个模型" % len(full_ids))
    target_model = full_ids[0] if full_ids else "mock-model"
    r_lim = a.post("/api/tokens", json={"t": "sk-gw-testlimit01", "m": target_model})
    with httpx.Client(base_url="http://127.0.0.1:18213", timeout=30,
                      headers={"Authorization": "Bearer sk-gw-testlimit01"}) as c_lim:
        r_models = c_lim.get("/v1/models").json()
    add("令牌:模型限制生效",
        r_lim.status_code == 200
        and [x["id"] for x in r_models.get("data") or []] == [target_model],
        "限制 %r 后可见=%s" % (target_model, [x["id"] for x in r_models.get("data") or []]))
    r_upd = a.post("/api/tokens/update", json={"t": "sk-gw-testlimit01", "m": ""})
    with httpx.Client(base_url="http://127.0.0.1:18213", timeout=30,
                      headers={"Authorization": "Bearer sk-gw-testlimit01"}) as c_lim2:
        r_models2 = c_lim2.get("/v1/models").json()
    add("令牌:更新为全部模型",
        r_upd.status_code == 200 and len(r_models2.get("data") or []) > 1,
        "清空限制后可见 %d 个" % len(r_models2.get("data") or []))
    a.post("/api/tokens/delete", json={"t": "sk-gw-testlimit01"})
    a.post("/api/tokens/delete", json={"t": new_tok})
    with httpx.Client(base_url="http://127.0.0.1:18213", timeout=30,
                      headers={"Authorization": "Bearer sk-gw-testlimit01"}) as c_lim3:
        r_gone = c_lim3.get("/v1/models")
    rows_t2 = a.get("/api/tokens").json().get("rows") or []
    add("令牌:删除后立即失效",
        r_gone.status_code == 401
        and all(x["t"] != new_tok for x in rows_t2)
        and all(x["t"] != "sk-gw-testlimit01" for x in rows_t2),
        "删除后请求 st=%s" % r_gone.status_code)

    # 非法 max_tokens(客户端算出超大负数):网关转发前清理,不再被严格上游 400
    r_neg = c.post(
        "/v1/chat/completions",
        json={"model": "mock-model", "max_tokens": -134237,
              "messages": [{"role": "user", "content": "hi"}]},
    )
    add("非法 max_tokens 被清理", r_neg.status_code == 200, "st=%s" % r_neg.status_code)

    # 安全守护:管理端点认证全覆盖(AST 静态检查,Python 3.8 兼容)
    import ast as _ast

    def _has_require(fn):
        for stmt in fn.body[:3]:
            for n in _ast.walk(stmt):
                if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name) and n.func.id == "_require":
                    return True
        return False

    _missing = []
    _tree = _ast.parse(open(os.path.join(ROOT, "admin_api.py"), encoding="utf-8").read())
    for _n in _tree.body:
        if isinstance(_n, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and _n.name not in ("login", "logout", "remote_update"):
            _decs = [d.func.attr for d in _n.decorator_list
                     if isinstance(d, _ast.Call) and isinstance(d.func, _ast.Attribute)]
            if any(x in ("get", "post") for x in _decs) and not _has_require(_n):
                _missing.append(_n.name)
    add("安全:管理端点认证全覆盖", not _missing, "缺失: %s" % (_missing or "无"))
    # remote_update 单独验证:必须含 Bearer token 鉴权或 _require 双路径
    _rtree = _ast.parse(open(os.path.join(ROOT, "admin_api.py"), encoding="utf-8").read())
    _rt_ok = False
    for _n in _rtree.body:
        if isinstance(_n, _ast.AsyncFunctionDef) and _n.name == "remote_update":
            _src = _ast.unparse(_n)
            _rt_ok = "compare_digest" in _src and "_require" in _src
    add("安全:remote_update 双路径鉴权", _rt_ok,
        "Bearer compare_digest + _require 都在=%s" % _rt_ok)

    # 测试页跳过标记:带 X-NGW-Skip-Training 的请求(后台模型测试页)不进训练集
    a.post("/api/settings", json={"config": {"training_min_chars": 0}})
    a.post("/api/training/clear", json={})
    c.headers["X-NGW-Skip-Training"] = "1"
    c.post(
        "/v1/chat/completions",
        json={"model": "mock-model", "messages": [{"role": "user", "content": "这是模型测试页的对话不应收集"}]},
    )
    del c.headers["X-NGW-Skip-Training"]
    time.sleep(0.5)
    tr_skip = a.get("/api/training?n=20").json()
    add("训练资料:测试页标记跳过收集", (tr_skip.get("total") or 0) == 0, "total=%s" % tr_skip.get("total"))
    a.post("/api/settings", json={"config": {"training_min_chars": 20}})

    # 管理端点健壮性:非 dict JSON body 必须 400 而非 500
    r_bad1 = a.post("/api/tokens", content=b"[1,2]", headers={"content-type": "application/json", "x-csrf": a.headers["X-CSRF"]})
    r_bad2 = a.post("/api/keys/op", content=b'"str"', headers={"content-type": "application/json", "x-csrf": a.headers["X-CSRF"]})
    r_bad3 = a.post("/api/keys/batch", content=b"notjson", headers={"content-type": "application/json", "x-csrf": a.headers["X-CSRF"]})
    add("管理端点:非法请求体返回 400",
        r_bad1.status_code == 400 and r_bad2.status_code == 400 and r_bad3.status_code == 400,
        "tokens=%s keysop=%s batch=%s" % (r_bad1.status_code, r_bad2.status_code, r_bad3.status_code))

    # 退化思考清理:推理栈故障时思考退化成成片 '!'(线上实测)。
    # 三条路径都必须清理;合法分隔线(----------)不能被误伤。
    r_ns = c.post(
        "/v1/chat/completions",
        json={"model": "thinkjunk", "messages": [{"role": "user", "content": "hi"}]},
    )
    j_ns = r_ns.json()
    _rs_ns = ((j_ns.get("choices") or [{}])[0].get("message") or {}).get("reasoning_content")
    add("退化思考:非流式清理", r_ns.status_code == 200 and _rs_ns == "" and
        ((j_ns.get("choices") or [{}])[0].get("message") or {}).get("content") == "答案",
        "reasoning=%r content=%r" % (_rs_ns, ((j_ns.get("choices") or [{}])[0].get("message") or {}).get("content")))

    raw_st = []
    with c.stream(
        "POST", "/v1/chat/completions",
        json={"model": "thinkjunk", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    ) as r_st:
        st_st = r_st.status_code
        raw_st = r_st.read().decode("utf-8", "replace")
    add("退化思考:流式清理",
        st_st == 200 and "!!!!!!!!!!!!!!!!" not in raw_st and "让我思考一下" in raw_st
        and "----------" in raw_st and "答案" in raw_st,
        "16连感叹号已清=%s 合法思考保留=%s 分隔线保留=%s" % (
            "!!!!!!!!!!!!!!!!" not in raw_st, "让我思考一下" in raw_st, "----------" in raw_st))

    raw_ms = []
    with c.stream(
        "POST", "/v1/messages",
        json={"model": "thinkjunk", "max_tokens": 64, "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    ) as r_ms:
        st_ms = r_ms.status_code
        raw_ms = r_ms.read().decode("utf-8", "replace")
    add("退化思考:Messages 流式清理",
        st_ms == 200 and "!!!!!!!!" not in raw_ms and "让我思考一下" in raw_ms,
        "思考退化已清=%s 合法思考保留=%s" % ("!!!!!!!!" not in raw_ms, "让我思考一下" in raw_ms))

    # 令牌追踪:日志记录调用令牌(遮罩)、令牌页显示最后调用 IP/时间、公开队列不泄漏
    r_trk = c.post(
        "/v1/chat/completions",
        json={"model": "mock-model", "messages": [{"role": "user", "content": "追踪我"}]},
    )
    time.sleep(0.8)  # 等异步 release 落库
    trk_rows = a.get("/api/logs?n=10").json().get("rows") or []
    trk_row = next((x for x in trk_rows if x[2] == "mock-model"), None)
    _tok = toks[0]["t"]
    _tok_mask = _tok[:10] + "…" + _tok[-4:] if len(_tok) > 14 else _tok
    add("令牌:日志记录调用令牌",
        trk_row is not None and len(trk_row) > 14 and trk_row[14] == _tok_mask,
        "row[14]=%r 期望=%r" % ((trk_row or [None] * 15)[14], _tok_mask))
    tok_rows = a.get("/api/tokens").json().get("rows") or []
    tok_ent = next((x for x in tok_rows if x["t"] == _tok), None)
    add("令牌:最后调用 IP/时间被追踪",
        tok_ent is not None and tok_ent.get("last_at", 0) > 0 and tok_ent.get("last_ip"),
        "last_at=%s last_ip=%r" % ((tok_ent or {}).get("last_at"), (tok_ent or {}).get("last_ip")))
    pub = httpx.get("http://127.0.0.1:18213/api/queue/public", timeout=10).json()
    add("令牌:公开队列不泄漏",
        all("tok" not in row for row in (pub.get("rows") or [])),
        "public rows=%d,含 tok 字段的=%d" % (len(pub.get("rows") or []), sum(1 for x in pub.get("rows") or [] if "tok" in x)))

    # 排队条目携带令牌:hold4 占住并发,第二个请求在排队等待期间管理端可见其令牌
    a.post("/api/settings", json={"config": {"acct_concurrency": 1}})
    ids4 = [k["id"] for k in a.get("/api/keys").json()["rows"]]
    a.post("/api/keys/batch", json={"op": "disable", "ids": ids4[1:]})
    th = threading.Thread(target=lambda: c.post(
        "/v1/chat/completions", json={"model": "hold4", "messages": [{"role": "user", "content": "h"}]}
    ), daemon=True)
    th.start()
    time.sleep(0.8)  # 第一个请求已持号(hold4 占 4s)
    q_status = {}

    def _q_req():
        try:
            q_status["code"] = c.post(
                "/v1/chat/completions", json={"model": "mock-model", "messages": [{"role": "user", "content": "q"}]}
            ).status_code
        except Exception:
            q_status["code"] = 0

    th2 = threading.Thread(target=_q_req, daemon=True)
    th2.start()
    time.sleep(1.2)  # 第二个请求此刻应在队列中
    q_rows = a.get("/api/queue").json().get("rows") or []
    q_tok_ok = any((x.get("tok") or "") == _tok_mask for x in q_rows)
    th2.join(20)
    add("令牌:排队条目携带令牌", q_tok_ok,
        "排队可见令牌=%s(排队中 %d 条)" % (q_tok_ok, len(q_rows)))
    a.post("/api/settings", json={"config": {"acct_concurrency": 0}})
    a.post("/api/keys/batch", json={"op": "enable", "ids": ids4[1:]})

    # in-flight 泄漏守护:重试 continue 路径曾泄漏账号并发计数(线上号池
    # "账户并发 201/共 202"全满、排队 300s 超时的根因)。
    # 检测器:并发 1 + 两个专用账号 —— 泄漏存在时同一账号的 in-flight 永不归零,
    # 后续请求必然排队超时
    a.post("/api/settings", json={"config": {
        "acct_concurrency": 1, "queue_max_wait": 3, "queue_poll_ms": 200,
        "cool_429_seconds": 1, "retry_backoff_base_ms": 100, "retry_backoff_max_ms": 200,
        "max_retries": 5, "warmup_seconds": 0,
        "daily_request_cap": 0, "daily_token_limit": 0, "rate_limit_per_minute": 100000,
    }})
    kids5 = [k["id"] for k in a.get("/api/keys").json()["rows"]]
    a.post("/api/keys/batch", json={"op": "unban", "ids": kids5})  # 清前面测试累计的封禁
    a.post("/api/keys/import", json={
        "text": "leak1@t.com,p,nvapi-leak00000001\nleak2@t.com,p,nvapi-leak00000002",
        "upstream_id": uid,
    })
    a.post("/api/keys/batch", json={"op": "disable", "ids": kids5})  # 只留两个泄漏专用账号
    # 场景1:429 吸收换号重试(mock rl 前两次 429,修复后应换号+真实冷却后重试成功)
    r51 = c.post(
        "/v1/chat/completions",
        json={"model": "rl", "messages": [{"role": "user", "content": "hi"}]},
        timeout=60,
    )
    # 泄漏检测:吸收路径若泄漏 in-flight,两个专用账号都会被幽灵占满 → 必然排队超时
    r52 = c.post(
        "/v1/chat/completions",
        json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
        timeout=60,
    )
    add("in-flight 泄漏:429 吸收路径", r51.status_code == 200 and r52.status_code == 200,
        "429吸收重试=%s 泄漏检测第二请求=%s" % (r51.status_code, r52.status_code))
    # 场景2:思考降级同号重试(kimi + effort=medium 触发上游 400 → reuse 同号降级重试成功)
    r53 = c.post(
        "/v1/chat/completions",
        json={"model": "kimi", "thinking_effort": "medium",
              "messages": [{"role": "user", "content": "hi"}]},
        timeout=60,
    )
    # 泄漏检测:降级路径若泄漏(修复前 continue 重新取号,旧号被 hold 覆盖),此处必然排队超时
    r54 = c.post(
        "/v1/chat/completions",
        json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
        timeout=60,
    )
    add("in-flight 泄漏:思考降级路径", r53.status_code == 200 and r54.status_code == 200,
        "降级重试=%s 泄漏检测第二请求=%s" % (r53.status_code, r54.status_code))
    # 清理专用账号
    leak_ids = [k["id"] for k in a.get("/api/keys").json()["rows"]
                if (k.get("email") or "").startswith("leak")]
    for kid in leak_ids:
        a.post("/api/keys/op", json={"op": "delete", "id": kid})
    # 恢复
    a.post("/api/settings", json={"config": {
        "acct_concurrency": 0, "queue_max_wait": 8, "queue_poll_ms": 150,
        "max_retries": 2, "retry_backoff_base_ms": 10, "retry_backoff_max_ms": 20,
        "cool_429_seconds": 1, "daily_request_cap": 100, "daily_token_limit": 0,
    }})
    a.post("/api/keys/batch", json={"op": "enable", "ids": kids5})
    a.post("/api/keys/batch", json={"op": "unban", "ids": kids5})

    # 漏 await 守护:_stream_convert 是 async 函数,两处调用曾漏 await(线上 500:
    # 'coroutine' object has no attribute 'body_iterator')。触发条件:协议转换端点
    # + 流式 + 慢头/慢首字节,测试 mock 上游此前从不覆盖这两条路径。
    # 路径1:响应头超 12s → _slow_convert_response → 交接 _stream_convert(线上报错点)
    t_slow = time.time()
    ev_sc = []
    with c.stream("POST", "/v1/messages",
                  json={"model": "slowhdr", "max_tokens": 64, "stream": True,
                        "messages": [{"role": "user", "content": "hi"}]}) as r_sc:
        st_sc = r_sc.status_code
        raw_sc = r_sc.read().decode("utf-8", "replace")
    for line in raw_sc.split("\n"):
        if line.startswith("event: "):
            ev_sc.append(line[7:].strip())
    add("转换流:慢头兜底路径不 500",
        st_sc == 200 and "message_start" in ev_sc and "message_stop" in ev_sc,
        "st=%s 事件=%s 耗时=%.0fs(修复前 coroutine 500)" % (st_sc, sorted(set(ev_sc))[:4], time.time() - t_slow))
    # 路径2:首字节超 8s → 心跳透传分支(return await _stream_convert)
    t_sf = time.time()
    ev_sf = []
    with c.stream("POST", "/v1/messages",
                  json={"model": "slowfirst", "max_tokens": 64, "stream": True,
                        "messages": [{"role": "user", "content": "hi"}]}) as r_sf:
        st_sf = r_sf.status_code
        raw_sf = r_sf.read().decode("utf-8", "replace")
    for line in raw_sf.split("\n"):
        if line.startswith("event: "):
            ev_sf.append(line[7:].strip())
    add("转换流:慢首字节心跳路径不 500",
        st_sf == 200 and "message_start" in ev_sf and "message_stop" in ev_sf,
        "st=%s 事件=%s 耗时=%.0fs" % (st_sf, sorted(set(ev_sf))[:4], time.time() - t_sf))

    # 号池热力图端点:分组/统计/状态判定
    pm = a.get("/api/poolmap").json()
    pm_groups = pm.get("groups") or []
    pm_total = sum(g["total"] for g in pm_groups)
    kids_pm = a.get("/api/keys?status=all&page=1").json()
    pm_keycount = kids_pm.get("total") or 0
    pm_consistent = all(g["total"] == g["ok"] + g["busy"] + g["bad"] for g in pm_groups)
    add("热力图:结构一致", bool(pm_groups) and pm_total == pm_keycount and pm_consistent,
        "组=%d 总数=%d/%d 汇总一致=%s" % (len(pm_groups), pm_total, pm_keycount, pm_consistent))
    # 停用一个账号 → 热力图应显示红色(s=2)
    kid_off = kids_pm["rows"][0]["id"]
    a.post("/api/keys/op", json={"op": "disable", "id": kid_off})
    pm2 = a.get("/api/poolmap").json()
    cell_off = None
    for g in pm2.get("groups") or []:
        cell_off = next((x for x in g["cells"] if x["id"] == kid_off), cell_off)
    a.post("/api/keys/op", json={"op": "enable", "id": kid_off})
    add("热力图:停用账号标红", cell_off is not None and cell_off["s"] == 2,
        "s=%s 原因=%r" % ((cell_off or {}).get("s"), (cell_off or {}).get("w")))

    # duplicate field:多轮历史带思考字段被中转二次加工 → 严格上游 400。
    # 网关应清洗历史消息中的 reasoning_content 后同号重试成功
    _hist = [
        {"role": "user", "content": "第一轮"},
        {"role": "assistant", "content": "第一轮回答", "reasoning_content": "第一轮思考"},
        {"role": "user", "content": "第二轮"},
    ]
    r_dup = c.post(
        "/v1/chat/completions",
        json={"model": "dupfield", "messages": _hist},
        timeout=30,
    )
    add("duplicate field:历史思考清洗后重试", r_dup.status_code == 200,
        "st=%s(修复前 400 duplicate)" % r_dup.status_code)
    with c.stream(
        "POST", "/v1/chat/completions",
        json={"model": "dupfield", "stream": True, "messages": _hist},
    ) as r_dups:
        st_dups = r_dups.status_code
        raw_dups = r_dups.read().decode("utf-8", "replace")
    add("duplicate field:流式历史思考清洗",
        st_dups == 200 and "duplicate" not in raw_dups and "ok" in raw_dups,
        "st=%s 含错误=%s" % (st_dups, "duplicate" in raw_dups))

    # enable_thinking(顶层思考开关):严格上游拒绝 → 降级清单已含,清洗后重试成功
    r_et = c.post(
        "/v1/chat/completions",
        json={"model": "enablethink", "enable_thinking": True,
              "messages": [{"role": "user", "content": "hi"}]},
        timeout=30,
    )
    add("enable_thinking:顶层降级(非流式)", r_et.status_code == 200,
        "st=%s(修复前 400 Unsupported)" % r_et.status_code)
    with c.stream(
        "POST", "/v1/chat/completions",
        json={"model": "enablethink", "enable_thinking": True, "stream": True,
              "messages": [{"role": "user", "content": "hi"}]},
    ) as r_ets:
        st_ets = r_ets.status_code
        raw_ets = r_ets.read().decode("utf-8", "replace")
    add("enable_thinking:顶层降级(流式)",
        st_ets == 200 and "Unsupported" not in raw_ets,
        "st=%s 含错误=%s" % (st_ets, "Unsupported" in raw_ets))

    # 僵尸队列条目清理:进程重启时死掉的等待请求无人出队,条目永久留在 db,
    # 仪表盘虚报「排队中 N」而队列面板为空(线上实测 36 条僵尸)
    import server as _srv
    # 远程更新端点:开关关闭时拒绝;Bearer 令牌鉴权路径验证
    a.post("/api/settings", json={"config": {"update_enabled": False, "update_token": "upd-test1234567890abcdef"}})
    r_up0 = a.post("/api/update", json={})  # admin 会话 + 开关关 → 400
    a.post("/api/settings", json={"config": {"update_enabled": True}})
    # Bearer 令牌路径(无需 admin cookie)
    r_tok1 = httpx.post("http://127.0.0.1:18213/api/update",
                        headers={"Authorization": "Bearer upd-test1234567890abcdef"}, timeout=30)
    r_tok2 = httpx.post("http://127.0.0.1:18213/api/update",
                        headers={"Authorization": "Bearer upd-wrong-token-xxxxxxxx"}, timeout=30)
    a.post("/api/settings", json={"config": {"update_enabled": False, "update_token": ""}})
    add("远程更新:开关与令牌鉴权",
        r_up0.status_code == 400
        and (r_tok1.status_code in (200, 500))
        and r_tok2.status_code == 401,
        "开关关=%s 令牌对=%s 令牌错=%s" % (r_up0.status_code, r_tok1.status_code, r_tok2.status_code))
    _qdb = {
        "config": {"queue_max_wait": 300},
        "queue": [
            {"id": "q1", "t": time.time() - 10, "ip": "-", "ep": "chat", "model": "m"},     # 新鲜
            {"id": "q2", "t": time.time() - 30, "ip": "-", "ep": "chat", "model": "m"},     # 新鲜
            {"id": "q3", "t": time.time() - 3000, "ip": "-", "ep": "chat", "model": "m"},   # 僵尸(>600s)
            {"id": "q4", "t": time.time() - 9000, "ip": "-", "ep": "chat", "model": "m"},   # 僵尸
        ],
    }
    _srv._prune_queue(_qdb, time.time())
    add("僵尸队列条目被清理", len(_qdb["queue"]) == 2, "剩余 %d 条(应为 2)" % len(_qdb["queue"]))

    # 看门狗雪崩判定(纯函数直测)
    _db = {"keys": [
        {"id": "k1", "enabled": True, "banned_until": now_i + 600},
        {"id": "k2", "enabled": True, "cooldown_until": now_i + 600},
        {"id": "k3", "enabled": True},
    ], "queue": [{"id": "q", "t": time.time(), "ip": "-", "ep": "chat", "model": "m"}]}
    d1, i1 = _srv.watchdog_dead(_db, {}, now_i, {"acct_concurrency": 2})
    d2, _ = _srv.watchdog_dead(_db, {"k3": 2}, now_i, {"acct_concurrency": 2})  # k3 并发满 → 全灭
    _db["queue"] = []
    d3, _ = _srv.watchdog_dead(_db, {"k3": 2}, now_i, {"acct_concurrency": 2})  # 无等待者
    _db["queue"] = [{"id": "q", "t": time.time(), "ip": "-", "ep": "chat", "model": "m"}]
    d4, _ = _srv.watchdog_dead(_db, {"k3": 1}, now_i, {"acct_concurrency": 2})  # k3 仍可用
    add("看门狗雪崩判定", (not d1) and d2 and (not d3) and (not d4),
        "部分可用=%s 全灭=%s 无等待=%s 有可用=%s" % (d1, d2, d3, d4))
    _cfgw = a.get("/api/settings").json()
    add("看门狗配置项在位", bool(_cfgw.get("watchdog_enabled")) and int(_cfgw.get("watchdog_minutes") or 0) >= 1,
        "enabled=%s minutes=%s" % (_cfgw.get("watchdog_enabled"), _cfgw.get("watchdog_minutes")))

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

    # ---- 2026-09-19 兼容性与稳定性修复批次 ----

    # 1) 流式成功请求不得触发「兜底释放」：return StreamingResponse 会走 _proxy 的
    #    finally，此前 hold 未置空导致每个流式请求都被双重释放 + 记一条假 500 日志，
    #    账号在流式传输期间就被提前放回号池（配合并发限制会触发上游 429）。
    a.post("/api/logs/clear", json={})
    c.post("/v1/chat/completions", json={"model": "mock-model", "stream": True,
                                          "messages": [{"role": "user", "content": "hi"}]})
    logs_after = a.get("/api/logs?n=100").json()
    rows_l = logs_after.get("logs") if isinstance(logs_after, dict) else logs_after
    double_rel = [row for row in (rows_l or []) if isinstance(row, list) and len(row) > 6
                  and "兜底释放" in str(row[6])]
    add("流式成功不双重释放账号", not double_rel,
        ("发现 %d 条假「兜底释放」日志" % len(double_rel)) if double_rel else "无兜底释放日志")

    # 2) CORS 头必须覆盖所有 /v1 响应(含 401 错误响应):浏览器直连客户端读不到
    #    无 CORS 头的响应,连报错都只会显示成 CORS 错误。
    r401 = httpx.get("http://127.0.0.1:18213/v1/models")
    add("错误响应也带 CORS 头", r401.headers.get("access-control-allow-origin") == "*",
        "401 响应 ACAO=%s" % r401.headers.get("access-control-allow-origin"))

    # 3) 预检回显客户端申请的头(浏览器端 OpenAI/Anthropic SDK 带 x-stainless-*/anthropic-beta)
    ro = httpx.options(
        "http://127.0.0.1:18213/v1/chat/completions",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type,anthropic-beta,x-stainless-lang",
        },
    )
    ah = (ro.headers.get("access-control-allow-headers") or "").lower()
    add("预检回显自定义头", "anthropic-beta" in ah and "x-stainless-lang" in ah,
        "allow-headers=%s" % ah[:80])

    # 4) BOM 容忍:部分 Windows 客户端发的 JSON 带 UTF-8 BOM,json.loads 会直接失败
    rb = c.post(
        "/v1/chat/completions",
        content=b'\xef\xbb\xbf{"model":"mock-model","messages":[{"role":"user","content":"hi"}]}',
        headers={"content-type": "application/json"},
    )
    add("BOM JSON 可解析", rb.status_code == 200, "st=%s" % rb.status_code)

    # 5) 尾斜杠兼容:有些客户端拼出 /v1/chat/completions/,网关必须直接处理
    rs = c.post(
        "/v1/chat/completions/",
        json={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    add("尾斜杠路由兼容", rs.status_code == 200, "st=%s" % rs.status_code)

    # 6) Anthropic 流事件的 data 必须带 type 字段;上游流无 [DONE] 也要补终端事件
    ev_types = {}
    with c.stream("POST", "/v1/messages",
                  json={"model": "nodone", "max_tokens": 32, "stream": True,
                        "messages": [{"role": "user", "content": "hi"}]}) as rm:
        ok_st = rm.status_code == 200
        raw = rm.read().decode("utf-8", "replace")
    cur_ev, cur_data = "", ""
    for line in raw.split("\n"):
        if line.startswith("event: "):
            cur_ev = line[7:].strip()
        elif line.startswith("data: ") and cur_ev:
            try:
                j = json.loads(line[6:])
            except Exception:
                j = {}
            ev_types.setdefault(cur_ev, []).append(j)
            cur_ev = ""
    md = (ev_types.get("message_delta") or [{}])[0]
    ms = (ev_types.get("message_stop") or [{}])[0]
    has_types = "type" in md and "type" in ms
    # 全量校验:每个 Anthropic 事件 data 的 type 必须与事件名一致(SDK 按 data.type 分发)
    ant_bad = [ev for ev, lst in ev_types.items() for j in lst if not isinstance(j, dict) or j.get("type") != ev]
    add("Anthropic 流事件带 type 且无 [DONE] 也能收尾",
        ok_st and has_types and ev_types.get("content_block_delta") and not ant_bad,
        "st=%s message_delta.type=%s message_stop.type=%s 事件=%s type不符=%s"
        % (rm.status_code, md.get("type"), ms.get("type"), sorted(ev_types), ant_bad[:3]))

    # 7) Responses 流无 [DONE] 也必须有 response.completed 终端事件;
    #    且每个事件的 data 必须带 "type" 判别字段(OpenAI SDK 按 type 构造事件,
    #    只发 SSE event 行不够 —— 曾经全部事件都缺 type,SDK 直接构造失败)
    ev2 = []
    type_bad = []
    with c.stream("POST", "/v1/responses",
                  json={"model": "nodone", "stream": True,
                        "input": "hi"}) as rr2:
        ok_st2 = rr2.status_code == 200
        raw2 = rr2.read().decode("utf-8", "replace")
    cur_ev, cur_data = "", ""
    for line in raw2.split("\n"):
        if line.startswith("event: "):
            cur_ev = line[7:].strip()
        elif line.startswith("data: ") and cur_ev:
            cur_data = line[6:].strip()
            ev2.append(cur_ev)
            try:
                j = json.loads(cur_data)
            except Exception:
                j = None
            if not isinstance(j, dict) or j.get("type") != cur_ev:
                type_bad.append("%s->%r" % (cur_ev, j if isinstance(j, dict) else j))
            cur_ev = ""
    add("Responses 流无 [DONE] 也收尾", ok_st2 and "response.completed" in ev2,
        "st=%s 事件=%s" % (rr2.status_code, sorted(set(ev2))[:6]))
    add("Responses 事件 data 带 type 判别字段", ok_st2 and not type_bad,
        "缺失/不符 %d 条%s" % (len(type_bad), (" 如 " + ";".join(type_bad[:3])) if type_bad else ""))

    # 8) 概览 RPM 统计不为 0(buckets 是时间戳列表,以前按 dict+小时键统计恒为 0)
    ov = a.get("/api/overview").json()
    add("概览 RPM 统计正常", ov.get("rpm", -1) >= 0, "rpm=%s" % ov.get("rpm"))

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
