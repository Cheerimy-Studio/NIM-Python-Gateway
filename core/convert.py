"""协议转换：OpenAI Responses ↔ Chat Completions，Anthropic Messages ↔ Chat Completions。

流式转换器为同步状态机：feed(chunk) 喂入上游 SSE 文本，内部通过 emit(event, data)
回调产出下游事件；流以 [DONE] 或 finish_reason 结束。
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .util import rand_id


def _j(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def flatten_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for c in content or []:
        if isinstance(c, str):
            parts.append(c)
            continue
        if not isinstance(c, dict):
            continue
        t = str(c.get("type") or "")
        if t in ("input_text", "output_text", "text", "summary_text") and "text" in c:
            parts.append(str(c["text"]))
        elif t == "refusal" and "refusal" in c:
            parts.append(str(c["refusal"]))
    return "".join(parts)


def map_usage(u: dict | None) -> dict:
    tin = (u or {}).get("prompt_tokens")
    tout = (u or {}).get("completion_tokens")
    return {
        "input_tokens": tin,
        "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": tout,
        "output_tokens_details": {"reasoning_tokens": 0},
        "total_tokens": (u or {}).get("total_tokens") or (((tin or 0) + (tout or 0)) or None),
    }


# ============================================================ Responses API


def responses_to_chat(req: dict) -> dict:
    if req.get("previous_response_id"):
        raise ValueError("本网关为无状态网关，不支持 previous_response_id；请把完整历史放入 input。")
    messages: list[dict] = []
    instructions = req.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})

    inp = req.get("input") or ""
    if isinstance(inp, str):
        if inp:
            messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if not isinstance(item, dict):
                continue
            typ = str(item.get("type") or "message")
            if typ == "message":
                role = str(item.get("role") or "user")
                if role == "developer":
                    role = "system"
                messages.append({"role": role, "content": flatten_content(item.get("content"))})
            elif typ == "function_call":
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": str(item.get("call_id") or item.get("id") or rand_id("call_")),
                                "type": "function",
                                "function": {
                                    "name": str(item.get("name") or ""),
                                    "arguments": str(item.get("arguments") or "{}"),
                                },
                            }
                        ],
                    }
                )
            elif typ == "function_call_output":
                out = item.get("output")
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(item.get("call_id") or ""),
                        "content": out if isinstance(out, str) else flatten_content(out),
                    }
                )
    if not messages:
        raise ValueError("input 不能为空")

    chat: dict = {"model": str(req.get("model") or ""), "messages": messages}
    for k in ("temperature", "top_p", "stream", "parallel_tool_calls", "user", "seed"):
        if k in req:
            chat[k] = req[k]
    if req.get("max_output_tokens") is not None:
        chat["max_tokens"] = int(req["max_output_tokens"])
    elif req.get("max_completion_tokens") is not None:
        chat["max_tokens"] = int(req["max_completion_tokens"])
    if isinstance(req.get("reasoning"), dict) and req["reasoning"].get("effort"):
        chat["reasoning_effort"] = req["reasoning"]["effort"]

    tools = []
    for t in req.get("tools") or []:
        if isinstance(t, dict) and t.get("type") == "function":
            if "function" in t:
                tools.append(t)
            else:
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": str(t.get("name") or ""),
                            "description": str(t.get("description") or ""),
                            "parameters": t.get("parameters") or {"type": "object", "properties": []},
                        },
                    }
                )
    if tools:
        chat["tools"] = tools
    tc = req.get("tool_choice")
    if isinstance(tc, dict) and tc.get("type") == "function" and "name" in tc and "function" not in tc:
        chat["tool_choice"] = {"type": "function", "function": {"name": tc["name"]}}
    elif tc is not None:
        chat["tool_choice"] = tc
    return chat


def chat_to_responses(chat: dict, meta: dict | None = None) -> dict:
    meta = meta or {}
    choice = (chat.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    finish = str(choice.get("finish_reason") or "")

    output: list[dict] = []
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        output.append(
            {
                "id": rand_id("rs_"),
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": reasoning}],
            }
        )
    text = msg.get("content") if isinstance(msg.get("content"), str) else flatten_content(msg.get("content"))
    if text or not msg.get("tool_calls"):
        output.append(
            {
                "id": rand_id("msg_"),
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        output.append(
            {
                "id": rand_id("fc_"),
                "type": "function_call",
                "status": "completed",
                "call_id": str(tc.get("id") or rand_id("call_")),
                "name": str(fn.get("name") or ""),
                "arguments": str(fn.get("arguments") or "{}"),
            }
        )

    status, incomplete = "completed", None
    if finish == "length":
        status, incomplete = "incomplete", {"reason": "max_output_tokens"}
    elif finish == "content_filter":
        status, incomplete = "incomplete", {"reason": "content_filter"}

    usage = chat.get("usage") or {}
    return {
        "id": "resp_" + str(chat.get("id") or rand_id()),
        "object": "response",
        "created_at": int(chat.get("created") or time.time()),
        "status": status,
        "incomplete_details": incomplete,
        "error": None,
        "model": chat.get("model") or meta.get("model") or "",
        "output": output,
        "parallel_tool_calls": True,
        "previous_response_id": None,
        "reasoning": {"effort": meta.get("reasoning_effort"), "summary": None},
        "temperature": meta.get("temperature"),
        "top_p": meta.get("top_p"),
        "max_output_tokens": meta.get("max_output_tokens"),
        "tools": [],
        "tool_choice": "auto",
        "usage": map_usage(usage if isinstance(usage, dict) else None),
        "metadata": {},
    }


def anthropic_to_chat(req: dict) -> dict:
    messages: list[dict] = []
    system = req.get("system")
    if system:
        text = system if isinstance(system, str) else flatten_content(system)
        if text:
            messages.append({"role": "system", "content": text})

    has_messages = False
    for m in req.get("messages") or []:
        if not isinstance(m, dict):
            continue
        has_messages = True
        role = "assistant" if m.get("role") == "assistant" else "user"
        content = m.get("content") or ""
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        text_parts: list[str] = []
        tool_calls: list[dict] = []
        tool_results: list[dict] = []
        for b in content or []:
            if isinstance(b, str):
                text_parts.append(b)
                continue
            if not isinstance(b, dict):
                continue
            t = str(b.get("type") or "")
            if t == "text":
                text_parts.append(str(b.get("text") or ""))
            elif t == "tool_use":
                tool_calls.append(
                    {
                        "id": str(b.get("id") or rand_id("call_")),
                        "type": "function",
                        "function": {
                            "name": str(b.get("name") or ""),
                            "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False),
                        },
                    }
                )
            elif t == "tool_result":
                inner = b.get("content") or ""
                tool_results.append(
                    {
                        "tool_call_id": str(b.get("tool_use_id") or ""),
                        "content": inner if isinstance(inner, str) else flatten_content(inner),
                    }
                )
        if tool_calls:
            messages.append(
                {"role": "assistant", "content": "".join(text_parts) or None, "tool_calls": tool_calls}
            )
        elif text_parts:
            messages.append({"role": role, "content": "".join(text_parts)})
        for tr in tool_results:
            messages.append({"role": "tool", "tool_call_id": tr["tool_call_id"], "content": tr["content"]})
        if not text_parts and not tool_calls and not tool_results:
            messages.append({"role": role, "content": ""})
    if not has_messages:
        raise ValueError("messages 不能为空")

    chat: dict = {
        "model": str(req.get("model") or ""),
        "messages": messages,
        "max_tokens": max(1, int(req.get("max_tokens") or 4096)),
    }
    for k in ("temperature", "top_p", "stream"):
        if k in req:
            chat[k] = req[k]
    if req.get("stop_sequences"):
        chat["stop"] = req["stop_sequences"]
    tools = []
    for t in req.get("tools") or []:
        if isinstance(t, dict) and t.get("name"):
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": str(t["name"]),
                        "description": str(t.get("description") or ""),
                        "parameters": t.get("input_schema") or {"type": "object", "properties": []},
                    },
                }
            )
    if tools:
        chat["tools"] = tools
    tc = req.get("tool_choice")
    if isinstance(tc, dict):
        ttype = str(tc.get("type") or "auto")
        if ttype == "any":
            chat["tool_choice"] = "required"
        elif ttype == "tool":
            chat["tool_choice"] = {"type": "function", "function": {"name": str(tc.get("name") or "")}}
        else:
            chat["tool_choice"] = "auto"
    return chat


_STOP_MAP = {"tool_calls": "tool_use", "length": "max_tokens"}


def chat_to_anthropic(chat: dict) -> dict:
    choice = (chat.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    finish = str(choice.get("finish_reason") or "stop")
    content: list[dict] = []
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        content.append({"type": "thinking", "thinking": reasoning})
    text = msg.get("content") if isinstance(msg.get("content"), str) else flatten_content(msg.get("content"))
    if text or not msg.get("tool_calls"):
        content.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            inp = json.loads(str(fn.get("arguments") or "{}"))
        except Exception:
            inp = {}
        content.append(
            {
                "type": "tool_use",
                "id": str(tc.get("id") or rand_id("toolu_")),
                "name": str(fn.get("name") or ""),
                "input": inp if isinstance(inp, dict) else {},
            }
        )
    usage = chat.get("usage") or {}
    return {
        "id": "msg_" + str(chat.get("id") or rand_id()),
        "type": "message",
        "role": "assistant",
        "model": chat.get("model") or "",
        "content": content,
        "stop_reason": _STOP_MAP.get(finish, "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(usage.get("prompt_tokens") or 0),
            "output_tokens": int(usage.get("completion_tokens") or 0),
        },
    }


# ---------------------------------------------------------------- 思考参数降级


def parse_thinking_defaults(raw: str) -> dict[str, str]:
    """解析模型默认思考强度配置：每行 model=effort（如 qwen3=low）。返回 {模型子串: effort}。"""
    out: dict[str, str] = {}
    for line in str(raw or "").splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip().lower(), v.strip().lower()
        if k and v in ("low", "medium", "high"):
            out[k] = v
    return out


def thinking_unsupported(body_text: str, status: int) -> bool:
    """上游报错是否属于「思考参数不兼容」（通常 400，含 thinking/reasoning 关键词）。"""
    if status != 400 or not body_text:
        return False
    low = body_text.lower()
    has_think = any(k in low for k in ("thinking", "reasoning_effort", "reasoning"))
    has_unsupported = any(
        k in low for k in ("unsupported", "not supported", "not support", "supported values", "invalid")
    )
    return has_think and has_unsupported


# 思考强度字段的所有已知变体（不同上游/客户端用的名字不同）
_EFFORT_KEYS = ("reasoning_effort", "thinking_effort", "reasoning_effort_level", "thinking_budget_level")
_THINKING_DROP_KEYS = (
    "reasoning_effort",
    "thinking_effort",
    "reasoning_effort_level",
    "thinking_budget_level",
    "reasoning",
    "thinking",
)
_THINKING_KWARG_KEYS = ("thinking", "enable_thinking", "clear_thinking")


def downgrade_thinking(body: dict, model: str, defaults: dict[str, str]) -> bool:
    """思考参数报错后自动降级。返回是否做了改动。

    规则：模型有默认强度配置 → 用默认强度；无配置 → 去除思考参数。
    defaults: {模型子串: effort}，子串匹配（不区分大小写）。
    覆盖所有已知字段名变体（reasoning_effort / thinking_effort / reasoning.effort 等）。
    """
    m = (model or "").lower()
    default_effort = None
    for pat, eff in defaults.items():
        if pat in m:
            default_effort = eff
            break

    changed = False
    if default_effort:
        # 有默认强度 → 所有已存在的强度字段统一改为默认值（沿用客户端原本的字段名）
        for k in _EFFORT_KEYS:
            if k in body and body[k] != default_effort:
                body[k] = default_effort
                changed = True
        if isinstance(body.get("reasoning"), dict) and body["reasoning"].get("effort"):
            if body["reasoning"]["effort"] != default_effort:
                body["reasoning"]["effort"] = default_effort
                changed = True
        # 若客户端没传任何强度字段（只开了 thinking 开关），补一个标准 reasoning_effort
        if not changed and not any(k in body for k in _EFFORT_KEYS):
            body["reasoning_effort"] = default_effort
            changed = True
    else:
        # 无默认 → 去除思考参数
        for k in _THINKING_DROP_KEYS:
            if body.pop(k, None) is not None:
                changed = True
        ctk = body.get("chat_template_kwargs")
        if isinstance(ctk, dict):
            for k in _THINKING_KWARG_KEYS:
                if ctk.pop(k, None) is not None:
                    changed = True
            if not ctk:
                body.pop("chat_template_kwargs", None)
    return changed


# ---------------------------------------------------------------- 请求体类型归一化

# OpenAI 兼容 API 中应传数值/布尔、但客户端（Sub2API/NewAPI 等）可能误传字符串的字段
_NUMERIC_FIELDS = {
    "temperature",
    "top_p",
    "top_k",
    "max_tokens",
    "max_completion_tokens",
    "min_tokens",
    "n",
    "seed",
    "frequency_penalty",
    "presence_penalty",
    "repetition_penalty",
    "length_penalty",
    "logprobs",
    "top_logprobs",
    "best_of",
    "max_input_tokens",
    "max_output_tokens",
    "max_response_tokens",
    "budget_tokens",
    "parallel_tool_calls",
    "store",
    "ttl_after_seconds",
    "compression_threshold",
    "context_size",
    "num_ctx",
    "num_predict",
    "repeat_penalty",
    "temperature_override",
}
# 布尔字符串字段（值可能为 "true"/"false"/"1"/"0"）
_BOOL_FIELDS = {
    "stream",
    "parallel_tool_calls",
    "store",
    "logprobs",
    "stream_options",
}
# 可安全递归的参数袋（不含 JSON Schema 等可能含字符串默认值的结构）
_PARAM_BAGS = ("reasoning", "thinking", "chat_template_kwargs")


def normalize_body_types(body: dict) -> dict:
    """把请求体里已知数值/布尔字段的字符串形式归一化为 JSON 原生类型。

    客户端（如 Sub2API）可能发送 "temperature": "0.95" 之类的字符串，
    Rust 类型化上游会直接拒绝。只处理白名单字段，避免误伤字符串参数；
    仅递归已知的参数袋 dict，不触碰 tools/functions 等含 schema 的结构。
    """
    if not isinstance(body, dict):
        return body
    for k, v in list(body.items()):
        if k in _BOOL_FIELDS and isinstance(v, str):
            sv = v.strip().lower()
            body[k] = sv in ("true", "1", "yes")
        elif k in _NUMERIC_FIELDS and isinstance(v, str):
            s = v.strip()
            if re.fullmatch(r"-?\d+", s):
                body[k] = int(s)
            elif re.fullmatch(r"-?\d+\.\d+", s):
                body[k] = float(s)
        elif k in _PARAM_BAGS and isinstance(v, dict):
            normalize_body_types(v)
    return body


def is_deserialize_error(body_text: str, status: int) -> bool:
    """上游报错是否为 JSON 反序列化/类型错误（字符串数字被拒绝）。"""
    if status != 400 or not body_text:
        return False
    low = body_text.lower()
    return any(
        k in low
        for k in (
            "failed to deserialize",
            "expected f32",
            "expected i64",
            "invalid type: string",
            "expected a float",
            "expected an integer",
            "deserializ",
        )
    )


# 明显是字符串的顶层字段（即使内容是数字也不能转）
_STRING_ONLY_FIELDS = {
    "model",
    "user",
    "response_format",
    "stop",
    "messages",
    "tools",
    "functions",
    "tool_choice",
    "function_call",
    "reasoning_effort",
    "thinking_effort",
}


def coerce_all_types(body: dict) -> bool:
    """激进转换：顶层及参数袋中所有「看起来像数字/布尔」的字符串→原生类型。

    用于上游 400 反序列化错误时的兜底重试，弥补白名单遗漏的字段。
    会跳过明显是字符串的字段（model/user/messages/tools 等）。返回是否有改动。
    """
    if not isinstance(body, dict):
        return False
    changed = False
    for k, v in list(body.items()):
        if k in _STRING_ONLY_FIELDS:
            continue
        if isinstance(v, str):
            sv = v.strip()
            if sv.lower() in ("true", "false"):
                body[k] = sv.lower() == "true"
                changed = True
            elif re.fullmatch(r"-?\d+", sv):
                body[k] = int(sv)
                changed = True
            elif re.fullmatch(r"-?\d+\.\d+", sv):
                body[k] = float(sv)
                changed = True
        elif k in _PARAM_BAGS and isinstance(v, dict):
            if coerce_all_types(v):
                changed = True
    return changed


def is_unsupported_param_error(body_text: str, status: int) -> bool:
    """上游报错是否为「不支持的参数」类（如 enable_thinking 不被该模型支持）。"""
    if status != 400 or not body_text:
        return False
    low = body_text.lower()
    return (
        "unsupported parameter" in low
        or "unsupported parameter(s)" in low
        or ("validation:" in low and "unsupported" in low)
    )


def strip_unsupported_params(body: dict, body_text: str) -> bool:
    """上游报不支持参数时，从请求体移除被点名的参数（含 thinking 相关），返回是否改动。"""
    if not isinstance(body, dict):
        return False
    changed = False
    # 提取被点名的参数名（如 `enable_thinking`、`thinking`）
    import re as _re

    names = set(_re.findall(r"`([a-z_]+)`", body_text or ""))
    for name in names:
        if name in body:
            body.pop(name, None)
            changed = True
    # 始终兜底清理 thinking 相关
    for k in _THINKING_DROP_KEYS + ("enable_thinking", "clear_thinking"):
        if body.pop(k, None) is not None:
            changed = True
    ctk = body.get("chat_template_kwargs")
    if isinstance(ctk, dict):
        for k in ("thinking", "enable_thinking", "clear_thinking"):
            if ctk.pop(k, None) is not None:
                changed = True
        if not ctk:
            body.pop("chat_template_kwargs", None)
    return changed


# ---------------------------------------------------------------- 上游错误分类（参考 AQUA）


def is_channel_exhausted(body_text: str) -> bool:
    """上游报「渠道级不可用」：同一渠道所有密钥共享渠道池，换钥/重试都注定失败 → 快速失败。"""
    s = (body_text or "").lower()
    return (
        "no available channel" in s
        or "no channel available" in s
        or "无可用渠道" in s
        or "channel_exhausted" in s
    )
