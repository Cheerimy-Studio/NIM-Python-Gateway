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


def parse_model_list(s) -> list[str]:
    """解析渠道可用模型列表，容忍各种粘贴格式：

    - 逗号 / 分号 / 换行分隔：`a, b, c`
    - 列表字面量（从 Python / JSON 复制来的）：`['a', 'b']`、`["a", "b"]`
    - 已经是 list / tuple（直接走 API 的情况）

    曾经只按分隔符切分，于是把 `['a']` 整段当成一个模型名存了下来，渠道白名单里
    就挂着一个永远匹配不上的名字 —— 该渠道对任何真实请求都会被判「渠道模型不匹配」。
    注意：不能无条件剥方括号，模型名本身可能带 `X[free]` 这类后缀；只在整段看起来
    就是列表字面量时，才按字面量解析。
    """
    if isinstance(s, (list, tuple)):
        raw = [str(x) for x in s]
    else:
        text = str(s or "").strip()
        raw = None
        if len(text) >= 2 and text[0] in "[(" and text[-1] in ")]":
            inner = text[1:-1]
            try:
                parsed = json.loads("[" + inner.replace("'", '"') + "]")
                if isinstance(parsed, list):
                    raw = [str(x) for x in parsed]
            except Exception:
                raw = None
            if raw is None:
                raw = _split_loose(inner)
        if raw is None:
            raw = _split_loose(text)
    seen: dict[str, None] = {}
    for p in raw:
        p = p.strip()
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
