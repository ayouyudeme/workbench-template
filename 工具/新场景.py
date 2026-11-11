#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""新建一个场景：复制 _模板、建 归档/备份、登记到 索引.md。

用法：
    python 工具/新场景.py 公众号写作
    python 工具/新场景.py 公众号写作 --slug wechat-writing --desc "技术向公众号"

只会在 场景\\<名字>\\ 下动手，不碰别的场景。
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "场景" / "_模板"
INDEX = ROOT / "索引.md"


def die(msg: str) -> int:
    print(f"× {msg}")
    return 1


def main() -> int:
    if sys.platform == "win32":
        try:  # 控制台默认 GBK，会把中文打成乱码
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("name", help="场景名，短名，例：公众号写作")
    ap.add_argument("--slug", default="", help="英文代号，例：wechat-writing（写进 §六 身份）")
    ap.add_argument("--desc", default="", help="一句话说明，写进索引表")
    args = ap.parse_args()

    name = args.name.strip()
    if not name or name in {"_模板"}:
        return die("场景名不能为空，也不能叫 _模板")
    if re.search(r'[\\/:*?"<>|]', name):
        return die('场景名不能带 \\ / : * ? " < > |')
    if name.startswith((".", "_")):
        return die("场景名不要以 . 或 _ 开头")

    target = ROOT / "场景" / name
    if target.exists():
        return die(f"场景「{name}」已存在：{target}")

    shutil.copytree(TPL, target)
    (target / "归档").mkdir(exist_ok=True)
    (target / "备份").mkdir(exist_ok=True)

    # 把占位符换成真名
    main_md = target / "项目信息.md"
    text = main_md.read_text(encoding="utf-8")
    text = text.replace("<场景名>", name).replace("S0X", "S01")
    text = text.replace("更新：YYYY-MM-DD", f"更新：{date.today():%Y-%m-%d}")
    if args.slug:
        text = text.replace("`<slug>`", f"`{args.slug}`")
    main_md.write_text(text, encoding="utf-8")
    card = target / "定位卡.md"
    card.write_text(card.read_text(encoding="utf-8").replace("<场景名>", name), encoding="utf-8")
    arch = target / "会话档案.md"
    arch.write_text(arch.read_text(encoding="utf-8").replace("<场景名>", name), encoding="utf-8")

    # 登记到索引
    if INDEX.is_file():
        idx = INDEX.read_text(encoding="utf-8")
        row = f"| `{name}` | {args.desc or '（待填一句话）'} | 在用 | `场景\\{name}\\` |"
        anchor = "| `_模板` | 新建场景时复制它 | 模板 | `场景\\_模板\\` |"
        if anchor in idx:
            idx = idx.replace(anchor, anchor + "\n" + row)
        else:
            idx = idx.rstrip() + "\n" + row + "\n"
        idx = re.sub(r"更新：\d{4}-\d{2}-\d{2}", f"更新：{date.today():%Y-%m-%d}", idx, count=1)
        INDEX.write_text(idx, encoding="utf-8")

    print(f"√ 场景已建：{target}")
    print("  四件套：身份.md / 定位卡.md / 项目信息.md / 会话档案.md")
    print("  已登记到 索引.md")
    print("  下一步：改 身份.md 和 定位卡.md，然后跑 体检/体检.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
