# -*- coding: utf-8 -*-
"""静态检查：用 pyflakes 扫全项目，专防「名字被弄丢 / 导入缺失」这类只在运行时炸的缺陷。

本项目已经踩过四次同类问题（都是先上线后暴露）：
  - core.util 的 str_cut 忘 import → 拦截日志 NameError
  - admin_api 忘 import os → 拦截规则接口 500
  - `import shutil as _sh` 之后却调 `shutil.rmtree` → 远程更新每次都 NameError
  - _proxy 改动时把 max_attempts 的赋值行弄丢 → 每个请求 UnboundLocalError
它们都能被静态抓到，所以这里固化成一道自动门禁。

脚本自带 canary 自检：先拿一段「已知有错」的代码试一次，确认检查器真的在报错 ——
否则「检查器哑掉但显示通过」比不检查更危险（本项目也踩过：断言被 200 门禁页掩盖）。

用法：python tests/static_check.py
依赖：pyflakes（见 requirements-dev.txt）
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = [
    "server.py",
    "admin_api.py",
    "core/store.py",
    "core/pool.py",
    "core/upstreams.py",
    "core/convert.py",
    "core/streams.py",
    "core/queue.py",
    "core/util.py",
    "tests/regression.py",
    "tests/compat.py",
    "tests/predeploy_check.py",
    "tests/update_e2e.py",
    "tests/static_check.py",
]

CANARY = '''import os
import shutil as _sh


def f():
    return shutil.rmtree("x")


def g(n):
    total = 0
    for i in range(missing_counter):
        total += i
    return total + _sh
'''


def run_pyflakes(paths: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(
            [sys.executable, "-m", "pyflakes", *paths],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except Exception as e:
        print("pyflakes 调用失败：%s: %s" % (type(e).__name__, e))
        return 2, ""
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main() -> int:
    print("=" * 62)
    # 1) canary：确认检查器在工作（别名误用 + 未定义名 + 未使用导入都应被抓到）
    fd, tmp = tempfile.mkstemp(suffix=".py", text=True)
    os.close(fd)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(CANARY)
    try:
        _code, canary_out = run_pyflakes([tmp])
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    if "No module named pyflakes" in canary_out or canary_out.strip() == "":
        print("!! 静态检查未执行：需要 pyflakes（pip install -r requirements-dev.txt）")
        print(canary_out.strip()[:200])
        return 1
    hits = canary_out.count("undefined name")
    if hits < 2:
        print("!! 检查器自检失败：canary 里 2 个未定义名只抓到 %d 个，检查结果不可信" % hits)
        print(canary_out.strip()[:300])
        return 1
    print("canary 自检通过（别名误用/未定义名都能抓到）")

    # 2) 真扫项目
    missing = [f for f in FILES if not os.path.isfile(os.path.join(ROOT, f))]
    if missing:
        print("!! 待检文件缺失：%s" % missing)
        return 1
    code, out = run_pyflakes(FILES)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    print("-" * 62)
    if not lines:
        print("静态检查通过：0 条问题（%d 个文件）" % len(FILES))
        return 0
    for ln in lines:
        print("  " + ln)
    print("-" * 62)
    print("静态检查发现 %d 条问题（exit=%d）" % (len(lines), code))
    return 1


if __name__ == "__main__":
    sys.exit(main())
