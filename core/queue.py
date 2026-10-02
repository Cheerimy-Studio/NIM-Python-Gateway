"""排队系统：FIFO 队列、位置追踪、统计、公开状态查询。"""

from __future__ import annotations

import os
import time

from .store import STORE

# 队列条目上限：防止大量请求堆积把 db 撑大、拖慢遍历。
# 超出时只保留最新的（等待中的请求不依赖条目存在，靠自身循环重试取号）。
QUEUE_MAX_ENTRIES = 500


def _cfg_max_wait() -> int:
    # 显式 0 = 关闭排队;fallback 与 DEFAULT_CONFIG 一致(15)
    try:
        v = int(STORE.load()["config"].get("queue_max_wait"))
    except (TypeError, ValueError):
        v = 15
    return 0 if v == 0 else max(5, v)


def add(ep: str, model: str, ip: str, tok: str = "", reason: str = "") -> str:
    """入队（FIFO）。

    每次入队都全表过滤一遍是没必要的开销（而且是在存储锁里做）：过期条目
    stats() 本就会跳过，这里只在超过上限时裁剪，保持 O(1)。
    reason = 当前的阻塞原因（如「冷却 12 · 账户并发 3 / 共 202」），供后台与
    队列页回答「为什么在排队」。
    """
    qid = "q_" + os.urandom(6).hex()

    def _fn(db: dict):
        q = db.get("queue")
        if not isinstance(q, list):
            q = []
            db["queue"] = q
        q.append({
            "id": qid, "t": time.time(), "ip": ip, "ep": ep[:8],
            "model": model[:60], "tok": tok[:20], "reason": str(reason)[:90],
        })
        if len(q) > QUEUE_MAX_ENTRIES:
            del q[:-QUEUE_MAX_ENTRIES]  # 只保留最新，防无限增长

    STORE.update(_fn)
    return qid


def set_reason(qid: str, reason: str) -> None:
    """刷新某条等待的阻塞原因（等待期间原因会变：从冷却变成并发满等）。"""
    r = str(reason)[:90]

    def _fn(db: dict):
        for e in db.get("queue") or []:
            if isinstance(e, dict) and e.get("id") == qid:
                e["reason"] = r
                return

    STORE.update(_fn)


def public_hint(reason: str) -> str:
    """把阻塞原因归类成对外可说的粗粒度结论（公开队列页用，不暴露号池细节）。

    优先级按「真正卡住它的东西」来：账号在冷却/限流 → 说限流冷却，账号并发满 →
    说账号繁忙；只有原因里**只剩**模型类问题时才说「该模型当前不可用」。
    （能进入排队的请求按定义就不是模型永久不可用 —— 那种会直接 404。）
    """
    r = str(reason or "")
    if not r:
        return "排队等待中"
    if any(k in r for k in ("封禁", "冷却", "RPM", "TPM", "日限", "上游RPM", "上游日限")):
        return "账号限流冷却中"
    if any(k in r for k in ("账户并发", "渠道并发")):
        return "账号繁忙"
    if any(k in r for k in ("渠道模型", "原名禁用", "模型不存在")):
        return "该模型当前不可用"
    return "等待可用账号"


def remove(qid: str) -> None:
    def _fn(db: dict):
        db["queue"] = [e for e in db.get("queue", []) if isinstance(e, dict) and e.get("id") != qid]

    STORE.update(_fn)


def clear() -> None:
    def _fn(db: dict):
        db["queue"] = []

    STORE.update(_fn)


def stats(public: bool = False) -> dict:
    """队列状态。public=True 供 /queue 公开页用：不带来源 IP（隐私）。"""
    db = STORE.load()
    max_wait = _cfg_max_wait()
    cutoff = time.time() - max_wait * 2
    now = time.time()
    rows = []
    for e in sorted(list(db.get("queue") or []), key=lambda e: (e.get("t", 0), e.get("id", ""))):
        if not isinstance(e, dict) or e.get("t", 0) < cutoff:
            continue
        row = {
            "wait": max(0, round(now - e["t"], 1)),
            "ep": e.get("ep", ""),
            "model": e.get("model", ""),
        }
        if public:
            # 公开页只给粗粒度结论，不暴露「多少个账号在冷却」这类号池细节
            row["hint"] = public_hint(e.get("reason", ""))
        else:
            row["ip"] = e.get("ip", "-")  # IP 只给后台，公开页不需要、也不该泄漏
            row["tok"] = e.get("tok", "")  # 令牌遮罩同样仅后台
            row["reason"] = e.get("reason", "")
        rows.append(row)
    return {
        "length": len(rows),
        "max_wait": max_wait,
        "rows": rows,
    }
