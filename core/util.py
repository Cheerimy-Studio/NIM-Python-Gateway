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


def parse_model_list(s: str) -> list[str]:
    parts = re.split(r"[\r\n,;]+", s or "")
    seen: dict[str, None] = {}
    for p in parts:
        p = p.strip().strip('"').strip("'").strip()
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
