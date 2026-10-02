# -*- coding: utf-8 -*-
"""部署前预检:拉取更新源(main tarball),演练一次完整覆盖流程,并做兼容性体检。

用途:改完代码、准备更新线上(或让别人部署)之前先跑一遍,确认
  1) 更新源能拉到、是合法 gzip/tar;
  2) 会覆盖哪些文件、哪些目录(数据/测试/备份)绝不会被碰;
  3) 下载到的源码在目标 Python 版本下语法可用(线上是 3.8,本机往往更新);
  4) 没有明显的高版本运行时 API 依赖。

用法:
  python tests/predeploy_check.py
  python tests/predeploy_check.py --url <tarball 地址>
退出码非 0 表示预检不通过。
"""
from __future__ import annotations

import argparse
import ast
import io
import os
import re
import shutil
import sys
import tarfile
import urllib.request

DEFAULT_URL = (
    "https://codeload.github.com/Cheerimy-Studio/NIM-Python-Gateway/tar.gz/refs/heads/main"
)
# 更新时一律不动的目录(与 server.py 的 _UPDATE_SKIP_DIRS 保持一致)
SKIP_DIRS = ("data", "tests", "backup", "_update_tmp", "__pycache__", ".git", ".github")
# 目标解释器版本(线上是 3.8)
TARGET_PY = (3, 8)
# 3.9+ 才有的运行时 API(出现在源码里会在老解释器上运行期炸)
NEW_API = re.compile(
    r"removeprefix\(|removesuffix\(|functools\.cache\b|math\.lcm\(|asyncio\.to_thread|"
    r"zoneinfo|graphlib|itertools\.pairwise"
)

problems: list[str] = []


def fail(msg: str) -> None:
    problems.append(msg)


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "gateway-updater"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def walk_files(src: str) -> list[str]:
    """更新会覆盖的文件清单(相对路径)。"""
    out: list[str] = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            out.append(os.path.relpath(os.path.join(root, fn), src).replace("\\", "/"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=DEFAULT_URL, help="更新源 tarball 地址")
    ap.add_argument("--keep", action="store_true", help="保留临时目录以便查看")
    args = ap.parse_args()

    tmp = os.path.join(os.environ.get("TEMP", "/tmp"), "gw-predeploy")
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)

    print("=" * 62)
    print("更新源: %s" % args.url)
    try:
        data = fetch(args.url)
    except Exception as e:
        fail("拉取失败: %s: %s" % (type(e).__name__, e))
        print("\n".join("!! " + p for p in problems))
        return 1
    print("下载: %d 字节, magic=%s" % (len(data), data[:2].hex()))
    if not data.startswith(b"\x1f\x8b"):
        fail("下载内容不是 gzip(前 120 字节: %r)" % data[:120])

    src_root = os.path.join(tmp, "src")
    os.makedirs(src_root)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
            tf.extractall(src_root)
    except Exception as e:
        fail("解包失败: %s: %s" % (type(e).__name__, e))
        print("\n".join("!! " + p for p in problems))
        return 1
    entries = os.listdir(src_root)
    src = os.path.join(src_root, entries[0]) if entries else src_root
    print("顶层目录: %s" % os.path.basename(src))

    # ---- 覆盖范围演练(在临时 base 上真的复制一遍,验证目录保护) ----
    base = os.path.join(tmp, "base")
    os.makedirs(os.path.join(base, "data"))
    os.makedirs(os.path.join(base, "tests"))
    open(os.path.join(base, "data", "db.json"), "w", encoding="utf-8").write('{"real":1}')
    open(os.path.join(base, "tests", "t.py"), "w", encoding="utf-8").write("REAL = 1\n")
    copied = walk_files(src)
    for rel in copied:
        dst = os.path.join(base, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(os.path.join(src, rel), dst)
    keep_ok = (
        open(os.path.join(base, "data", "db.json"), encoding="utf-8").read() == '{"real":1}'
        and open(os.path.join(base, "tests", "t.py"), encoding="utf-8").read() == "REAL = 1\n"
    )
    print("将覆盖 %d 个文件: %s" % (len(copied), ", ".join(sorted(copied))))
    print("data/ 与 tests/ 未被覆盖: %s" % keep_ok)
    if not keep_ok:
        fail("目录保护失效:data/ 或 tests/ 被覆盖")
    for must in ("server.py", "admin_api.py", "core/store.py", "web/admin.html", "web/admin.js"):
        if must not in copied:
            fail("覆盖清单缺少关键文件: %s" % must)

    # ---- 目标解释器语法体检 ----
    py_files = [p for p in copied if p.endswith(".py")]
    bad_syntax = []
    api_hits = []
    for rel in py_files:
        text = open(os.path.join(src, rel), encoding="utf-8").read()
        try:
            ast.parse(text, filename=rel, feature_version=TARGET_PY)
        except SyntaxError as e:
            bad_syntax.append("%s: %s" % (rel, e.msg))
        if rel.startswith("tests/"):
            continue
        for i, ln in enumerate(text.split("\n"), 1):
            if NEW_API.search(ln):
                api_hits.append("%s:%d %s" % (rel, i, ln.strip()[:70]))
    print("Python %d.%d 语法不兼容: %d %s" % (TARGET_PY[0], TARGET_PY[1], len(bad_syntax), bad_syntax))
    print("3.9+ 运行时 API 命中: %d %s" % (len(api_hits), api_hits[:5]))
    if bad_syntax:
        fail("存在目标解释器无法解析的语法")
    if api_hits:
        fail("存在 3.9+ 运行时 API 依赖")

    # ---- 前端零注释纪律(随页面下发,不允许多余注释) ----
    for rel in copied:
        if not rel.startswith("web/"):
            continue
        text = open(os.path.join(src, rel), encoding="utf-8").read()
        if rel.endswith((".js", ".html")) and re.search(r"(^|\n)\s*(//|/\*)|<!--", text):
            fail("前端文件含注释(纪律:前端零注释): %s" % rel)

    if not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)
    print("-" * 62)
    if problems:
        print("预检不通过:")
        for p in problems:
            print("  !! " + p)
        return 1
    print("预检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
