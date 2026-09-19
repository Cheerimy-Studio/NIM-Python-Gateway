"""账号池：CSV 解析导入、多维限速调度、分级冷却、熔断、最近 10 次记录。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time

from . import upstreams
from .store import STORE
from .util import mask_email, str_cut, upstream_snippet

# 进程内在途请求计数：key_id -> 数量（账户/渠道并发限制用，重启归零）
_inflight: dict[str, int] = {}

# 可靠性按「渠道 + 模型」统计时的最小样本数；不足则退回渠道整体成功率
_MODEL_RECENT_MIN = 3


def _model_key(uid: str, model: str) -> str:
    """「渠道 + 模型」联合键。用 NUL 分隔，避免模型名里含分隔符造成歧义。"""
    return f"{uid}\x00{model}"


def _apply_ban(k: dict, until: int, reason: str) -> None:
    if until > (k.get("banned_until") or 0):
        k["banned_until"] = until
        k["ban_reason"] = reason
    if reason == "invalid_key":
        k["status"] = "invalid"


def _cfgint(cfg: dict, name: str, default: int) -> int:
    """读取全局整型配置：字段存在就返回其值（含 0），缺失才用默认值。"""
    v = cfg.get(name)
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _classify(http_status: int, errno: int, error: str) -> str:
    """错误分级。新增 channel/model 级：这两类是上游/渠道问题，不惩罚账号。"""
    low = (error or "").lower()
    # 渠道级不可用：换钥/重试都注定失败，调用方应快速失败
    if (
        "no available channel" in low
        or "no channel available" in low
        or "无可用渠道" in low
        or "channel_exhausted" in low
    ):
        return "channel"
    # 模型级不可用（下线/无通道）：换钥有意义，但不该惩罚账号
    if http_status >= 400 and any(
        k in low
        for k in (
            "model_not_found",
            "model not found",
            "model_disabled",
            "model disabled",
            "model does not exist",
            "does not exist",
            "模型已关闭",
            "模型不存在",
            "not available on any",
        )
    ):
        return "model"
    if http_status == 429:
        return "429"
    if http_status == 402:
        # 402 Payment Required：账号额度/余额已耗尽。重试多少次都注定失败，
        # 必须按硬失败处理（封禁→禁用），否则这个账号会被反复选中反复失败。
        return "payment"
    if http_status in (401, 403):
        return "auth"
    if errno == 28 or "timed out" in low or "timeout" in low:
        return "timeout"
    if errno in (6, 7, 35, 52, 56) or http_status == 0:
        return "conn"
    if http_status >= 500:
        return "5xx"
    return "req"


# ---------------------------------------------------------------- CSV 解析与导入


def parse_accounts(text: str) -> tuple[list[dict], int]:
    """粘贴文本：每行三列 email,password,apikey，仅支持逗号分隔。"""
    return _parse_rows(text, loose=False)


def parse_accounts_loose(text: str) -> tuple[list[dict], int]:
    """文件上传：CSV/TSV 宽松解析，分隔符空格/Tab/逗号/分号/竖线自动识别，列序不限。"""
    return _parse_rows(text, loose=True)


def _parse_rows(text: str, loose: bool) -> tuple[list[dict], int]:
    accounts: list[dict] = []
    invalid = 0
    n = 0
    for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if n > 20000:
            break
        line = line.strip()
        if not line:
            continue
        n += 1
        low = line.lower()
        # 表头行跳过
        if "apikey" in low and "email" in low:
            continue
        if line.startswith(("email", "邮箱", "账号", "user", "用户名")) and "nvapi-" not in low:
            continue
        if loose:
            account = _parse_line_loose(line)
        else:
            cells = [c.strip() for c in re.split(r",+", line) if c.strip()]
            account = _from_cells(cells)
        if account is None:
            invalid += 1
            continue
        accounts.append(account)
    return accounts, invalid


def _parse_line_loose(line: str) -> dict | None:
    cells = [c.strip() for c in re.split(r"[\s,;|]+", line)]
    api_key = ""
    email = ""
    for c in cells:
        if not c:
            continue
        if not api_key and c.lower().startswith("nvapi-"):
            api_key = c
            continue
        if not email and "@" in c and "." in c.split("@")[-1] and " " not in c:
            email = c
            continue
    if not api_key:
        non_empty = [c for c in cells if c]
        if len(non_empty) >= 2 and "@" in non_empty[0] and "." in non_empty[0].split("@")[-1]:
            email = non_empty[0]
            api_key = non_empty[-1]
        elif len(non_empty) == 1 and len(non_empty[0]) >= 16:
            api_key = non_empty[0]
    if not api_key or len(api_key) < 8:
        return None
    password = ""
    for c in cells:
        if not c or c == api_key or (email and c.lower() == email.lower()):
            continue
        password = c
        break
    return {
        "email": email or "unknown-" + hashlib.md5(api_key.encode()).hexdigest()[:8],
        "password": password,
        "apikey": api_key,
    }


def _from_cells(cells: list[str]) -> dict | None:
    if len(cells) != 3:
        return None
    email, password, api_key = cells
    if "@" not in email or "." not in email.split("@")[-1] or len(api_key) < 8:
        return None
    return {"email": email, "password": password, "apikey": api_key}


def new_key(email: str, password: str, apikey: str, upstream_id: str = "") -> dict:
    now = int(time.time())
    return {
        "id": "k_" + os.urandom(5).hex(),
        "email": email,
        "password": password,
        "apikey": apikey,
        "upstream_id": upstream_id,
        "enabled": True,
        "status": "active",
        "created_at": now,
        "updated_at": now,
        "last_used_at": 0,
        "first_seen_at": 0,
        "total_requests": 0,
        "total_success": 0,
        "total_fail": 0,
        "consecutive_failures": 0,
        "hard_fail_count": 0,
        "rl_streak": 0,
        "banned_until": 0,
        "ban_reason": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "last_error": "",
        "last_error_at": 0,
        "daily": {},
        "minute_tokens": {},
        "hour_requests": {},
        "recent": [],
    }


def import_accounts(text: str, upstream_id: str = "", loose_text: str = "") -> dict:
    accounts, invalid = parse_accounts(text)
    if loose_text:
        l_accounts, l_invalid = parse_accounts_loose(loose_text)
        accounts = l_accounts + accounts
        invalid += l_invalid
    res = {"added": 0, "updated": 0, "duplicate": 0, "invalid": invalid, "lines": 0, "total": 0}
    res["lines"] = len(
        [x for x in ((text or "") + "\n" + (loose_text or "")).replace("\r", "").split("\n") if x.strip()]
    )
    if not accounts:
        return res

    def _fn(db: dict):
        by_email = {k["email"].lower(): i for i, k in enumerate(db["keys"])}
        by_key = {k["apikey"]: i for i, k in enumerate(db["keys"])}
        for a in accounts:
            ek = a["email"].lower()
            if a["apikey"] in by_key:
                res["duplicate"] += 1
                continue
            if ek in by_email:
                i = by_email[ek]
                changed = False
                if db["keys"][i]["apikey"] != a["apikey"]:
                    db["keys"][i]["apikey"] = a["apikey"]
                    db["keys"][i]["upstream_id"] = upstream_id
                    changed = True
                if a["password"] and db["keys"][i]["password"] != a["password"]:
                    db["keys"][i]["password"] = a["password"]
                    changed = True
                if changed:
                    db["keys"][i]["updated_at"] = int(time.time())
                    res["updated"] += 1
                else:
                    res["duplicate"] += 1
                continue
            db["keys"].append(new_key(a["email"], a["password"], a["apikey"], upstream_id))
            by_email[ek] = len(db["keys"]) - 1
            by_key[a["apikey"]] = len(db["keys"]) - 1
            res["added"] += 1
        res["total"] = len(db["keys"])

    STORE.update(_fn)
    return res


# ---------------------------------------------------------------- 调度


def _fail_ratio(k: dict) -> float:
    tr = k.get("total_requests") or 0
    return (k.get("total_fail") or 0) / tr if tr else 0.0


def _compare(a: dict, b: dict) -> int:
    """池内择优排序。

    第一优先级是「最久未用」（LRU）——保证账号轮换、负载分散，不会一直压着一个 Key；
    失败数/失败率只作次级判据（失败账号通常已被冷却或封禁过滤，不靠这里降权，
    否则健康账号会被反复选中、其余账号长期闲置）。
    """
    ka = (a.get("last_used_at") or 0, a.get("consecutive_failures") or 0, _fail_ratio(a), a["id"])
    kb = (b.get("last_used_at") or 0, b.get("consecutive_failures") or 0, _fail_ratio(b), b["id"])
    return (ka > kb) - (ka < kb)


def acquire(db: dict | None = None, est_tokens: int = 0, model: str = "") -> dict:
    """取号。db=None 时自行加锁更新（独立调用），否则在调用方的锁内执行。"""
    if db is not None:
        out: dict = {"result": "none", "key": None, "reason": "", "total": 0}
        _acquire_fn(db, out, est_tokens, model)
        return out
    out2: dict = {"result": "none", "key": None, "reason": "", "total": 0}
    STORE.update(lambda d: _acquire_fn(d, out2, est_tokens, model))
    return out2


def _acquire_fn(db: dict, out: dict, est_tokens: int, model: str) -> None:
    """在调用方持有的锁内执行取号逻辑。"""
    out: dict = out  # noqa
    cfg = db["config"]
    now_f = time.time()
    now = int(now_f)
    minute = time.strftime("%Y%m%d%H%M", time.gmtime(now))
    hour = time.strftime("%Y%m%d%H", time.gmtime(now))
    day = time.strftime("%Y-%m-%d", time.localtime(now))

    def gnum(name: str, default: int) -> int:
        """全局限额读取：0 或 -1 = 不限/关闭（返回 0），正数为限额，字段缺失才用默认值。

        注意不能把显式的 0 当成“未设置”回退默认——否则用户设 0（不限）会被套上默认限额。
        """
        v = cfg.get(name)
        if v is None:
            return default
        try:
            iv = int(v)
        except (TypeError, ValueError):
            return default
        return iv if iv > 0 else 0

    cfg_rpm = gnum("rate_limit_per_minute", 20)
    cfg_tpm = gnum("tpm_limit", 50000)
    cfg_cooldown_ms = gnum("account_cooldown_ms", 500)
    cfg_daily_cap = gnum("daily_request_cap", 100)
    cfg_daily_tok = gnum("daily_token_limit", 900000)
    cfg_hourly = gnum("hourly_request_limit", 5)
    cfg_acct_conc = gnum("acct_concurrency", 0)
    cfg_chan_conc = gnum("total_concurrency", 0)
    cfg_pool_rpm = gnum("pool_rpm_cap", 0)
    cfg_pool_daily = gnum("pool_daily_cap", 0)

    ups = {u["id"]: u for u in db.get("upstreams", []) if isinstance(u, dict)}
    reason = {
        "banned": 0,
        "cooldown": 0,
        "rpm": 0,
        "tpm": 0,
        "daily": 0,
        "pool": 0,
        "pool_rpm": 0,
        "pool_daily": 0,
        "channel_model": 0,
        "model_hidden": 0,
        "acct_conc": 0,
        "chan_conc": 0,
    }
    # 渠道在途总量（进程内计数，release 时递减）
    chan_inflight: dict[str, int] = {}
    for k2 in db["keys"]:
        c = _inflight.get(str(k2.get("id") or ""), 0)
        if c > 0:
            u2 = str(k2.get("upstream_id") or "")
            chan_inflight[u2] = chan_inflight.get(u2, 0) + c
    pools: dict[str, dict] = {}
    total = 0

    # 渠道覆盖设置按渠道预计算一次（同渠道账号共享），避免每账号重复解析（大号池显著提速）
    def _chan_eff(up: dict | None, field: str, default: int) -> int:
        """渠道覆盖：-1=不限（返回 0），0=继承全局，正数=覆盖。"""
        v = int((up or {}).get(field) or 0)
        if v == -1:
            return 0
        if v > 0:
            return v
        return int(default)

    def _chan_cfg(up: dict | None) -> dict:
        return {
            "rpm": _chan_eff(up, "rpm", cfg_rpm),
            "tpm": _chan_eff(up, "tpm", cfg_tpm),
            "daily_cap": _chan_eff(up, "daily_request_cap", cfg_daily_cap),
            "daily_tok": _chan_eff(up, "daily_token_limit", cfg_daily_tok),
            "hourly": _chan_eff(up, "hourly_request_limit", cfg_hourly),
            "cooldown_ms": _chan_eff(up, "account_cooldown_ms", cfg_cooldown_ms),
            "acct_conc": _chan_eff(up, "acct_concurrency", cfg_acct_conc),
            "chan_conc": _chan_eff(up, "total_concurrency", cfg_chan_conc),
            "pool_rpm_cap": _chan_eff(up, "rpm_cap", cfg_pool_rpm),
            "pool_daily_cap": _chan_eff(up, "daily_cap", cfg_pool_daily),
            "hide": upstreams.hide_original(up, cfg),
            "models": (up or {}).get("models") or [],
            "targets": list(((up or {}).get("model_map") or {}).values()),
        }

    _cfg_cache: dict[str, dict] = {}

    for k in db["keys"]:
        if not k.get("enabled"):
            continue
        total += 1
        uid = str(k.get("upstream_id") or "")
        up = ups.get(uid)
        if up is not None and not up.get("enabled"):
            reason["pool"] += 1
            continue

        cc = _cfg_cache.get(uid)
        if cc is None:
            cc = _chan_cfg(up)
            _cfg_cache[uid] = cc
        rpm = cc["rpm"]
        tpm = cc["tpm"]
        daily_cap = cc["daily_cap"]
        daily_tok = cc["daily_tok"]
        hourly = cc["hourly"]
        cooldown_ms = cc["cooldown_ms"]
        acct_conc = cc["acct_conc"]
        chan_conc = cc["chan_conc"]
        pool_rpm_cap = cc["pool_rpm_cap"]
        pool_daily_cap = cc["pool_daily_cap"]

        if model and up is not None:
            if cc["hide"] and model in cc["targets"]:
                reason["model_hidden"] += 1
                continue
            models = cc["models"]
            if models and model not in models:
                reason["channel_model"] += 1
                continue

        if (k.get("banned_until") or 0) > now:
            reason["banned"] += 1
            continue
        if (k.get("cooldown_until") or 0) > now:
            reason["cooldown"] += 1
            continue
        if cooldown_ms > 0 and (now_f - float(k.get("last_used_at") or 0)) * 1000 < cooldown_ms:
            reason["cooldown"] += 1
            continue
        # 并发数：0 或 -1 = 不限
        if acct_conc > 0 and _inflight.get(k["id"], 0) >= acct_conc:
            reason["acct_conc"] += 1
            continue
        if chan_conc > 0 and chan_inflight.get(uid, 0) >= chan_conc:
            reason["chan_conc"] += 1
            continue
        # 滑动窗口 RPM：最近 60 秒内的请求时间戳列表（rpm<=0 表示不限）
        win = [t for t in (db.get("buckets", {}).get(k["id"]) or []) if now_f - t < 60]
        first_seen = float(k.get("first_seen_at") or 0)
        # 账号预热：新账号逐步提升到全额 RPM
        if rpm > 0:
            # 预热时长：0=关闭（不能用 or 回退，否则 0 配置会被当成未设置）
            try:
                warmup_s = max(0, int(cfg.get("warmup_seconds")))
            except (TypeError, ValueError):
                warmup_s = 300
            eff_rpm = rpm
            if warmup_s > 0 and first_seen > 0:
                elapsed = now_f - first_seen
                if elapsed < warmup_s:
                    # 预热从全额 RPM 的 50% 起步、线性升满：起步即压到 1 会让新号池完全不可用
                    eff_rpm = max(1, int(rpm * max(0.5, elapsed / warmup_s)))
            if len(win) >= eff_rpm:
                reason["rpm"] += 1
                continue
        used_min = (k.get("minute_tokens") or {}).get(minute) or 0
        # TPM 预检：账号本分钟额度已耗尽，或本次请求会超出额度时跳过。
        # 但若请求自身的估算量就超过整分钟额度，则任何账号都无法承载它，
        # 此时再跳过只会把整个号池逐个耗空（大 prompt 场景下 429 连片的根因）。
        if tpm > 0 and (used_min >= tpm or (est_tokens <= tpm and used_min + est_tokens > tpm)):
            reason["tpm"] += 1
            continue
        d = (k.get("daily") or {}).get(day) or {"requests": 0, "tokens": 0}
        if daily_cap > 0 and d.get("requests", 0) >= daily_cap:
            _apply_ban(k, int(_tomorrow(now)), "daily_cap")
            _replace_key(db, k)
            reason["daily"] += 1
            continue
        if (
            daily_tok > 0
            and d.get("tokens", 0) >= daily_tok
            and hourly > 0
            and (k.get("hour_requests") or {}).get(hour, 0) >= hourly
        ):
            reason["daily"] += 1
            continue

        weight = 10
        if up is not None:
            if (
                pool_rpm_cap > 0
                and (db.get("pool_buckets", {}).get(uid, {}).get(minute) or 0) >= pool_rpm_cap
            ):
                reason["pool_rpm"] += 1
                continue
            if pool_daily_cap > 0 and (db.get("pool_daily", {}).get(uid, {}).get(day) or 0) >= pool_daily_cap:
                reason["pool_daily"] += 1
                continue
            weight = max(1, int(up.get("weight") or 10))
            pools.setdefault(uid, {"w": weight, "items": []})
            pools[uid]["items"].append(k)
            if not first_seen:
                k["first_seen_at"] = now_f

    out["reason"] = _reason_text(reason, total)
    out["total"] = total
    # 只有全部启用账号都因「真正不可恢复」原因被拒才判 permanent（渠道模型不匹配/原名禁用）。
    # 上游停用、日限、封禁、冷却、限流、并发满都是暂时的，会恢复 → 值得排队等待。
    perm_cnt = reason["channel_model"] + reason["model_hidden"]
    out["permanent"] = total > 0 and perm_cnt == total
    if not pools:
        # 全部账号当前不可用：给出最早恢复的等待秒数（队列据此退避，
        # 避免固定 400ms 轮询对正在限流/冷却的账号反复空转打风暴）
        now_i = int(time.time())
        waits = []
        for k in db["keys"]:
            if not k.get("enabled"):
                continue
            for w in ((k.get("banned_until") or 0) - now_i, (k.get("cooldown_until") or 0) - now_i):
                if w > 0:
                    waits.append(w)
        if waits:
            out["wait_hint"] = max(0.5, min(float(min(waits)), 5.0))
        return

    pool_ids = list(pools)
    scores = {}
    recent = db.get("up_recent", {})
    for pid in pool_ids:
        # 可靠性按「渠道 + 模型」定：同一个上游对不同模型的可用性差别很大
        # （某些模型经常排队/下线，而另一些一直正常），用整体成功率会让好模型
        # 被差模型拖累、差模型被好模型掩盖。样本不足时退回渠道整体成功率。
        rec_m = recent.get(_model_key(pid, model)) or []
        rec = rec_m if len(rec_m) >= _MODEL_RECENT_MIN else (recent.get(pid) or [])
        ok_n = sum(1 for v in rec if v)
        scores[pid] = (ok_n + 5) / (len(rec) + 10)
    pool_ids.sort(key=lambda p: (scores[p], pools[p]["w"]), reverse=True)
    chosen = pool_ids[0]
    items = pools[chosen]["items"]
    import functools

    items.sort(key=functools.cmp_to_key(_compare))
    k = items[0]
    k["last_used_at"] = now_f
    k["total_requests"] = k.get("total_requests", 0) + 1
    _inflight[k["id"]] = _inflight.get(k["id"], 0) + 1
    # 只给「真正被使用的账号」打 RPM 时间戳。曾经是给所有候选账号都打点，
    # 于是每个账号的 60 秒窗口被无谓塞满，很快整池一起撞上单账号 RPM 上限
    # → 号池假性枯竭（不论多少账号，吞吐都被压到约等于单账号 RPM）。
    kw = [t for t in (db.get("buckets", {}).get(k["id"]) or []) if now_f - t < 60]
    kw.append(now_f)
    db.setdefault("buckets", {})[k["id"]] = kw
    if chosen:
        pb = db.setdefault("pool_buckets", {}).setdefault(chosen, {})
        pb[minute] = pb.get(minute, 0) + 1
        if len(pb) > 2:
            keep = (int(minute) - 1, int(minute))
            for mk in [m for m in pb if int(m) not in keep]:
                del pb[mk]
        pd = db.setdefault("pool_daily", {}).setdefault(chosen, {})
        pd[day] = pd.get(day, 0) + 1
        if len(pd) > 3:
            for dkey in sorted(pd)[:-3]:
                del pd[dkey]
    _replace_key(db, k)
    out.update({"result": "ok", "key": k, "reason": "", "total": total})


def _tomorrow(now: int) -> int:
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    return int(midnight)


def _reason_text(r: dict, total: int) -> str:
    if total == 0:
        return "密钥池为空"
    labels = [
        ("封禁", "banned"),
        ("冷却", "cooldown"),
        ("RPM", "rpm"),
        ("TPM", "tpm"),
        ("日限", "daily"),
        ("上游停用", "pool"),
        ("上游RPM", "pool_rpm"),
        ("上游日限", "pool_daily"),
        ("渠道模型", "channel_model"),
        ("原名禁用", "model_hidden"),
        ("账户并发", "acct_conc"),
        ("渠道并发", "chan_conc"),
    ]
    parts = [f"{label} {r[key]}" for label, key in labels if r.get(key)]
    return (" · ".join(parts) if parts else "无匹配") + f" / 共 {total}"


def _replace_key(db: dict, k: dict) -> None:
    for i, row in enumerate(db["keys"]):
        if row["id"] == k["id"]:
            db["keys"][i] = k
            return


def release(
    key_id: str,
    success: bool,
    http_status: int,
    error: str = "",
    usage: dict | None = None,
    log: dict | None = None,
    errno: int = 0,
) -> None:
    # 释放并发占用（take_account 成功时在 _acquire_fn 中 +1）
    c = _inflight.get(key_id, 0)
    if c > 1:
        _inflight[key_id] = c - 1
    else:
        _inflight.pop(key_id, None)

    def _fn(db: dict):
        now = int(time.time())
        now_f = time.time()
        cfg = db["config"]
        minute = time.strftime("%Y%m%d%H%M", time.gmtime(now))
        hour = time.strftime("%Y%m%d%H", time.gmtime(now))
        day = time.strftime("%Y-%m-%d", time.localtime(now))

        if key_id:
            for i, k in enumerate(db["keys"]):
                if k.get("id") != key_id:
                    continue
                up_row = next((u for u in db.get("upstreams", []) if u["id"] == k.get("upstream_id")), None)

                def eff(field: str, default) -> int:
                    """渠道覆盖：-1=不惩罚（返回 0，跳过该项），0=继承全局，正数=覆盖。"""
                    v = int((up_row or {}).get(field) or 0)
                    if v == -1:
                        return 0
                    if v > 0:
                        return v
                    return int(default)

                if success:
                    k["total_success"] = k.get("total_success", 0) + 1
                    k["consecutive_failures"] = 0
                    k["rl_streak"] = 0  # 成功即清零"连续被限流"计数
                else:
                    k["total_fail"] = k.get("total_fail", 0) + 1
                    cls = _classify(http_status, errno, error)
                    if cls in ("req", "channel", "model"):
                        # 请求类/渠道级/模型级错误：不是账号的问题，不惩罚账号
                        k["consecutive_failures"] = 0
                    elif cls == "429":
                        k["consecutive_failures"] = 0
                        # 连续被限流 → 冷却指数退避（30→60→120→240→300 封顶）。
                        # 反复用同一账号硬重试只会持续触发 429、让上游限流窗口无法恢复。
                        streak = int(k.get("rl_streak") or 0) + 1
                        k["rl_streak"] = streak
                        base = eff("cool_429_seconds", _cfgint(cfg, "cool_429_seconds", 30))
                        if base > 0:
                            cool = min(base * (2 ** min(streak - 1, 4)), 300)
                            k["cooldown_until"] = max(k.get("cooldown_until") or 0, now + cool)
                    elif cls in ("auth", "payment"):
                        # 401/403 鉴权失败、402 余额不足：都按「硬失败」处理，走同一套
                        # 阶梯（先封禁，累计到阈值后禁用一天）。
                        k["consecutive_failures"] = k.get("consecutive_failures", 0) + 1
                        k["hard_fail_count"] = k.get("hard_fail_count", 0) + 1
                        reason = "no_credit" if cls == "payment" else "invalid_key"
                        limit = eff("hard_fail_disable_count", _cfgint(cfg, "hard_fail_disable_count", 3))
                        if limit > 0 and k["hard_fail_count"] >= limit:
                            _apply_ban(k, now + 86400, reason)
                        else:
                            ban_s = eff("hard_fail_ban_seconds", _cfgint(cfg, "hard_fail_ban_seconds", 600))
                            if ban_s > 0:
                                _apply_ban(k, now + ban_s, reason)
                    else:
                        # 5xx / 超时 / 连接失败：阶梯封禁 + 分级冷却
                        k["consecutive_failures"] = k.get("consecutive_failures", 0) + 1
                        step = eff("ban_step_seconds", _cfgint(cfg, "ban_step_seconds", 5))
                        cap = eff("ban_max_seconds", _cfgint(cfg, "ban_max_seconds", 300))
                        if step > 0:
                            _apply_ban(k, now + min(step * k["consecutive_failures"], cap), "fail_ladder")
                        cool_map = {
                            "5xx": "cool_5xx_seconds",
                            "timeout": "cool_timeout_seconds",
                            "conn": "cool_conn_seconds",
                        }
                        if cls in cool_map:
                            cool = eff(cool_map[cls], _cfgint(cfg, cool_map[cls], 30))
                            if cool > 0:
                                k["cooldown_until"] = max(k.get("cooldown_until") or 0, now + cool)
                    if error:
                        k["last_error"] = str_cut(error, 200)
                        k["last_error_at"] = now

                tin = int((usage or {}).get("prompt_tokens") or 0)
                tout = int((usage or {}).get("completion_tokens") or 0)
                k["prompt_tokens"] = k.get("prompt_tokens", 0) + tin
                k["completion_tokens"] = k.get("completion_tokens", 0) + tout

                daily = k.setdefault("daily", {})
                d = daily.get(day) or {"requests": 0, "tokens": 0}
                d["requests"] = d.get("requests", 0) + 1
                d["tokens"] = d.get("tokens", 0) + tin + tout
                daily[day] = d
                if len(daily) > 3:
                    for dk in sorted(daily)[:-3]:
                        del daily[dk]

                mt = k.setdefault("minute_tokens", {})
                mt[minute] = mt.get(minute, 0) + tin + tout
                if len(mt) > 2:
                    keep = (int(minute) - 1, int(minute))
                    for mk in [m for m in mt if int(m) not in keep]:
                        del mt[mk]

                hr = k.setdefault("hour_requests", {})
                hr[hour] = hr.get(hour, 0) + 1
                if len(hr) > 2:
                    keep = (int(hour) - 1, int(hour))
                    for mk in [m for m in hr if int(m) not in keep]:
                        del hr[mk]

                recent = k.setdefault("recent", [])
                recent.insert(
                    0,
                    [
                        now,
                        str_cut(str(log.get("ep")) if log else "", 8),
                        str_cut(str(log.get("model")) if log else "", 60),
                        http_status,
                        int((log.get("ms") if log else 0) or 0),
                        str_cut(error, 120),
                    ],
                )
                del recent[10:]

                # 渠道近期可行性（最近 20 次成功率，供取号智能路由）。
                # 同时记两份：渠道整体 + 「渠道+模型」，路由时优先用按模型的那份。
                uid = str(k.get("upstream_id") or "")
                if uid:
                    flag = 1 if success else 0
                    recent = db.setdefault("up_recent", {})
                    rec = recent.setdefault(uid, [])
                    rec.insert(0, flag)
                    del rec[20:]
                    m_name = str_cut(str(log.get("model")) if log else "", 80)
                    if m_name and m_name != "-":
                        rec_m = recent.setdefault(_model_key(uid, m_name), [])
                        rec_m.insert(0, flag)
                        del rec_m[20:]

                # 模型熔断：仅统计真实上游侧失败（5xx / 超时 / 连接失败）。
                # 请求类错误（400/404/410 等，多因下游参数或模型下线）、鉴权、限流不计，
                # 下游排队/无账号等未触达上游的拒绝也不会走到这里（key_id 为空）。
                if uid and not success and _classify(http_status, errno, error) in ("5xx", "timeout", "conn"):
                    model_name = str_cut(str(log.get("model")) if log else "", 80)
                    if model_name and model_name != "-" and cfg.get("breaker_enabled", True):
                        br = db.setdefault("model_breaker", {}).setdefault(
                            model_name, {"fails": 0, "opened_until": 0}
                        )
                        br["fails"] = br.get("fails", 0) + 1
                        th = eff("breaker_threshold", _cfgint(cfg, "breaker_threshold", 3))
                        bs = eff("breaker_seconds", _cfgint(cfg, "breaker_seconds", 60))
                        if th > 0 and bs > 0 and br["fails"] >= th:
                            br["opened_until"] = now + bs
                break

        # 每日统计（保留 14 天）—— 仅统计实际到达上游的请求
        if key_id:
            st = db["stats"].get(day) or {"total": 0, "success": 0, "fail": 0, "models": {}}
            st["total"] += 1
            if success:
                st["success"] += 1
            else:
                st["fail"] += 1
            model = str(log.get("model") if log else "")
            if success and model and model != "-":
                st["models"][model] = st["models"].get(model, 0) + 1
                if len(st["models"]) > 50:
                    keep = dict(sorted(st["models"].items(), key=lambda x: -x[1])[:50])
                    st["models"] = keep
            db["stats"][day] = st
            if len(db["stats"]) > 14:
                for dk in sorted(db["stats"])[:-14]:
                    del db["stats"][dk]

        # 请求日志
        if log and cfg.get("log_enabled", True):
            logs = db.setdefault("logs", [])
            # 扩展字段（新格式）：up_model / stream / ttfb / in_tok / out_tok
            logs.insert(
                0,
                [
                    int(log.get("t") or now),
                    str_cut(str(log.get("ep")), 8),
                    str_cut(str(log.get("model")), 80),
                    str_cut(str(log.get("key")), 40),
                    int(log.get("st") or 0),
                    int(log.get("ms") or 0),
                    str_cut(str(log.get("err")), 140),
                    str_cut(str(log.get("ip")), 45),
                    int(log.get("att") or 1),
                    str_cut(str(log.get("up_model") or ""), 80),
                    1 if log.get("stream") else 0,
                    int(log.get("ttfb") or 0),
                    int(log.get("in_tok") or 0),
                    int(log.get("out_tok") or 0),
                ],
            )
            max_logs = max(0, _cfgint(cfg, "log_max", 200))
            del logs[max_logs:]

    STORE.update(_fn)


async def arelease(
    key_id: str,
    success: bool,
    http_status: int,
    error: str = "",
    usage: dict | None = None,
    log: dict | None = None,
    errno: int = 0,
) -> None:
    """异步版 release：不阻塞事件循环，高并发下不占主线程。"""
    import asyncio

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, release, key_id, success, http_status, error, usage, log, errno)


def breaker_open(model: str) -> dict | None:
    cfg = STORE.load()["config"]
    if not cfg.get("breaker_enabled", True):
        return None
    br = (STORE.load().get("model_breaker") or {}).get(model)
    if not isinstance(br, dict):
        return None
    left = int(br.get("opened_until") or 0) - int(time.time())
    if left <= 0:
        return None
    return {"fails": int(br.get("fails") or 0), "left": left}


# ---------------------------------------------------------------- 管理操作


def set_enabled(key_id: str, enabled: bool) -> bool:
    found = False

    def _fn(db: dict):
        nonlocal found
        for k in db["keys"]:
            if k["id"] == key_id:
                found = True
                k["enabled"] = enabled
                if enabled:
                    k["status"] = "active"
                    k["consecutive_failures"] = 0
                    k["hard_fail_count"] = 0
                    k["banned_until"] = 0
                    k["ban_reason"] = ""
                else:
                    k["status"] = "manual_disabled"
                k["updated_at"] = int(time.time())
                break

    STORE.update(_fn)
    return found


def delete_key(key_id: str) -> bool:
    found = False

    def _fn(db: dict):
        nonlocal found
        for i, k in enumerate(db["keys"]):
            if k["id"] == key_id:
                found = True
                del db["keys"][i]
                db["buckets"].pop(key_id, None)
                break

    STORE.update(_fn)
    return found


def reset_stats(key_id: str) -> bool:
    found = False

    def _fn(db: dict):
        nonlocal found
        for k in db["keys"]:
            if k["id"] == key_id:
                found = True
                for f in (
                    "total_requests",
                    "total_success",
                    "total_fail",
                    "consecutive_failures",
                    "hard_fail_count",
                    "prompt_tokens",
                    "completion_tokens",
                ):
                    k[f] = 0
                k["last_error"] = ""
                k["last_error_at"] = 0
                k["banned_until"] = 0
                k["ban_reason"] = ""
                k["daily"] = {}
                k["recent"] = []
                if k.get("enabled"):
                    k["status"] = "active"
                break

    STORE.update(_fn)
    return found


def clear_all() -> int:
    n = len(STORE.load().get("keys", []))

    def _fn(db: dict):
        db["keys"] = []
        db["buckets"] = {}

    STORE.update(_fn)
    return n


def reset_all_stats() -> None:
    def _fn(db: dict):
        for k in db["keys"]:
            for f in (
                "total_requests",
                "total_success",
                "total_fail",
                "consecutive_failures",
                "hard_fail_count",
                "prompt_tokens",
                "completion_tokens",
            ):
                k[f] = 0
            k["last_error"] = ""
            k["last_error_at"] = 0
            k["banned_until"] = 0
            k["ban_reason"] = ""
            k["daily"] = {}
            k["recent"] = []
            if k.get("enabled"):
                k["status"] = "active"
        db["stats"] = {}

    STORE.update(_fn)


def test_key(key_id: str) -> dict:
    key = next((k for k in list(STORE.load().get("keys") or []) if k.get("id") == key_id), None)
    if key is None:
        return {"ok": False, "error": "密钥不存在"}
    import httpx

    base = upstreams.base_for(key)
    t0 = time.time()
    status = 0
    body = ""
    try:
        r = httpx.get(base + "/models", headers={"Authorization": "Bearer " + str(key["apikey"])}, timeout=20)
        status = r.status_code
        body = r.text
    except Exception as e:
        body = str(e)
    ms = int((time.time() - t0) * 1000)
    try:
        data = json.loads(body)
    except Exception:
        data = None
    ok = 200 <= status < 400 and isinstance(data, dict)
    count = len(data.get("data") or []) if ok else 0
    err = (
        "" if ok else upstream_snippet({"status": status, "body": body, "error": body if status == 0 else ""})
    )
    release(
        key_id,
        ok,
        status,
        err,
        None,
        {
            "t": int(time.time()),
            "ep": "test",
            "model": "-",
            "key": mask_email(str(key["email"])),
            "st": status,
            "ms": ms,
            "err": err,
            "ip": "-",
            "att": 1,
        },
    )
    return {"ok": ok, "status": status, "ms": ms, "models": count, "error": err}
