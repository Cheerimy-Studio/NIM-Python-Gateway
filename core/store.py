"""JSON 存储层：原子写、进程内锁、mtime 校验缓存、默认配置与迁移。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Any, Callable

# 数据目录：默认项目根/data，可用环境变量 NGW_DATA_DIR 覆盖（多实例/测试隔离用）
DATA_DIR = os.environ.get("NGW_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
)
DB_PATH = os.path.join(DATA_DIR, "db.json")

DEFAULT_CONFIG: dict[str, Any] = {
    # 调度与限额（渠道 0=继承此处）
    "rate_limit_per_minute": 20,
    "tpm_limit": 50000,
    "account_cooldown_ms": 500,
    "hourly_request_limit": 5,
    "acct_concurrency": 0,
    "total_concurrency": 0,
    "pool_rpm_cap": 0,
    "pool_daily_cap": 0,
    "daily_request_cap": 100,
    "daily_token_limit": 900000,
    "warmup_seconds": 300,
    "max_retries": 2,
    "retry_backoff_base_ms": 500,
    "retry_backoff_max_ms": 4000,
    "retry_min_wait_ms": 0,  # 失败重试最小等待（毫秒，0=不限制）
    "request_timeout": 120,
    "connect_timeout": 10,
    # 封禁与账号保护
    "ban_step_seconds": 5,
    "ban_max_seconds": 300,
    "hard_fail_ban_seconds": 600,
    "hard_fail_disable_count": 3,
    # 排队
    "queue_enabled": True,
    "queue_max_wait": 15,
    "queue_poll_ms": 400,
    # 模型限制
    "model_whitelist": "",
    "model_blacklist": "",
    # 隐私与兼容
    "hide_upstream_errors": True,
    "hide_mapped_names": True,
    "param_overrides": "",
    # 防风控与熔断
    "cool_429_seconds": 30,
    "cool_5xx_seconds": 30,
    "cool_timeout_seconds": 45,
    "cool_conn_seconds": 10,
    "breaker_enabled": True,
    "breaker_threshold": 3,
    "breaker_seconds": 60,
    "ttfb_timeout": 60,
    "sse_idle_timeout": 60,
    # 其他
    "upstream_base": "https://integrate.api.nvidia.com/v1",
    "log_enabled": True,
    "log_max": 200,
    "session_log_max": 100,
    "timezone": "Asia/Shanghai",
    "models_cache_ttl": 600,
    "verify_tls": True,
    "admin_username": "admin",
}

LEGACY_KEYS = ("cooldown_seconds", "auto_disable_threshold")


def session_cookie(cfg: dict) -> str:
    secret = str(cfg.get("session_secret") or "")
    return hmac.new(secret.encode(), b"admin", hashlib.sha256).hexdigest()


def csrf_token(cfg: dict) -> str:
    secret = str(cfg.get("session_secret") or "")
    return hmac.new(secret.encode(), b"csrf", hashlib.sha256).hexdigest()


def _hash_password(pw: str) -> str:
    import base64

    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 120_000)
    return "pbkdf2$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(pw: str, stored: str) -> bool:
    import base64

    try:
        _, salt_b64, dk_b64 = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt_b64), 120_000)
        return hmac.compare_digest(dk, base64.b64decode(dk_b64))
    except Exception:
        return False


class Store:
    """单例存储：asyncio + threading 双锁，mtime 校验缓存，原子写。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._aloop_lock: asyncio.Lock | None = None
        self._memo: tuple[int, int, dict] | None = None
        self._memo_at = 0.0  # memo 建立时的 monotonic 时间
        self._dirty = False  # 有未落盘的变更
        os.makedirs(DATA_DIR, exist_ok=True)
        guard = os.path.join(DATA_DIR, ".htaccess")
        if not os.path.isfile(guard):
            with open(guard, "w") as f:
                f.write("Require all denied\n")

    @property
    def _alock(self) -> asyncio.Lock:
        if self._aloop_lock is None:
            self._aloop_lock = asyncio.Lock()
        return self._aloop_lock

    # ---------- 读取 ----------

    def _read_file(self) -> dict:
        try:
            with open(DB_PATH, "rb") as f:
                raw = f.read()
        except OSError:
            raw = b""
        db = None
        if raw:
            try:
                db = json.loads(raw)
            except Exception:
                try:
                    with open(DB_PATH + f".corrupt-{int(time.time())}", "wb") as f:
                        f.write(raw)
                except OSError:
                    pass
        if not isinstance(db, dict):
            db = {}
        return db

    def load(self) -> dict:
        with self._lock:
            # 短期缓存：memo 建立后 50ms 内直接返回，跳过 os.stat（高并发下大幅减少系统调用）
            if self._memo is not None and (time.monotonic() - self._memo_at) < 0.05:
                return self._memo[2]
            try:
                st = os.stat(DB_PATH)
                mtime, size = int(st.st_mtime), st.st_size
            except OSError:
                mtime, size = 0, 0
            if self._memo and self._memo[0] == mtime and self._memo[1] == size:
                self._memo_at = time.monotonic()
                return self._memo[2]
            raw_db = self._read_file()
            before = json.dumps(raw_db, sort_keys=True)
            db = dict(raw_db)
            self._migrate(db)
            # 启动期迁移（首次建库 / 版本升级）立即落盘：session_secret、访问令牌等
            # 随机值必须与 update() 读到的完全一致，否则首次写入会重新随机生成，
            # 导致已登录会话与已签发令牌全部失效
            if json.dumps(db, sort_keys=True) != before:
                self._write(db)
                try:
                    st = os.stat(DB_PATH)
                    mtime, size = int(st.st_mtime), st.st_size
                except OSError:
                    pass
            self._memo = (mtime, size, db)
            self._memo_at = time.monotonic()
            return db

    def _migrate(self, db: dict) -> None:
        base = {
            "upstreams": [],
            "pool_buckets": {},
            "pool_daily": {},
            "up_recent": {},
            "model_breaker": {},
            "keys": [],
            "buckets": {},
            "stats": {},
            "logs": [],
            "queue": [],
            "models_cache": None,
            "sessions": [],
            "channel_presets": {},
        }
        for k, v in base.items():
            db.setdefault(k, v)
        cfg = db.setdefault("config", {})
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
        for legacy in LEGACY_KEYS:
            cfg.pop(legacy, None)
        if not isinstance(db.get("logs"), list):
            db["logs"] = []
        if not isinstance(db.get("keys"), list):
            db["keys"] = []
        if not isinstance(db.get("queue"), list):
            db["queue"] = []
        for k, v in {
            "buckets": {},
            "pool_buckets": {},
            "pool_daily": {},
            "up_recent": {},
            "model_breaker": {},
        }.items():
            if not isinstance(db.get(k), dict):
                db[k] = {}
        if not db.get("channel_presets"):
            db["channel_presets"] = {
                "nvidia_nim": {
                    "name": "NVIDIA NIM",
                    "rpm": 25,
                    "tpm": 50000,
                    "account_cooldown_ms": 500,
                    "max_retries": 2,
                    "retry_backoff_base_ms": 500,
                    "retry_backoff_max_ms": 4000,
                    "ban_step_seconds": 5,
                    "ban_max_seconds": 300,
                    "hard_fail_ban_seconds": 600,
                    "hard_fail_disable_count": 3,
                    "daily_request_cap": -1,
                    "daily_token_limit": -1,
                    "hourly_request_limit": 5,
                    "request_timeout": 120,
                    "connect_timeout": 10,
                },
                "openai_compat": {
                    "name": "OpenAI 兼容",
                    "rpm": -1,
                    "tpm": -1,
                    "account_cooldown_ms": 200,
                    "max_retries": 2,
                    "retry_backoff_base_ms": 500,
                    "retry_backoff_max_ms": 5000,
                    "ban_step_seconds": 10,
                    "ban_max_seconds": 600,
                    "hard_fail_ban_seconds": 300,
                    "hard_fail_disable_count": 5,
                    "daily_request_cap": -1,
                    "daily_token_limit": -1,
                    "hourly_request_limit": -1,
                    "request_timeout": 120,
                    "connect_timeout": 5,
                },
            }
        if "session_secret" not in cfg or not cfg["session_secret"]:
            cfg["session_secret"] = os.urandom(24).hex()
        if not str(cfg.get("admin_username") or ""):
            cfg["admin_username"] = "admin"
        h = str(cfg.get("admin_password_hash") or "")
        if not h or not h.startswith("pbkdf2$"):
            # 首次初始化（或检测到不兼容的旧哈希）：随机生成初始密码并打印一次，
            # 绝不预置固定密码 —— 否则所有部署共用同一个默认口令。
            pw = os.environ.get("NGW_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
            cfg["admin_password_hash"] = _hash_password(pw)
            print(
                "\n"
                + "=" * 62
                + f"\n  首次初始化管理员账号\n    用户名: {cfg['admin_username']}\n    密  码: {pw}\n"
                + "  请立即登录后台修改密码。\n"
                + "=" * 62,
                flush=True,
            )
        gt = cfg.get("gateway_tokens")
        struct: list = []
        for t in gt or []:
            if isinstance(t, str) and t:
                struct.append({"t": t, "m": []})
            elif isinstance(t, dict) and t.get("t"):
                struct.append({"t": str(t["t"]), "m": [str(x) for x in t.get("m", [])]})
        if not struct:
            struct = [{"t": "sk-gw-" + os.urandom(12).hex(), "m": []}]
        cfg["gateway_tokens"] = struct

    # ---------- 写入 ----------

    def _write(self, db: dict) -> None:
        tmp = DB_PATH + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, DB_PATH)

    def update(self, fn: Callable[[dict], Any]) -> dict:
        """加锁读-改-写；memo 未失效时直接复用内存态，避免每次全量读盘+序列化对比。"""
        with self._lock:
            try:
                st = os.stat(DB_PATH)
                mtime, size = int(st.st_mtime), st.st_size
            except OSError:
                mtime, size = 0, 0
            if self._memo and self._memo[0] == mtime and self._memo[1] == size:
                # 复用已迁移的内存态；fn 改内存，写盘由后台定期落盘统一处理
                db = self._memo[2]
                fn(db)
                self._dirty = True
                return db
            # memo 失效（外部改动/首次）：读盘 + 迁移
            raw_db = self._read_file()
            before = json.dumps(raw_db, sort_keys=True)
            db = dict(raw_db)
            self._migrate(db)
            fn(db)
            after = json.dumps(db, sort_keys=True)
            if after != before:
                self._write(db)
                self._dirty = False
                try:
                    st2 = os.stat(DB_PATH)
                    self._memo = (int(st2.st_mtime), st2.st_size, db)
                    self._memo_at = time.monotonic()
                except OSError:
                    pass
            return db

    def flush(self) -> None:
        """把内存态写盘（后台定期调用）。高并发下合并多次变更为一次磁盘写。"""
        with self._lock:
            if not self._dirty or self._memo is None:
                return
            self._write(self._memo[2])
            self._dirty = False
            try:
                st = os.stat(DB_PATH)
                self._memo = (int(st.st_mtime), st.st_size, self._memo[2])
                self._memo_at = time.monotonic()
            except OSError:
                pass

    # ---------- 异步便捷封装 ----------

    async def aupdate(self, fn: Callable[[dict], Any]) -> Any:
        """异步更新：兼容 Python 3.8（用 run_in_executor 替代 to_thread）。"""
        async with self._alock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self.update, fn)


STORE = Store()
