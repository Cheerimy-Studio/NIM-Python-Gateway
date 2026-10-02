# -*- coding: utf-8 -*-
"""真实更新路径的端到端测试(会真的覆盖代码 + execv 重启)。

做法:把网关代码复制到临时目录,在那里跑一个网关实例,更新源指向本地
http.server 提供的假更新包 —— 覆盖只发生在临时副本上,绝不碰真实仓库。

验证:
  1) POST /api/update 调用方拿到 200 与说明(而不是连接被重置/502)
  2) 更新包内容真的覆盖上去了(标记文件出现)
  3) 旧代码被 execv 替换:进程号不变、期间有一段不可用、之后恢复正常
回归套件里跑不到这一段(那里用 dryrun,避免覆盖真实仓库),所以单独成脚本。

用法:python tests/update_e2e.py
"""
from __future__ import annotations

import http.server
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = os.path.join(os.environ.get("TEMP", "/tmp"), "gw-update-e2e")
ADMIN_PW = "e2e-update-pass"
UPDATE_TOKEN = "upd-e2e-test-token"
SKIP = ("data", "tests", "backup", "_update_tmp", "__pycache__", ".git", ".github", ".venv")

results: list[tuple[str, bool, str]] = []
gw_log_tail: list[str] = []


def add(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def kill_port(port: int) -> str:
    """清掉仍占用该端口的进程。

    Windows 上 os.execv 是「新进程 + 原进程退出」的模拟实现,重启后的网关会是
    孤儿进程(PID 变、不再是我们 Popen 的子进程),不主动收掉就会一直占着端口。
    """
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=20).stdout
            pids = set()
            for ln in out.splitlines():
                parts = ln.split()
                if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(":%d" % port) \
                        and parts[3].upper() == "LISTENING":
                    pids.add(parts[4])
            for pid in pids:
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=20)
            return "已清理 %s" % (",".join(sorted(pids)) or "无")
        out = subprocess.run(["lsof", "-ti", "tcp:%d" % port], capture_output=True, text=True, timeout=20).stdout
        pids = [p.strip() for p in out.split() if p.strip()]
        for pid in pids:
            subprocess.run(["kill", "-9", pid], capture_output=True, timeout=20)
        return "已清理 %s" % (",".join(pids) or "无")
    except Exception as e:
        return "清理跳过(%s)" % type(e).__name__


def wait_http(url: str, timeout: float = 60.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(url, timeout=2).status_code < 500:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def main() -> int:
    import tarfile

    shutil.rmtree(TMP, ignore_errors=True)
    base = os.path.join(TMP, "base")
    os.makedirs(base)
    # 1) 复制网关代码到临时 base(更新只会覆盖这个副本)
    for fn in ("server.py", "admin_api.py"):
        shutil.copy2(os.path.join(ROOT, fn), os.path.join(base, fn))
    for d in ("core", "web"):
        shutil.copytree(os.path.join(ROOT, d), os.path.join(base, d),
                        ignore=shutil.ignore_patterns("__pycache__"))
    os.makedirs(os.path.join(base, "data"))

    # 2) 造一个「新版本」更新包:就是当前副本 + 一个标记文件
    pkg_dir = os.path.join(TMP, "pkg", "pkg-main")
    os.makedirs(pkg_dir)
    for root, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for fn in files:
            rel = os.path.relpath(os.path.join(root, fn), base)
            dst = os.path.join(pkg_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(root, fn), dst)
    with open(os.path.join(pkg_dir, "UPDATE_MARKER.txt"), "w", encoding="utf-8") as f:
        f.write("deployed-by-e2e-test\n")
    tar_path = os.path.join(TMP, "upd.tar.gz")
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(pkg_dir, arcname="pkg-main")

    # 3) 本地 http 服务提供更新包
    serve_dir = TMP
    mock_port = free_port()

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):  # noqa: D102
            pass

    handler = lambda *a, **kw: Quiet(*a, directory=serve_dir, **kw)  # noqa: E731
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", mock_port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    # 4) 从临时 base 起网关(数据目录也在临时目录)
    gw_port = free_port()
    env = {
        **os.environ,
        "NGW_DATA_DIR": os.path.join(base, "data"),
        "NGW_ADMIN_PASSWORD": ADMIN_PW,
        "NGW_UPDATE_URL": "http://127.0.0.1:%d/upd.tar.gz" % mock_port,
        "PYTHONPATH": base,
        "http_proxy": "",
        "https_proxy": "",
        "HTTP_PROXY": "",
        "HTTPS_PROXY": "",
        "no_proxy": "*",
        "NO_PROXY": "*",
    }
    gw_out = open(os.path.join(TMP, "gw_out.log"), "w+", encoding="utf-8", errors="replace")
    gw_err = open(os.path.join(TMP, "gw_err.log"), "w+", encoding="utf-8", errors="replace")
    gw = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "server:app", "--port", str(gw_port)],
        cwd=base, env=env, stdout=gw_out, stderr=gw_err,
    )
    url = "http://127.0.0.1:%d" % gw_port
    try:
        up = wait_http(url + "/")
        add("临时网关启动", up, "%s" % url)
        if not up:
            return report()

        a = httpx.Client(base_url=url, timeout=60)
        r = a.post("/api/login", json={"username": "admin", "password": ADMIN_PW})
        add("登录临时网关", r.status_code == 200, "st=%s" % r.status_code)
        a.headers["X-CSRF"] = r.json().get("csrf", "")
        a.post("/api/settings", json={"config": {"update_enabled": True, "update_token": UPDATE_TOKEN}})

        t0 = time.time()
        # 5) 真触发更新:这一句在修复前拿不到响应(execv 抢在响应之前)
        try:
            r_up = httpx.post(url + "/api/update",
                              headers={"Authorization": "Bearer " + UPDATE_TOKEN}, timeout=60)
            st, note = r_up.status_code, str(r_up.json().get("note") or "")
        except Exception as e:
            st, note = -1, "%s: %s" % (type(e).__name__, str(e)[:60])
        add("调用方拿到 200 与说明(修复点:execv 不能抢在响应前)",
            st == 200 and "已覆盖" in note, "st=%s note=%s 用时%.1fs" % (st, note[:40], time.time() - t0))

        # 6) 覆盖是否真的发生
        marker = os.path.join(base, "UPDATE_MARKER.txt")
        t1 = time.time()
        while time.time() - t1 < 20 and not os.path.isfile(marker):
            time.sleep(0.3)
        add("更新包已覆盖到代码目录", os.path.isfile(marker),
            "标记文件出现=%s" % os.path.isfile(marker))

        # 7) execv 重启:等启动日志出现第二次,再等服务重新可用。
        #    判据刻意不用 PID:Windows 上 os.execv 会换成新进程(PID 变、且不再是
        #    本进程的子进程),POSIX 上才保持同 PID —— 「第二次启动」跨平台都成立。
        #    注意:更新响应返回后还有 delay(约 2s)才真正 execv,期间旧进程仍在服务,
        #    所以不能一看到端口可用就认为重启完成。
        err_path = os.path.join(TMP, "gw_err.log")
        logtxt = ""
        startups = 0
        t2 = time.time()
        while time.time() - t2 < 45:
            try:
                with open(err_path, encoding="utf-8", errors="replace") as f:
                    logtxt = f.read()
            except Exception:
                logtxt = ""
            startups = logtxt.count("Application startup complete")
            if startups >= 2:
                break
            time.sleep(0.5)
        back = False
        t3 = time.time()
        while time.time() - t3 < 30:
            try:
                if httpx.get(url + "/", timeout=2).status_code < 500:
                    back = True
                    break
            except Exception:
                pass
            time.sleep(0.4)
        add("execv 重启:日志出现第二次启动且服务恢复", back and startups >= 2,
            "服务可用=%s 启动次数=%d 等待=%.1fs" % (back, startups, time.time() - t2))
        add("更新前已备份代码(backup/code)", os.path.isdir(os.path.join(base, "backup", "code")),
            "backup/code 存在=%s" % os.path.isdir(os.path.join(base, "backup", "code")))
        if not back or startups < 2:
            for ln in [x for x in logtxt.split("\n") if x.strip()][-8:]:
                gw_log_tail.append("[stderr] " + ln[:150])
        # 8) 重启后仍是同一份代码在跑:再打一次业务接口
        try:
            rr = httpx.get(url + "/api/intercept", headers={"X-CSRF": a.headers.get("X-CSRF", "")}, timeout=10)
            add("重启后服务可正常响应", rr.status_code in (200, 401), "st=%s" % rr.status_code)
        except Exception as e:
            add("重启后服务可正常响应", False, type(e).__name__)
    finally:
        try:
            gw.terminate()
            gw.wait(timeout=10)
        except Exception:
            try:
                gw.kill()
            except Exception:
                pass
        httpd.shutdown()
        # 重启后的孤儿进程不受 Popen 管,按端口收掉
        add("收尾清理重启后的孤儿进程", True, kill_port(gw_port))
        for f in (gw_out, gw_err):
            try:
                f.close()
            except Exception:
                pass
        shutil.rmtree(TMP, ignore_errors=True)
    return report()


def report() -> int:
    ok_all = True
    print("=" * 62)
    for name, ok, detail in results:
        print("%s %s | %s" % ("PASS" if ok else "FAIL", name, detail))
        ok_all = ok_all and ok
    if gw_log_tail:
        print("-" * 62)
        print("重启后网关输出(尾部):")
        for ln in gw_log_tail:
            print("  " + ln)
    print("=" * 62)
    print("ALL PASS" if ok_all else "FAILURES")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
