#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""工作台 · 场景视图 + 场景编辑。

起一个只在本机跑的网页：
  · 左边看场景（身份卡、定位卡、§0 交接、记忆体积、体检）
  · 最上面「场景编辑」：填表生成/修改身份卡，可设为当前场景并生效到 DSH
  · 改动申请：改卡之前先提申请，由人点同意/驳回

用法：
    python 界面\\server.py
    python 界面\\server.py --no-browser
    python 界面\\server.py --build      # 生成离线快照
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCENES = ROOT / "场景"
TPL = SCENES / "_模板"
HEALTH_PY = ROOT / "体检" / "体检.py"
PROPOSALS = ROOT / "提案"
PENDING = PROPOSALS / "待处理"
DONE = PROPOSALS / "已处理"
ACTIVE_MD = ROOT / "当前场景.md"
RULES_MD = ROOT / "工具" / "铁律库.md"
_persona_env = os.environ.get("WB_PERSONA", "")
PERSONA = Path(_persona_env) if _persona_env else (
    Path(os.path.expanduser("~")) / ".dsh" / "agents" / "projects" / "persona.md"
)

MAIN_LIMIT = 25 * 1024
HANDOFF_LIMIT = 4 * 1024
CARD_LIMIT = 2048

WB_BEGIN = "<!-- WB:BEGIN 工作台自动生成，勿手改这一段 -->"
WB_END = "<!-- WB:END -->"
BLANK_NAME = "空白"

TEMPLATES = {
    "导演型": {
        "serve": "我，负责出片的人",
        "goal": "把内容做成能直接发的片子",
        "role": "视频导演",
        "deliver": "带音效的成片；时长由内容定",
        "rules": ["不自动试跑", "只交付成品", "不确定就停下来问"],
        "tone": "说人话，不堆术语",
        "read": ["定位卡.md", "项目信息.md 的 §0"],
    },
    "运维型": {
        "serve": "我，管这台机器的人",
        "goal": "环境能稳定跑，出问题能查",
        "role": "系统运维",
        "deliver": "可运行的环境 + 每条变更一行记录",
        "rules": ["改前先备份", "不碰生产库", "每条变更留记录"],
        "tone": "先给结论，再给命令",
        "read": ["定位卡.md", "项目信息.md 的 §0", "_索引.md"],
    },
    "研究型": {
        "serve": "我，要拿结论去做事的人",
        "goal": "把一堆资料收敛成能用的结论",
        "role": "资料助理",
        "deliver": "带出处的结论；没出处的地方标存疑",
        "rules": ["没有出处不写", "不确定就标存疑", "先结论后过程"],
        "tone": "克制，不用形容词撑场面",
        "read": ["定位卡.md", "项目信息.md 的 §0", "目录的 _索引.md"],
    },
    "空白": {
        "serve": "", "goal": "", "role": "", "deliver": "",
        "rules": [], "tone": "", "read": ["定位卡.md", "项目信息.md 的 §0"],
    },
}

FLUFF = ["专业", "高效", "严谨", "认真", "负责", "优秀", "资深", "全能", "聪明", "贴心"]


# ---------- 解析 ----------

def _sections(text: str, prefix: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    cur = None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(prefix):
            cur = s.lstrip(prefix).strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line.rstrip())
    return out


def parse_identity(p: Path) -> dict:
    data = {"serve": "", "goal": "", "role": "", "deliver": "", "rules": [], "tone": "", "read": []}
    if not p.is_file():
        return data
    text = p.read_text(encoding="utf-8", errors="replace")
    for title, lines in _sections(text, "##").items():
        body = [l.strip() for l in lines if l.strip()]
        if "服务对象" in title:
            joined = " ".join(body)
            m = re.search(r"这次要[：:]\s*(.+)$", joined)
            if m:
                data["goal"] = m.group(1).strip()
                data["serve"] = joined[: m.start()].strip(" 。")
            else:
                data["serve"] = joined
        elif "谁" in title or title == "身份":
            data["role"] = " ".join(body)
        elif "交付" in title:
            data["deliver"] = " ".join(body)
        elif "规矩" in title or "规则" in title or "铁律" in title:
            data["rules"] = [re.sub(r"^[-*•]\s*|^\d+[.、]\s*", "", l) for l in body]
        elif "口吻" in title or "语气" in title:
            data["tone"] = " ".join(body)
        elif "必读" in title:
            data["read"] = [re.sub(r"^[-*•]\s*|^\d+[.、]\s*", "", l) for l in body]
    for k in ("serve", "goal", "role", "deliver", "tone"):
        if _blankish(data[k]):
            data[k] = ""
    data["rules"] = [r for r in data["rules"] if not _blankish(r)]
    data["read"] = [r for r in data["read"] if not _blankish(r)]
    return data


def parse_card(p: Path) -> dict:
    out: dict[str, str] = {}
    if not p.is_file():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"^\s*[-*]\s*\*\*(.+?)\*\*\s*[：:]\s*(.*)$", line)
        if m:
            out[m.group(1).strip()] = m.group(2).strip()
    return out


def parse_info(p: Path) -> dict:
    info = {"handoff": "", "sections": [], "id": ""}
    if not p.is_file():
        return info
    chunks, cur = [], None
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("§"):
            cur = {"title": s, "lines": []}
            chunks.append(cur)
        elif cur is not None and s and not s.startswith("#"):
            cur["lines"].append(s)
    for c in chunks:
        if c["title"].startswith("§0"):
            info["handoff"] = " ".join(c["lines"])
        elif c["title"].startswith("§六"):
            raw = " ".join(c["lines"]).lstrip("-* ").strip()
            m = re.search(r"场景\s*(S\d+)", raw)
            info["id"] = m.group(1) if m else raw.split("·")[0].strip()
        c["lines"] = c["lines"][:8]
    info["sections"] = [c for c in chunks if not c["title"].startswith("§0")]
    return info


def read_active_raw() -> str:
    if ACTIVE_MD.is_file():
        m = re.search(r"当前场景[：:]\s*(.+)", ACTIVE_MD.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1).strip()
    return ""


def read_active() -> str:
    v = read_active_raw()
    return "" if v in ("（默认人格）", "(默认人格)", BLANK_NAME) else v


def read_previous() -> str:
    if ACTIVE_MD.is_file():
        m = re.search(r"上一个场景[：:]\s*(.+)", ACTIVE_MD.read_text(encoding="utf-8", errors="replace"))
        if m:
            v = m.group(1).strip()
            return "" if v in ("（无）", "(无)") else v
    return ""


def load_rules() -> list[dict]:
    """从 工具\\铁律库.md 读候选铁律：`## 组名` 分组，`- 条目` 一条。"""
    if not RULES_MD.is_file():
        return []
    groups, cur = [], {"group": "通用", "items": []}
    for line in RULES_MD.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("## "):
            if cur["items"]:
                groups.append(cur)
            cur = {"group": s[3:].strip(), "items": []}
        elif s.startswith("- ") and len(s) > 2:
            cur["items"].append(s[2:].strip())
    if cur["items"]:
        groups.append(cur)
    return groups


# ---------- 体检 ----------

def load_health_module():
    if not HEALTH_PY.is_file():
        return None
    spec = importlib.util.spec_from_file_location("wb_health", HEALTH_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def health_issues() -> list[dict]:
    mod = load_health_module()
    if not mod:
        return [{"level": "WARN", "where": "体检\\体检.py", "msg": "体检脚本不存在"}]
    mod.issues = []
    try:
        mod.check_root(ROOT)
        names = mod.check_scenarios(ROOT)
        mod.check_index_registration(ROOT, names)
        mod.check_dense_dirs(ROOT)
        mod.check_tools(ROOT)
        return [{"level": lv, "where": w, "msg": m} for lv, w, m in mod.issues]
    except Exception as e:
        return [{"level": "WARN", "where": "体检\\体检.py", "msg": f"体检跑不起来：{e}"}]


# ---------- 扫描 ----------

def scan() -> dict:
    scenarios = []
    if SCENES.is_dir():
        for d in sorted(SCENES.iterdir(), key=lambda x: x.name):
            if not d.is_dir() or d.name.startswith("."):
                continue
            if d.name.startswith("_") and d.name != "_模板":
                continue
            is_tpl = d.name == "_模板"
            files = {n: (d / n).is_file() for n in ("身份.md", "定位卡.md", "项目信息.md", "会话档案.md")}
            main_p = d / "项目信息.md"
            info = parse_info(main_p)
            size_main = main_p.stat().st_size if main_p.is_file() else 0
            hand = len(info["handoff"].encode("utf-8")) if info["handoff"] else 0
            identity = parse_identity(d / "身份.md")
            if is_tpl:
                identity["role"] = identity["role"] or "新建场景时复制它"
            scenarios.append({
                "name": d.name,
                "dir": str(d),
                "template": is_tpl,
                "identity": identity,
                "card": parse_card(d / "定位卡.md"),
                "info": {"handoff": info["handoff"], "sections": info["sections"]},
                "id": info["id"],
                "size": {"main": size_main, "handoff": hand, "card": (d / "身份.md").stat().st_size if (d / "身份.md").is_file() else 0},
                "limits": {"main": MAIN_LIMIT, "handoff": HANDOFF_LIMIT, "card": CARD_LIMIT},
                "files": files,
                "problem": (not all(files.values()) or size_main > MAIN_LIMIT or hand > HANDOFF_LIMIT) and not is_tpl,
                "updated": datetime.fromtimestamp(d.stat().st_mtime).strftime("%m-%d %H:%M"),
            })

    return {
        "generatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "root": str(ROOT),
        "active": read_active(),
        "blank": read_active_raw() == BLANK_NAME,
        "previous": read_previous(),
        "persona": {"path": str(PERSONA), "exists": PERSONA.is_file()},
        "scenarios": scenarios,
        "issues": health_issues(),
        "proposals": len(list(PENDING.glob("*.md"))) if PENDING.is_dir() else 0,
        "templates": list(TEMPLATES.keys()),
        "ruleLibrary": load_rules(),
    }


# ---------- 写卡片 ----------

def _yaml_safe(s: str) -> str:
    return (s or "").replace("\r", " ").strip()


PLACEHOLDERS = {
    "（未设定）", "（未设定：保持 DSH 原本的人格）", "（未写）", "（还没写）",
    "（待填）", "（待填路径）", "（无）", "（不设）", "（没写）", "（还没填）",
    "未设定", "未写", "待填", "无", "-", "—", "–",
}


def _blankish(s: str) -> bool:
    return _yaml_safe(s) in PLACEHOLDERS or not _yaml_safe(s)


def render_identity(f: dict) -> str:
    rules = [r.strip() for r in f.get("rules", []) if r.strip()][:5]
    read = [r.strip() for r in f.get("read", []) if r.strip()][:4]
    serve, goal = _yaml_safe(f.get("serve")), _yaml_safe(f.get("goal"))
    serve_line = serve + (f"。这次要：{goal}" if goal else "")
    lines = ["# 身份", "", "## 服务对象", "", serve_line or "（还没写：人是谁、这次要什么）", "",
             "## 我是谁", "", _yaml_safe(f.get("role")) or "（还没写：AI 在这个场景担什么角色）", "",
             "## 交付", "", _yaml_safe(f.get("deliver")) or "（还没写：交什么、什么算做完）", "",
             "## 规矩", ""]
    lines += [f"- {r}" for r in rules] or ["- （还没写：至少一条否决式铁律，如「不自动试跑」）"]
    lines += ["", "## 口吻", "", _yaml_safe(f.get("tone")) or "（还没写）", "", "## 必读", ""]
    lines += [f"- {r}" for r in read] or ["- （还没写：开工先读哪几个文件）"]
    return "\n".join(lines) + "\n"


def render_card(f: dict) -> str:
    rules = [r.strip() for r in f.get("rules", []) if r.strip()][:5]
    kv = [
        ("身份职责", _yaml_safe(f.get("role")) or "（待填）"),
        ("资产地图", _yaml_safe(f.get("assets")) or "（待填路径）"),
        ("铁律", " · ".join(rules) if rules else "（待填）"),
        ("上次进度", "（开工后由 AI 更新）"),
        ("协作对象", _yaml_safe(f.get("partner")) or "（无）"),
        ("触发词", _yaml_safe(f.get("trigger")) or "（待填）"),
    ]
    body = "\n".join(f"- **{k}**：{v}" for k, v in kv)
    return f"# 定位卡 · {_yaml_safe(f.get('name')) or '<场景名>'}\n\n> 开工先看这一页，一屏看完：我是谁、东西在哪、什么不能碰、上次做到哪。\n\n{body}\n"


def validate(f: dict) -> list[str]:
    w: list[str] = []
    role, deliver = _yaml_safe(f.get("role")), _yaml_safe(f.get("deliver"))
    goal = _yaml_safe(f.get("goal"))
    rules = [r.strip() for r in f.get("rules", []) if r.strip()]
    read = [r.strip() for r in f.get("read", []) if r.strip()]
    if not role:
        w.append("「我是谁」还空着——AI 不知道自己要扮演谁")
    if any(x in role for x in FLUFF):
        w.append("角色里出现了「专业/高效」这类词，对 AI 没有约束力，换成「做什么、不做什么」")
    if not rules:
        w.append("一条铁律都没有。至少给一条否决式的，如「不自动试跑」")
    elif not any(re.match(r"^(不|必须|禁止|只|先|所有|每)", r) for r in rules):
        w.append("铁律读起来像口号。写成能判断违没违的句子：不 XXX / 必须 XXX")
    if not deliver:
        w.append("「交付」空着——AI 不知道什么算做完")
    if goal and len(goal) > 50:
        w.append(f"目标 {len(goal)} 字，超过 50 字就砍成一句话")
    if not read:
        w.append("「必读」空着——AI 开工要先读哪几个文件，写清楚才不瞎翻")
    card = render_identity(f) + render_card(f)
    if len(card.encode("utf-8")) > CARD_LIMIT:
        w.append(f"卡片 {len(card.encode('utf-8'))} 字节，超过 {CARD_LIMIT}，AI 开场读它会抓不住重点")
    return w


def backup(path: Path, tag: str) -> str | None:
    if not path.is_file():
        return None
    bdir = path.parent / "备份"
    bdir.mkdir(exist_ok=True)
    dst = bdir / f"{tag}-{datetime.now():%Y%m%d-%H%M%S}.md"
    shutil.copy2(path, dst)
    return str(dst)


def log_version(scene_dir: Path, text: str) -> None:
    bdir = scene_dir / "备份"
    bdir.mkdir(exist_ok=True)
    with (bdir / "版本记录.md").open("a", encoding="utf-8") as fh:
        fh.write(f"- {datetime.now():%Y-%m-%d %H:%M} {text}\n")


def register_index(name: str, desc: str) -> None:
    idx = ROOT / "索引.md"
    if not idx.is_file():
        return
    text = idx.read_text(encoding="utf-8")
    if f"`{name}`" in text:
        return
    row = f"| `{name}` | {desc or '（待填一句话）'} | 在用 | `场景\\{name}\\` |"
    anchor = "| `_模板` | 新建场景时复制它 | 模板 | `场景\\_模板\\` |"
    text = text.replace(anchor, anchor + "\n" + row) if anchor in text else text.rstrip() + "\n" + row + "\n"
    text = re.sub(r"更新：\d{4}-\d{2}-\d{2}", f"更新：{datetime.now():%Y-%m-%d}", text, count=1)
    idx.write_text(text, encoding="utf-8")


def create_scene(name: str, desc: str = "") -> Path:
    target = SCENES / name
    if target.exists():
        raise ValueError("场景已存在")
    shutil.copytree(TPL, target)
    for sub in ("归档", "备份"):
        (target / sub).mkdir(exist_ok=True)
    existing = [p for p in SCENES.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))]
    sid = f"S{len(existing) + 1:02d}"
    for fn, old in (("项目信息.md", "<场景名>"), ("定位卡.md", "<场景名>"), ("会话档案.md", "<场景名>")):
        p = target / fn
        if p.is_file():
            p.write_text(p.read_text(encoding="utf-8").replace(old, name).replace("S0X", sid), encoding="utf-8")
    register_index(name, desc)
    return target


def save_scene(payload: dict) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name or re.search(r'[\\/:*?"<>|]', name) or name.startswith((".", "_")):
        return {"ok": False, "msg": "场景名不合法：不能带 \\ / : * ? \" < > |，也不能以 . 或 _ 开头"}
    create = bool(payload.get("create"))
    fields = payload.get("fields") or {}
    fields["name"] = name

    target = SCENES / name
    if create:
        if name == BLANK_NAME:
            return {"ok": False, "msg": "「空白」是内置卡，不能新建或覆盖"}
        if target.exists():
            return {"ok": False, "msg": "场景已存在，改成打开它"}
        target = create_scene(name, _yaml_safe(fields.get("goal"))[:40])
    elif not target.is_dir():
        return {"ok": False, "msg": "没有这个场景"}

    warns = validate(fields)
    if create:
        b1 = b2 = None
    else:
        b1 = backup(target / "身份.md", "身份")
        b2 = backup(target / "定位卡.md", "定位卡")
    (target / "身份.md").write_text(render_identity(fields), encoding="utf-8")
    (target / "定位卡.md").write_text(render_card(fields), encoding="utf-8")
    log_version(target, f"保存卡片（{'新建' if create else '修改'}）")
    refreshed = {"persona": "skipped", "msg": ""}
    if not create and read_active() == name:
        refreshed = refresh_persona(name)
    return {"ok": True, "warnings": warns, "backups": [b for b in (b1, b2) if b], "name": name, "activated": refreshed}


# ---------- 生效到 DSH ----------

def persona_block(name: str, ident: dict, card: dict) -> str:
    """只写填了的内容：没填的口吻/资产/触发词整行不出现，避免每轮被占位话占嘴。"""
    rules = "；".join(ident["rules"]) or "（未写）"
    read = "、".join(ident["read"]) or "定位卡.md、项目信息.md §0"
    lines = [WB_BEGIN, f"## 当前场景：{name}", ""]
    lines.append(f"- 你是：{ident['role'] or '（未设定）'}")
    if ident["serve"]:
        tail = f"；这次要：{ident['goal']}" if ident.get("goal") else ""
        lines.append(f"- 服务对象：{ident['serve']}{tail}")
    if ident["deliver"]:
        lines.append(f"- 交付：{ident['deliver']}")
    lines.append(f"- 铁律：{rules}")
    if ident["tone"]:
        lines.append(f"- 口吻：{ident['tone']}")
    lines.append(f"- 开工先读：{read}")
    if not _blankish(card.get("资产地图", "")):
        lines.append(f"- 资产地图：{_yaml_safe(card['资产地图'])}")
    if not _blankish(card.get("触发词", "")):
        lines.append(f"- 触发词：{_yaml_safe(card['触发词'])}")
    lines += [
        "",
        "改卡入口：工作台\\界面\\start.bat → 场景编辑。这段由工作台维护，手改会被覆盖。",
        WB_END,
    ]
    return "\n".join(lines)


def activate(name: str) -> dict:
    if name == BLANK_NAME:
        return deactivate(blank=True)
    d = SCENES / name
    if not d.is_dir():
        return {"ok": False, "msg": "没有这个场景"}
    ident = parse_identity(d / "身份.md")
    card = parse_card(d / "定位卡.md")
    block = persona_block(name, ident, card)

    prev = read_active()
    ACTIVE_MD.write_text(
        f"当前场景：{name}\n更新时间：{datetime.now():%Y-%m-%d %H:%M}\n卡片：场景\\{name}\\身份.md\n上一个场景：{prev if prev and prev != name else '（无）'}\n",
        encoding="utf-8",
    )

    result = {"ok": True, "name": name}
    result.update(write_persona(block))
    return result


def refresh_persona(name: str) -> dict:
    """卡片被改动后，如果它正是当前生效的场景，立刻把新卡片刷进人格文件。"""
    d = SCENES / name
    if not d.is_dir():
        return {"persona": "skipped", "msg": ""}
    block = persona_block(name, parse_identity(d / "身份.md"), parse_card(d / "定位卡.md"))
    try:
        old = PERSONA.read_text(encoding="utf-8") if PERSONA.is_file() else ""
    except Exception:
        old = ""
    if WB_BEGIN not in old:
        return {"persona": "skipped", "msg": "这个场景没在生效，不用刷"}
    return write_persona(block)


def write_persona(block: str) -> dict:
    if not PERSONA.parent.is_dir():
        return {"persona": "no-dir", "msg": "没找到 DSH 项目人格目录"}
    try:
        old = PERSONA.read_text(encoding="utf-8") if PERSONA.is_file() else ""
    except Exception as e:
        return {"persona": "error", "msg": f"读不了人格文件：{e}"}
    if PERSONA.is_file():
        bdir = ROOT / "备份"
        bdir.mkdir(exist_ok=True)
        shutil.copy2(PERSONA, bdir / f"dsh-persona-{datetime.now():%Y%m%d-%H%M%S}.md")
    if WB_BEGIN in old and WB_END in old:
        new = re.sub(re.escape(WB_BEGIN) + r".*?" + re.escape(WB_END), lambda _: block, old, flags=re.S)
    else:
        new = (old.rstrip() + "\n\n" + block + "\n") if old.strip() else block + "\n"
    try:
        PERSONA.write_text(new, encoding="utf-8")
        return {"persona": "written", "msg": "已刷新 DSH 人格"}
    except Exception as e:
        return {"persona": "error", "msg": f"人格文件写不进去（{e}）"}


def deactivate(blank: bool = False) -> dict:
    """回到 DSH 原生人格：删掉 WB 块，卡片文件原样保留，随时可再开。
    blank=True 表示「切回空白卡」，效果一样，只把记录写成 空白。"""
    prev = read_active()
    label = BLANK_NAME if blank else "（默认人格）"
    note = "空白卡：不注入任何角色，保持 DSH 原本的人格。" if blank else "不注入任何角色卡，保持 DSH 原本的人格与习惯。"
    ACTIVE_MD.write_text(
        f"当前场景：{label}\n更新时间：{datetime.now():%Y-%m-%d %H:%M}\n上一个场景：{prev or '（无）'}\n说明：{note}\n",
        encoding="utf-8",
    )
    result = {"ok": True, "name": label, "persona": "skipped", "msg": "", "previous": prev}
    if not PERSONA.is_file():
        result["persona"] = "no-file"
        result["msg"] = "还没有 DSH 人格文件，本来就没生效"
        return result
    try:
        old = PERSONA.read_text(encoding="utf-8")
    except Exception as e:
        result["persona"] = "error"
        result["msg"] = f"读不了人格文件：{e}"
        return result
    if WB_BEGIN not in old:
        result["persona"] = "clean"
        result["msg"] = "人格文件里本来就没有角色块"
        return result

    bdir = ROOT / "备份"
    bdir.mkdir(exist_ok=True)
    shutil.copy2(PERSONA, bdir / f"dsh-persona-{datetime.now():%Y%m%d-%H%M%S}.md")
    new = re.sub(re.escape(WB_BEGIN) + r".*?" + re.escape(WB_END), "", old, flags=re.S)
    new = re.sub(r"\n{3,}", "\n\n", new).rstrip() + "\n"
    try:
        PERSONA.write_text(new, encoding="utf-8")
        result["persona"] = "removed"
        result["msg"] = "已从 DSH 人格文件移除角色块"
    except Exception as e:
        result["persona"] = "error"
        result["msg"] = f"人格文件写不进去（{e}）。工作台内已停用，但 DSH 里还留着旧块。"
    return result


# ---------- 提案 ----------

def list_proposals() -> list[dict]:
    out = []
    if not PENDING.is_dir():
        return out
    for p in sorted(PENDING.glob("*.md")):
        text = p.read_text(encoding="utf-8", errors="replace")
        item = {"file": p.name, "title": p.stem, "body": text.strip()}
        for key, field in (("场景", "scene"), ("类型", "kind"), ("现状", "now"), ("建议", "suggest")):
            m = re.search(rf"^-\s*{key}[：:]\s*(.+)$", text, re.M)
            item[field] = m.group(1).strip() if m else ""
        out.append(item)
    return out


def create_proposal(data: dict) -> dict:
    scene = str(data.get("scene") or "").strip()
    kind = str(data.get("kind") or "改进").strip() or "改进"
    now = str(data.get("now") or "").strip()
    suggest = str(data.get("suggest") or "").strip()
    impact = str(data.get("impact") or "").strip()
    if not scene or not suggest:
        return {"ok": False, "msg": "至少写上「哪个场景」和「建议改成什么」"}
    if re.search(r'[\\/:*?"<>|]', scene):
        return {"ok": False, "msg": "场景名带了非法字符"}
    PENDING.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    head = suggest.splitlines()[0][:24].strip(" 。，,") or "改动"
    fname = f"{stamp}-{scene}-{head}.md"
    body = "\n".join([
        f"# 改动申请 · {head}",
        "",
        f"- 场景：{scene}",
        f"- 类型：{kind}",
        f"- 现状：{now or '（未填）'}",
        f"- 建议：{suggest}",
        f"- 影响：{impact or '（未填）'}",
        f"- 提出：{datetime.now():%Y-%m-%d %H:%M}",
        "",
    ])
    (PENDING / fname).write_text(body, encoding="utf-8")
    return {"ok": True, "file": fname}


def decide_proposal(file: str, decision: str) -> dict:
    if "/" in file or "\\" in file:
        return {"ok": False, "msg": "非法文件名"}
    src = PENDING / file
    if not src.is_file():
        return {"ok": False, "msg": "提案不存在"}
    DONE.mkdir(parents=True, exist_ok=True)
    verdict = "已同意" if decision == "accept" else "已驳回"
    body = src.read_text(encoding="utf-8", errors="replace").rstrip()
    body += f"\n\n- 处理：{verdict}  {datetime.now():%Y-%m-%d %H:%M}\n"
    (DONE / src.name).write_text(body, encoding="utf-8")
    src.unlink()
    return {"ok": True, "verdict": verdict}


# ---------- 服务 ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "WorkbenchUI/2.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) or b"{}"
        return json.loads(raw.decode("utf-8-sig"))

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            body = (HERE / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            self._json(scan())
        elif path == "/api/proposals":
            self._json({"items": list_proposals()})
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            data = self._body()
        except Exception:
            self._json({"ok": False, "msg": "坏请求"}, 400)
            return

        if path == "/api/scene/save":
            try:
                self._json(save_scene(data))
            except Exception as e:
                self._json({"ok": False, "msg": f"保存失败：{e}"}, 500)
        elif path == "/api/scene/activate":
            self._json(activate(str(data.get("name") or "")))
        elif path == "/api/scene/deactivate":
            self._json(deactivate())
        elif path == "/api/proposals/create":
            self._json(create_proposal(data))
        elif path == "/api/proposals/decide":
            self._json(decide_proposal(str(data.get("file") or ""), str(data.get("decision") or "")))
        elif path == "/api/open":
            kind = data.get("kind")
            if kind == "root":
                target = ROOT
            elif kind == "scene":
                name = str(data.get("name") or "")
                cand = SCENES / name
                if not name or "/" in name or "\\" in name or not cand.is_dir():
                    self._json({"ok": False, "msg": "没有这个场景"}, 404)
                    return
                target = cand
            elif kind == "prop":
                target = PENDING if PENDING.is_dir() else PROPOSALS
            else:
                self._json({"ok": False, "msg": "不支持的打开类型"}, 400)
                return
            try:
                os.startfile(str(target))  # type: ignore[attr-defined]
                self._json({"ok": True, "path": str(target)})
            except Exception as e:
                self._json({"ok": False, "msg": str(e)}, 500)
        else:
            self._json({"ok": False, "msg": "没有这个接口"}, 404)


def pick_port(preferred: int) -> int:
    for p in range(preferred, preferred + 20):
        try:
            ThreadingHTTPServer(("127.0.0.1", p), Handler).server_close()
            return p
        except OSError:
            continue
    return preferred


def build_snapshot() -> Path:
    data = scan()
    html = (HERE / "index.html").read_text(encoding="utf-8")
    inject = "<script>window.__WB_DATA__=" + json.dumps(data, ensure_ascii=False) + ";</script>\n<script>"
    html = html.replace("<script>\nconst HUE", inject + "\nconst HUE", 1)
    out = HERE / "快照.html"
    out.write_text(html, encoding="utf-8")
    return out


def main() -> int:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    PENDING.mkdir(parents=True, exist_ok=True)
    DONE.mkdir(parents=True, exist_ok=True)

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--build", action="store_true", help="生成离线快照，不起服务")
    args = ap.parse_args()

    if args.build:
        print(f"✓ 快照：{build_snapshot()}")
        return 0

    port = pick_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"工作台界面： {url}")
    print(f"根目录：     {ROOT}")
    print("Ctrl+C 停止")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
