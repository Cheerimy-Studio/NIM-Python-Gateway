"""排队系统：FIFO 队列、位置追踪、统计、公开状态查询。"""

from __future__ import annotations

import os
import time

from .store import STORE

# 队列条目上限：防止大量请求堆积把 db 撑大、拖慢遍历。
# 超出时只保留最新的（等待中的请求不依赖条目存在，靠自身循环重试取号）。
QUEUE_MAX_ENTRIES = 500


def _cfg_max_wait() -> int:
    return max(5, int(STORE.load()["config"].get("queue_max_wait") or 30))


def add(ep: str, model: str, ip: str) -> str:
    """入队（FIFO），自动清理已崩溃请求的残留条目。"""
    qid = "q_" + os.urandom(6).hex()
    max_wait = _cfg_max_wait()

    def _fn(db: dict):
        cutoff = time.time() - max_wait * 2
        q = [e for e in list(db.get("queue") or []) if isinstance(e, dict) and e.get("t", 0) >= cutoff]
        q.append({"id": qid, "t": time.time(), "ip": ip, "ep": ep[:8], "model": model[:60]})
        if len(q) > QUEUE_MAX_ENTRIES:
            q = q[-QUEUE_MAX_ENTRIES:]  # 只保留最新，防无限增长
        db["queue"] = q

    STORE.update(_fn)
    return qid


def remove(qid: str) -> None:
    def _fn(db: dict):
        db["queue"] = [e for e in db.get("queue", []) if isinstance(e, dict) and e.get("id") != qid]

    STORE.update(_fn)


def clear() -> None:
    def _fn(db: dict):
        db["queue"] = []

    STORE.update(_fn)


def stats() -> dict:
    """公开队列状态（不含敏感信息）。"""
    db = STORE.load()
    max_wait = _cfg_max_wait()
    cutoff = time.time() - max_wait * 2
    now = time.time()
    rows = []
    for e in sorted(list(db.get("queue") or []), key=lambda e: (e.get("t", 0), e.get("id", ""))):
        if not isinstance(e, dict) or e.get("t", 0) < cutoff:
            continue
        rows.append(
            {
                "wait": max(0, round(now - e["t"], 1)),
                "ep": e.get("ep", ""),
                "model": e.get("model", ""),
                "ip": e.get("ip", "-"),
            }
        )
    return {
        "length": len(rows),
        "max_wait": max_wait,
        "rows": rows,
    }
