"""通用工具函数。"""

from __future__ import annotations

import json
import math
import os
import re


def rand_id(prefix: str = "") -> str:
    return prefix + os.urandom(6).hex()


def str_cut(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n]


def estimate_request_tokens(body: str, req: dict | None = None) -> int:
    """预估请求 token：请求体按 3 字节≈1 token，输出按 max_tokens 预留。"""
    max_tokens = 300
    if req is None:
        try:
            req = json.loads(body)
        except Exception:
            req = None
    if isinstance(req, dict):
        max_tokens = int(req.get("max_tokens") or req.get("max_output_tokens") or 300)
    return max(1, math.ceil(len(body) / 3) + max(1, max_tokens))


def estimate_output_tokens(data_bytes: int) -> int:
    return max(0, math.ceil(data_bytes / 3))


def _split_loose(s: str) -> list[str]:
    return [p for p in re.split(r"[\r\n,;]+", s or "")]


def literal_items(raw: Any) -> list:
    """把「dict / list / 文本 / 字面量字符串」统一成条目列表。

    前端有两条提交路径：表单发文本，而渠道列表上的「启用/禁用」按钮会直接把整行
    对象回传（models 是数组、model_map 是字典）。解析器必须两种都吃 —— 否则
    str(list) 得到的 "['a']" 会被按逗号切碎，白名单里就挂一个永远匹配不上的名字。
    """
    if isinstance(raw, dict):
        return [f"{k}={v}" for k, v in raw.items()]
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    text = str(raw or "").strip()
    if len(text) >= 2 and text[0] in "[{" and text[-1] in "]}":
        try:
            parsed = json.loads(text.replace("'", '"'))
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            return [f"{k}={v}" for k, v in parsed.items()]
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
        return _split_loose(text[1:-1])
    return _split_loose(text)


def as_bool(v: Any, default: bool = False) -> bool:
    """把 bool / 数字 / 字符串（"false"、"0"、"no"）统一成 bool。

    bool("false") 是 True —— 字符串形式的开关必须显式识别，否则前端传 "false"
    会把功能打开，或把配置静默重置成默认值。
    """
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "y", "on"):
        return True
    if s in ("false", "0", "no", "n", "off", ""):
        return False
    return default


def parse_model_list(s: Any) -> list[str]:
    """解析渠道可用模型列表，容忍各种粘贴格式：

    - 逗号 / 分号 / 换行分隔：`a, b, c`
    - 列表字面量（从 Python / JSON 复制来的）：`['a', 'b']`、`["a", "b"]`
    - 已经是 list（列表页按钮回传整行对象的情况）

    曾经只按分隔符切分，于是把 `['a']` 整段当成一个模型名存了下来，渠道白名单里
    就挂着一个永远匹配不上的名字 —— 该渠道对任何真实请求都会被判「渠道模型不匹配」。
    注意：不能无条件剥方括号，模型名本身可能带 `X[free]` 这类后缀；只在整段看起来
    就是列表字面量时，才按字面量解析。
    """
    seen: dict[str, None] = {}
    for raw in literal_items(s):
        p = str(raw).strip()
        # 只剥掉成对的引号；方括号保持原样（模型名可能自带 [free] 之类后缀）
        if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'":
            p = p[1:-1].strip()
        if p and len(p) <= 160:
            seen[p] = None
    return list(seen)


def upstream_snippet(res: dict) -> str:
    """从上游响应提取人类可读错误。"""
    if res.get("error"):
        return str(res["error"])[:200]
    body = res.get("body") or ""
    try:
        j = json.loads(body)
        if isinstance(j, dict):
            err = j.get("error")
            candidates = [
                err.get("message") if isinstance(err, dict) else None,
                j.get("message"),
                j.get("detail"),
                j.get("title"),
            ]
            for m in candidates:
                if isinstance(m, str) and m:
                    return m[:200]
    except Exception:
        pass
    body = str(body).strip()
    return body[:160] if body else f"上游返回 HTTP {res.get('status', 0)}"


def mask_email(email: str) -> str:
    at = email.find("@")
    if at < 0:
        return email[:2] + "***"
    return email[: min(2, at)] + "***" + email[at:]
