#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作台体检。

把《工作台宪法》里的硬指标变成可以跑的检查：
  1. 根目录只放宪法/索引/路线图/场景/工具/体检
  2. 每个场景四件套齐全
  3. 项目信息.md <= 25KB，§0 当前交接 <= 4KB
  4. 文档密集目录必须有 _索引.md
  5. 每个场景都在 索引.md 里登记过
  6. 工具清单存在
  7. §0 的「更新：日期」不超过 7 天

用法：
    python 体检/体检.py            # 体检 + 写报告
    python 体检/体检.py --no-write  # 只看结果
退出码：0 = 全过（允许 WARN），1 = 有 FAIL。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

MB = 1024 * 1024
KB = 1024

MAIN_LIMIT = 25 * KB          # 项目信息.md 上限
HANDOFF_LIMIT = 4 * KB        # §0 当前交接 上限
INDEX_COUNT = 30              # 文档数红线
INDEX_SMALL_COUNT = 8         # 小目录：文档数下限
INDEX_SMALL_BYTES = 500 * KB  # 小目录：体积下限

EXEMPT_DIRS = {"归档", "备份", "_临时", "缓存", ".git", "报告", "node_modules", ".venv", "__pycache__"}
REQUIRED_FILES = ["身份.md", "定位卡.md", "项目信息.md", "会话档案.md"]
ROOT_ALLOWED = {"00-工作台宪法.md", "AGENTS.md", "README.md", "README.en.md", "LICENSE", ".gitignore",
                "索引.md", "路线图.md", "场景", "工具", "体检", "可选-界面", "提案", "当前场景.md", "备份", ".git"}

issues: list[tuple[str, str, str]] = []  # (级别, 位置, 说明)


def add(level: str, where: str, msg: str) -> None:
    issues.append((level, where, msg))


def section_size(text: str, marker: str) -> int:
    """取以 marker 开头的那一节（到下一个 § 或文末）的 UTF-8 字节数。"""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith(marker):
            start = i
            break
    if start is None:
        return -1
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].strip().startswith("§"):
            end = j
            break
    return len("\n".join(lines[start:end]).encode("utf-8"))


def human(n: int) -> str:
    if n >= MB:
        return f"{n / MB:.1f}MB"
    if n >= KB:
        return f"{n / KB:.1f}KB"
    return f"{n}B"


def check_handoff_fresh(d: Path, text: str) -> None:
    """§0 里要有一行「更新：YYYY-MM-DD」；超过 7 天视为收工没写交接。"""
    if d.name.startswith(("_", "示例-")):
        return  # 模板和示例不参与新鲜度检查
    m = re.search(r"更新[：:]\s*(\d{4}-\d{2}-\d{2})", text)
    if not m:
        add("WARN", f"场景/{d.name}/项目信息.md",
            "§0 没写「更新：YYYY-MM-DD」，看不出交接是不是新的")
        return
    try:
        day = datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        add("WARN", f"场景/{d.name}/项目信息.md", "§0 的更新日期格式不对")
        return
    age = (datetime.now().date() - day).days
    if age > 7:
        add("WARN", f"场景/{d.name}/项目信息.md",
            f"§0 上次更新 {age} 天前，收工交接可能没写")


def check_root(root: Path) -> None:
    for entry in root.iterdir():
        if entry.name in ROOT_ALLOWED or entry.name.startswith("."):
            continue
        add("FAIL", f"根目录/{entry.name}", "不在白名单；根目录只放宪法/索引/路线图/场景/工具/体检")


def check_scenarios(root: Path) -> list[str]:
    base = root / "场景"
    if not base.is_dir():
        add("FAIL", "场景/", "场景目录不存在")
        return []

    names = []
    for d in sorted(base.iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        names.append(d.name)
        for f in REQUIRED_FILES:
            if not (d / f).is_file():
                add("FAIL", f"场景/{d.name}/{f}", "四件套缺文件")

        main = d / "项目信息.md"
        if main.is_file():
            size = main.stat().st_size
            if size > MAIN_LIMIT:
                add("FAIL", f"场景/{d.name}/项目信息.md",
                    f"{human(size)} 超过 25KB 上限；旧轮次该剪到 归档/")
            text = main.read_text(encoding="utf-8", errors="replace")
            hs = section_size(text, "§0")
            if hs < 0:
                add("WARN", f"场景/{d.name}/项目信息.md", "没有 §0 当前交接")
            elif hs > HANDOFF_LIMIT:
                add("FAIL", f"场景/{d.name}/项目信息.md",
                    f"§0 当前交接 {human(hs)} 超过 4KB 上限")
            check_handoff_fresh(d, text)
    return names


def check_index_registration(root: Path, names: list[str]) -> None:
    idx = root / "索引.md"
    if not idx.is_file():
        add("FAIL", "索引.md", "索引文件不存在")
        return
    text = idx.read_text(encoding="utf-8", errors="replace")
    for n in names:
        if n not in text:
            add("WARN", "索引.md", f"场景「{n}」没有登记")


def check_dense_dirs(root: Path) -> int:
    checked = 0
    for cur, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in EXEMPT_DIRS and not d.startswith(".")]
        docs = [f for f in files if not f.startswith(".") and f != "_索引.md"]
        if not docs:
            continue
        checked += 1
        total = sum((Path(cur) / f).stat().st_size for f in docs)
        need = len(docs) >= INDEX_COUNT or (len(docs) >= INDEX_SMALL_COUNT and total >= INDEX_SMALL_BYTES)
        if need and not (Path(cur) / "_索引.md").is_file():
            rel = os.path.relpath(cur, root).replace(os.sep, "/")
            add("FAIL", f"{rel}/", f"{len(docs)} 份文档 / {human(total)}，缺 _索引.md")
    return checked


def check_tools(root: Path) -> None:
    if not (root / "工具" / "工具清单.md").is_file():
        add("WARN", "工具/工具清单.md", "工具清单不存在；装了东西没处登记")


def main() -> int:
    if os.name == "nt":
        try:  # 控制台默认 GBK，会把中文打成乱码
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    root = Path(__file__).resolve().parent.parent
    check_root(root)
    names = check_scenarios(root)
    check_index_registration(root, names)
    dirs = check_dense_dirs(root)
    check_tools(root)

    fails = [i for i in issues if i[0] == "FAIL"]
    warns = [i for i in issues if i[0] == "WARN"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    print(f"工作台体检  {now}")
    print(f"根目录：{root}")
    print(f"场景 {len(names)} 个，目录 {dirs} 个；问题：{len(fails)} 个 FAIL / {len(warns)} 个 WARN")
    for level, where, msg in issues:
        print(f"  [{level}] {where}  {msg}")
    if not issues:
        print("  全部达标。")

    if "--no-write" not in sys.argv:
        out_dir = root / "体检" / "报告"
        out_dir.mkdir(parents=True, exist_ok=True)
        report = out_dir / f"{datetime.now().strftime('%Y-%m-%d_%H%M')}.md"
        lines = [
            "# 工作台体检报告",
            "",
            f"- 时间：{now}",
            f"- 根目录：`{root}`",
            f"- 结论：**{'不通过' if fails else '通过'}**（FAIL {len(fails)} / WARN {len(warns)}）",
            f"- 场景：{'、'.join(names) if names else '（无）'}",
            "",
            "## 问题",
            "",
        ]
        if issues:
            lines += ["| 级别 | 位置 | 说明 |", "|---|---|---|"]
            lines += [f"| {lv} | `{w}` | {m} |" for lv, w, m in issues]
        else:
            lines.append("无。")
        lines += ["", "## 下次优先修", ""]
        if fails:
            lines += [f"{i}. {w}：{m}" for i, (_, w, m) in enumerate(fails, 1)]
        else:
            lines.append("没有 FAIL；WARN 可以慢慢处理。")
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"报告：{report}")

    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
