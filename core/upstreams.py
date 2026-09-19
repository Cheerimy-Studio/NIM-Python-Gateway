"""上游渠道管理：CRUD、模型策略、模型映射、固定参数、可达性预检。"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from .store import STORE
from .util import as_bool, literal_items, parse_model_list


def all_upstreams() -> list[dict]:
    # 快照：update() 可能在 executor 线程里原地 append，直接遍历共享列表有竞态
    return [u for u in list(STORE.load().get("upstreams") or []) if isinstance(u, dict)]


def get_upstream(uid: str) -> dict | None:
    for u in all_upstreams():
        if u["id"] == uid:
            return u
    return None


def ensure_default() -> None:
    db = STORE.load()
    ids = [u["id"] for u in db.get("upstreams", [])]
    need = not ids or any(str(k.get("upstream_id") or "") not in ids for k in db.get("keys", []))
    if need:
        STORE.update(_migrate)


def _migrate(db: dict) -> None:
    if not db.get("upstreams"):
        db["upstreams"] = [
            {
                "id": "u_" + os.urandom(6).hex(),
                "name": "NVIDIA NIM",
                "base": str(db["config"].get("upstream_base") or "https://integrate.api.nvidia.com/v1"),
                "enabled": True,
                "weight": 10,
                "rpm_cap": 0,
                "daily_cap": 0,
                "rpm": 0,
                "tpm": 0,
                "daily_request_cap": 0,
                "daily_token_limit": 0,
                "request_timeout": 0,
                "models": [],
                "model_map": {},
                "hide_errors": 0,
                "hide_mapped": 0,
                "param_overrides": {},
            }
        ]
    ids = [u["id"] for u in db["upstreams"]]
    default_id = db["upstreams"][0]["id"]
    for i, k in enumerate(db["keys"]):
        uid = str(k.get("upstream_id") or "")
        if uid not in ids:
            db["keys"][i]["upstream_id"] = default_id


def base_for(key: dict) -> str:
    uid = str(key.get("upstream_id") or "")
    for u in all_upstreams():
        if u["id"] == uid:
            return str(u["base"]).rstrip("/")
    from .store import STORE

    return str(STORE.load()["config"].get("upstream_base", "")).rstrip("/")


def override_for(key: dict, field: str, global_value) -> int:
    """重试/超时类字段覆盖：0 或 -1 均继承全局（重试与超时无"不限"语义）。"""
    uid = str(key.get("upstream_id") or "")
    for u in all_upstreams():
        if u["id"] == uid:
            v = int(u.get(field) or 0)
            return v if v > 0 else int(global_value)
    return int(global_value)


def flag_for(uid: str, field: str, global_on: bool) -> bool:
    for u in all_upstreams():
        if u["id"] == uid:
            v = int(u.get(field) or 0)
            return global_on if v == 0 else v == 1
    return global_on


def upstream_value(key: dict, field: str, default: str = "") -> str:
    """获取渠道字符串字段：空=继承全局。"""
    uid = str(key.get("upstream_id") or "")
    for u in all_upstreams():
        if u["id"] == uid:
            v = str(u.get(field) or "").strip()
            return v if v else str(default)
    return str(default)


def map_model_for(key: dict, model: str) -> str:
    uid = str(key.get("upstream_id") or "")
    for u in all_upstreams():
        if u["id"] == uid:
            return str((u.get("model_map") or {}).get(model) or model)
    return model


def _join_lines(raw: Any) -> str:
    """把「列表 / 字典 / 文本 / 字面量字符串」统一成每行一条的文本。"""
    parts = []
    for x in literal_items(raw):
        t = str(x).strip()
        if len(t) >= 2 and t[0] == t[-1] and t[0] in "\"'":
            t = t[1:-1].strip()
        if t:
            parts.append(t)
    return "\n".join(parts)[:2000]


def parse_model_map(raw: Any) -> dict[str, str]:
    # 统一走 literal_items：字典 / 数组（列表页按钮回传整行）/ 文本 / 字面量字符串都能解析。
    # 曾经只处理 dict 与「按行 =」文本，前端回传数组或 JSON 字符串时映射会被静默丢掉。
    lines = literal_items(raw)
    out: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        pair = re.split(r"[=>]", line, maxsplit=1)
        if (
            len(pair) == 2
            and pair[0].strip()
            and pair[1].strip()
            and len(pair[0]) <= 160
            and len(pair[1]) <= 160
        ):
            out[pair[0].strip()] = pair[1].strip()
        if len(out) >= 200:
            break
    return out


def parse_param_overrides(raw: Any) -> dict[str, Any]:
    """解析为 {scope: {param: value}}；scope='*' 作用于全部模型。

    值统一做类型归一化（数字→int/float、true/false→bool、其余字符串），
    避免词典形式（前端可视化编辑器）把 "0.95" 当字符串发给上游。
    """
    if isinstance(raw, dict):
        scoped: dict[str, dict] = {}
        flat = False
        for k, v in raw.items():
            if isinstance(v, dict):
                scoped[str(k)] = {pk: _coerce_param_value(pv) for pk, pv in v.items()}
            else:
                flat = True
        if flat or (not scoped and raw):
            lines = [f"{k}={v}" for k, v in raw.items()]
            return {"*": _parse_param_pairs("\n".join(lines))}
        return scoped
    return _parse_scoped_text(str(raw or ""))


def _coerce_param_value(val: Any) -> Any:
    """把参数值归一化为 JSON 原生类型。

    字符串数字/布尔 → int/float/bool；以 [ 或 { 开头的合法 JSON → 数组/对象
    （否则像 stop=["a","b"]、logit_bias={"1":5} 这类参数只能以字符串发上去，
    上游会直接拒绝）；其余保持字符串。
    """
    if not isinstance(val, str):
        return val
    v = val.strip()
    low = v.lower()
    if low in ("true", "yes"):
        return True
    if low in ("false", "no"):
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    if re.fullmatch(r"-?\d+\.\d+", v):
        return float(v)
    if v[:1] in "[{":
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            pass
    return v


def _parse_param_pairs(text: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    pieces: list[str] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        # 只有一行里出现多组 k=v 时才按逗号/分号拆 —— 否则逗号可能是值的一部分
        # （例如数组值 stop=["a","b"]），盲拆会把值切断并静默丢掉后半段。
        pieces.extend(re.split(r"[,;]", line) if line.count("=") > 1 else [line])
    for piece in pieces:
        piece = piece.strip()
        if "=" not in piece:
            continue
        name, _, val = piece.partition("=")
        name = name.strip()
        val = val.strip()
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,31}", name) or len(val) > 2000:
            continue
        out[name] = _coerce_param_value(val)
    return out


def _parse_scoped_text(text: str) -> dict[str, dict]:
    scoped: dict[str, dict] = {}
    scope = "*"
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^([^=:]{1,160}):\s*(.+)$", line)
        if m and "=" in m.group(2):
            scope = m.group(1).strip()
            line = m.group(2)
        for piece in re.split(r"[,;]", line):
            piece = piece.strip()
            if "=" not in piece:
                continue
            name, _, val = piece.partition("=")
            name = name.strip()
            val = val.strip()
            if not re.fullmatch(r"[a-z_][a-z0-9_]{0,31}", name) or len(val) > 200:
                continue
            scoped.setdefault(scope, {})[name] = _parse_param_pairs(f"{name}={val}").get(name)
        scope = "*"
    return scoped


def apply_param_overrides(key: dict, model: str, body: dict) -> dict:
    """全局 → 渠道(*) → 渠道(模型)，逐级强制覆盖。"""
    cfg = STORE.load()["config"]
    merged = dict(_parse_param_pairs(str(cfg.get("param_overrides") or "")))
    uid = str(key.get("upstream_id") or "")
    for u in all_upstreams():
        if u["id"] == uid:
            scoped = parse_param_overrides(u.get("param_overrides") or {})
            merged.update(scoped.get("*") or {})
            for scope, params in scoped.items():
                if scope != "*" and (scope == model or _fnmatch(scope, model)):
                    merged.update(params)
            break
    for name, val in merged.items():
        if re.fullmatch(r"[a-z_][a-z0-9_]{0,31}", name):
            # 兼容历史数据：旧版可视化编辑器把值存成字符串，统一归一化类型
            body[name] = _coerce_param_value(val)
    return body


def _fnmatch(pattern: str, name: str) -> bool:
    import fnmatch

    return fnmatch.fnmatchcase(name, pattern)


def model_routable(model: str, hide_mapped_global: bool) -> bool:
    any_enabled = False
    for u in all_upstreams():
        if not u.get("enabled"):
            continue
        any_enabled = True
        models = u.get("models") or []
        if models and model not in models:
            continue
        # 直接读渠道自身 hide_mapped（0=继承全局），避免再嵌套查表（原为 O(n²)）
        hm = int(u.get("hide_mapped") or 0)
        hide = hide_mapped_global if hm == 0 else hm == 1
        if hide and model in (u.get("model_map") or {}).values():
            continue
        return True
    return not any_enabled


def hide_original(up: dict | None, cfg: dict | None = None) -> bool:
    """渠道是否禁用「上游原名」访问：渠道 hide_mapped 1=强制开启，0=继承全局。

    开启后客户端只能用别名（model_map 的键）调用，直接用上游原名会被拒绝。
    """
    v = int((up or {}).get("hide_mapped") or 0)
    if v == 1:
        return True
    if v == 0:
        return bool((cfg or STORE.load()["config"]).get("hide_mapped_names", True))
    return False


def curated_models() -> list[str]:
    """网关对外可调用的模型清单（只由渠道配置决定，不访问上游）。

    - 渠道显式配置的 models（客户端侧名称）
    - model_map 的别名（键）—— 别名始终是可用入口

    渠道禁用原名访问时，上游原名（model_map 的 value）会被拒绝，必须从清单中剔除：
    否则 /v1/models 会宣传一个调用必然失败的模型名。
    """
    ups = [u for u in all_upstreams() if u.get("enabled")]
    cfg = STORE.load()["config"]
    out: dict[str, None] = {}
    curated = False
    for u in ups:
        hidden = set((u.get("model_map") or {}).values()) if hide_original(u, cfg) else set()
        for m in u.get("models") or []:
            if m in hidden:
                continue
            out[m] = None
            curated = True
    if curated:
        for u in ups:
            for m in u.get("model_map") or {}:
                out[m] = None
        return list(out)
    # 没有渠道配置白名单：配置层面无从枚举，但别名始终可调用，至少列出来
    for u in ups:
        for m in u.get("model_map") or {}:
            out[m] = None
    return list(out)


def validate_save(data: dict) -> tuple[dict | None, str]:
    name = str(data.get("name") or "").strip()[:40]
    base = str(data.get("base") or "").strip().rstrip("/")
    if not name:
        return None, "名称不能为空"
    if not re.match(r"^https?://", base):
        return None, "Base URL 必须以 http(s):// 开头"

    def clamp(field: str, lo: int, hi: int, default: int) -> int:
        """-1 = 不限制（存为 -1），0 = 继承/默认，正数 = 覆盖值。

        容忍字符串数字与 bool 字符串：前端两种提交路径下同一字段可能是 0/1、
        "0"、"true"/"false"。以前 "true" 会让 int() 抛错并静默退回默认值。
        """
        v = data.get(field, default)
        if isinstance(v, str) and v.strip().lower() in ("true", "false", "yes", "no", "on", "off"):
            v = 1 if as_bool(v) else 0
        try:
            iv = int(v)
            if iv == -1:
                return -1
            # lo 以前是个没人用的参数：下限从未生效（例如 weight 允许存 0）
            return min(hi, max(lo, iv))
        except (TypeError, ValueError):
            return default

    row = {
        "name": name,
        "base": base,
        "weight": clamp("weight", 1, 100, 10),
        "rpm_cap": clamp("rpm_cap", 0, 1_000_000, 0),
        "daily_cap": clamp("daily_cap", 0, 100_000_000, 0),
        "rpm": clamp("rpm", 0, 1_000_000, 0),
        "tpm": clamp("tpm", 0, 100_000_000, 0),
        "daily_request_cap": clamp("daily_request_cap", 0, 100_000_000, 0),
        "daily_token_limit": clamp("daily_token_limit", 0, 10_000_000_000, 0),
        "request_timeout": clamp("request_timeout", 0, 3600, 0),
        "connect_timeout": clamp("connect_timeout", 0, 120, 0),
        "account_cooldown_ms": clamp("account_cooldown_ms", 0, 60_000, 0),
        "acct_concurrency": clamp("acct_concurrency", 0, 10_000, 0),
        "total_concurrency": clamp("total_concurrency", 0, 10_000, 0),
        "hourly_request_limit": clamp("hourly_request_limit", 0, 100_000, 0),
        "max_retries": clamp("max_retries", 0, 20, 0),
        "retry_backoff_base_ms": clamp("retry_backoff_base_ms", 0, 60_000, 0),
        "retry_backoff_max_ms": clamp("retry_backoff_max_ms", 0, 300_000, 0),
        "ban_step_seconds": clamp("ban_step_seconds", 0, 3600, 0),
        "ban_max_seconds": clamp("ban_max_seconds", 0, 86_400, 0),
        "hard_fail_ban_seconds": clamp("hard_fail_ban_seconds", 0, 86_400, 0),
        "hard_fail_disable_count": clamp("hard_fail_disable_count", 0, 100, 0),
        "cool_429_seconds": clamp("cool_429_seconds", 0, 3600, 0),
        "cool_5xx_seconds": clamp("cool_5xx_seconds", 0, 3600, 0),
        "cool_timeout_seconds": clamp("cool_timeout_seconds", 0, 3600, 0),
        "cool_conn_seconds": clamp("cool_conn_seconds", 0, 3600, 0),
        "breaker_threshold": clamp("breaker_threshold", 0, 50, 0),
        "breaker_seconds": clamp("breaker_seconds", 0, 86_400, 0),
        # 不预先 str()：list 入参也能被 parse_model_list 正确识别
        "models": parse_model_list(data.get("models") or "")[:300],
        "model_map": parse_model_map(data.get("model_map") or {}),
        "hide_errors": clamp("hide_errors", 0, 2, 0),
        "hide_mapped": clamp("hide_mapped", 0, 2, 0),
        "param_overrides": parse_param_overrides(data.get("param_overrides") or {}),
        # 思考强度默认值：同样是「表单发文本 / 按钮回传整行对象」两种来源，
        # 统一归一化成每行一条的文本，避免把 ['kimi=low'] 整段当成一条配置。
        "thinking_defaults": _join_lines(data.get("thinking_defaults")),
        # bool("false") 是 True：开关必须按字符串语义解析，否则会反向生效
        "enabled": as_bool(data.get("enabled", True), True),
    }
    return row, ""


def save(data: dict) -> tuple[dict | None, str]:
    row, err = validate_save(data)
    if row is None:
        return None, err
    uid = str(data.get("id") or "")
    from .store import STORE

    def _fn(db: dict):
        for i, u in enumerate(db["upstreams"]):
            if u["id"] == uid:
                row["id"] = uid
                row["created_at"] = u.get("created_at")
                if row["enabled"]:
                    row.pop("auto_disabled_at", None)
                    row.pop("auto_reason", None)
                db["upstreams"][i] = row
                return
        row["id"] = "u_" + __import__("os").urandom(6).hex()
        row["created_at"] = int(time.time())
        db["upstreams"].append(row)

    STORE.update(_fn)
    return get_upstream(row["id"]), ""


def delete(uid: str) -> tuple[bool, str]:
    db = STORE.load()
    found = any(u["id"] == uid for u in db.get("upstreams", []))
    if not found:
        return False, "上游不存在"
    used = sum(1 for k in db.get("keys", []) if k.get("upstream_id") == uid)
    if used:
        return False, f"该上游仍有 {used} 个账号，请先删除或转移"
    if len(db.get("upstreams", [])) <= 1:
        return False, "至少保留一个上游"

    def _fn(db: dict):
        db["upstreams"] = [u for u in db["upstreams"] if u["id"] != uid]
        db["pool_buckets"].pop(uid, None)
        db["pool_daily"].pop(uid, None)
        # 连同该渠道的「渠道+模型」可靠性记录（键为 uid\x00model）一起清掉
        recent = db.get("up_recent", {})
        for key in [k for k in recent if k == uid or k.startswith(uid + "\x00")]:
            recent.pop(key, None)

    STORE.update(_fn)
    return True, ""
