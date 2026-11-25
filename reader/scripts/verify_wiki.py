"""零成本验证 wiki 层（阶段 0：目录骨架 / 草稿审批 / index / log / 反向映射）+ 全局层文件编辑。

用法：python scripts/verify_wiki.py

背景（2026-10-31，架构设计第 7 节 7.2–7.4）：阶段 0 刻意**不建模型表、不调 LLM**，
只用文件把「审批 → 入 wiki → 更新 index → 记 log → 反向映射」这条链跑通。本脚本
钉的是这条链上那些「错了不会报错、只会某天突然发现」的地方：

  A. `ensure_dirs` 必须幂等 —— app 启动 / 每次审批都会调它，重复调用不能冲掉内容。
  B. `approve` 是**唯一**入库闸门 —— 缺 frontmatter 字段必须拒（sources 断了反向映射、
     updated 断了"这是哪天的结论"）；目标已存在也必须拒（覆盖 = 静默删一页人的知识）。
  C. index 必须**按页路径**认行，不能按标题：标题会重名，按标题替换会静默吃掉别人的行。
  D. log 必须能被 `read_log` 解析回来（round-trip）—— "机器可解析"的真正判据不是
     正则长得像，是写进去能读出来。
  E. 反向映射 `meta/sources.json` 必须由页的 `sources` 全量重建成「来源 → 受影响页」，
     这是来源变化时唯一能指出"该复查哪几页"的东西。
  F. 全局层文件保存的三条护栏：空内容拒、未知 key 拒（不接前端传路径）、CRLF 归一 +
     旧版备份（防误覆盖）。
  K. `/wiki` 页面与端点：缺字段要**提前**点名并置灰按钮、失败要把后端原话显示出来、
     `?p=` 只能读 `wiki/` 之下的 .md、审批只吃 POST、中文退稿理由要进 log。
  L. `/wiki` 模板契约：不用 hx-prompt（HTTP 头 latin-1 装不下中文）、转义边界在
     mdrender 而非模板、rail 有入口、禁用态有样式。
  M. 划线/批注（阶段 1）：source_id 校验唯一实现（entry 正整数 / report 形如
     daily-日期 / web·chat 尚未启用）、空摘录拒、端点失败把原因显示在片段里、
     组件挂到文章详情与日报/周报详情三处。
  N. 「问 AI」长文分片（2026-11-15）：正文不再截断 —— `ask()` 先 `split_body()`
     精确切片，多片则逐片取材 + 一次汇总；这里钉源码形状（`clip_quotes` 不许
     回到对话链路、分片共用一份 stats、引文边界按深度补、失败要三处声明缺口）。
     行为判据在 `core/tests/test_conversation_chunked.py`。
  O. 只收高质量信号（2026-11-16）：审批对话不进待提炼清单（否则每轮编译都报
     「1 处没提炼成：draftbatch:current」且永远消不掉）；阅读列表不再有
     「发送到 wiki」入口（按**形状**判，注释里的说明不算），但 `/wiki/ingest`
     与 `extract(force=False)` 能力保留。行为判据在
     `core/tests/test_ingest_signal_only.py`。
  Q. 审批页就地改标签（2026-11-16，用户要求「参考微信好友 tag，输入时能选已有的」）：
     收敛判据只有一份（`clean_tags`，两条写盘路径都调它）、超限**拒收**不截断、
     chip 的 HTML 只有一份（初始渲染与 POST 回包共用）、前端换的节点 id 与端点
     地址必须和模板/路由一致。行为判据在 `core/tests/test_approval_tags.py`。
  U. 首页不露目录绝对路径（2026-11-16，用户：「路径里的用户名等会泄露隐私」）：
     `/wiki` 只许打 `stats.root_label`（相对项目根 `data/wiki`；WIKI_DIR 指到项目外
     则「自定义目录」），**所有** wiki 模板都不许出现未脱敏的 `stats.root`；
     `stats()` 必须真给这个键（缺键模板静默变空、没人报错）；`root_label` 靠
     `relative_to` 判项目内外，不是截字符串、也不是取不到就退回绝对路径。
     行为判据在 `core/tests/test_wiki_root_label.py`（真渲染，路径里故意带
     `/Users/zhangsan` 断言一个字都不出现）。
  V. 对话消息气泡 + 进入即滚底（2026-11-19，用户要求「我发的右对齐+浅背景、
     参考业界；进入对话自动定位到底部」）：两处对话共用 `_wc_msg.html` 与 `.wc-*`
     样式，所以气泡只在 app.css 改一处；class 名 wc-user/wc-assistant 不许动
     （test_approval_chat 钉死）。滚底 js 三处时机：审批浮层 openModal 打开、
     文章浮窗首次加载 settle、文章浮窗重开；`?msg=` 深链待定位时不许滚。
     这些都是「没症状的缺陷」（不对齐/不滚底没人报错），只能靠形状守。
  V2. 气泡去身份标签 + 生成期禁用发送按钮（2026-11-19）：右对齐+底色已自明，
     wc-who（我/AI）白占一行 → 模板/JS/CSS 三处全删（只删一处别处会画回来）；
     自动总结走 submit→wcStreamSubmit，SSE 拦了 htmx 使 hx-disabled-elt 失效，
     故在 JS 里手动禁用发送按钮、cleanup 解禁，并在飞时吞 submit 不放行 htmx
     （disabled 只挡鼠标，textarea 的 Enter 仍触发 submit，会重复发一条花钱）。

★ 全部在临时目录里跑（override_settings WIKI_DIR / PROJECT_ROOT），不碰真实 data/wiki
  与 Meta/ —— 验证脚本绝不能改用户数据。
"""
import glob
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "apps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402
django.setup()

# ★★ 数据库隔离（2026-10-31 事故）：
#    本脚本 M 段要"真落库"测划线/批注，原先直接对**真实 data/reader.sqlite3** 里的
#    core_highlight / core_annotation 执行 `.objects.all().delete()` —— 于是每跑一次
#    验证，用户的划线就被清空一次（表现为"重启/改完代码后我的划线全没了"）。
#    现在整体切到 Django 测试库（sqlite 内存，跑完 atexit 拆掉），真实库一行不碰。
#    ★ 判据不是"我改成了 test db"，而是跑之前往真实库插一条哨兵、跑完它还在。
import _dbtest  # noqa: E402

_dbtest.isolate()

from django.template.loader import render_to_string  # noqa: E402
from django.test import RequestFactory, override_settings  # noqa: E402

from core import marks, mdrender, promptspec, wiki  # noqa: E402
from prefs import views as prefs_views  # noqa: E402
from wiki import views as wiki_views  # noqa: E402  （wiki 是 URL 分区的 app，存储层是 core.wiki）

ok = bad = 0
fails = []


def check(name, cond, detail=""):
    global ok, bad
    if cond:
        ok += 1
        print("  PASS  %s" % name)
    else:
        bad += 1
        fails.append(name)
        print("  FAIL  %s   %s" % (name, detail))


def read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


PANEL = read("templates/prefs/_panel.html")
JS = read("static/app.js")
CSS = read("static/app.css")
URLS = read("apps/prefs/urls.py")

GOOD_DRAFT = """---
title: 融资与资本开支
domain: invest
sources: [entry:4907]
updated: 2026-10-31
confidence: medium
tags: []
candidates: [1]
---

# 融资与资本开支

- 巨头用发债而不是配售，说明它们认为自己股价不便宜。
"""

print("\n== A. 目录骨架：幂等 ==")
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "wiki"
    with override_settings(WIKI_DIR=root):
        r1 = wiki.ensure_dirs()
        check("首次建出全部子目录",
              all((root / d).exists() for d in wiki.SUBDIRS),
              str([d for d in wiki.SUBDIRS if not (root / d).exists()]))
        check("首次写出 index/log/schema/sources.json",
              all((root / n).exists() for n in
                  ("index.md", "log.md", "schema.md", "meta/sources.json")))
        check("首次返回 created 非空（有初始化动作）", bool(r1["created"]), str(r1))

        # 写点内容再 ensure 一次：不能覆盖、不能重复记 init
        (root / "schema.md").write_text("# 我自己改过的规范\n", encoding="utf-8")
        init_before = len([e for e in wiki.read_log() if e["action"] == "init"])
        r2 = wiki.ensure_dirs()
        check("★ 第二次 ensure 不覆盖已存在文件（幂等）",
              (root / "schema.md").read_text(encoding="utf-8") == "# 我自己改过的规范\n")
        check("第二次 created 为空", r2["created"] == [], str(r2))
        check("★ init 只记一次（否则每次调用都往日志灌一条）",
              len([e for e in wiki.read_log() if e["action"] == "init"]) == init_before)

        print("\n== B. frontmatter 解析 ==")
        fm, body = wiki.parse_front(GOOD_DRAFT)
        check("内联列表解析成真列表", fm.get("sources") == ["entry:4907"], str(fm))
        check("title/domain/updated/confidence 都取到",
              fm.get("title") == "融资与资本开支" and fm.get("domain") == "invest"
              and fm.get("updated") == "2026-10-31" and fm.get("confidence") == "medium")
        check("正文不含 frontmatter", body.lstrip().startswith("# 融资与资本开支"), body[:30])

        block = "---\ntitle: 甲\ndomain: ai\nsources:\n- entry:1\n- web:x\nupdated: 2026-10-31\nconfidence: low\n---\n正文\n"
        fm2, _ = wiki.parse_front(block)
        check("块状列表（`- item` 换行写法）也能解析",
              fm2.get("sources") == ["entry:1", "web:x"], str(fm2))
        fm3, body3 = wiki.parse_front("没有 frontmatter 的纯正文\n")
        check("无 frontmatter 时返回空 dict + 原文", fm3 == {} and body3.startswith("没有"))
        check("★ 不引 PyYAML（依赖是技术债，这点结构自己解析）",
      re.search(r"^\s*(?:import|from)\s+yaml", read("core/wiki.py"), re.M) is None)

        print("\n== C. 审批（Gate 1）==")
        (root / ".drafts" / "capex.md").write_text(GOOD_DRAFT, encoding="utf-8")
        drafts = wiki.list_drafts()
        check("草稿列表能列出（complete=True）",
              len(drafts) == 1 and drafts[0]["complete"] is True, str(drafts))

        okward, payload = wiki.approve("capex.md")
        check("slug 带 .md 后缀时拒绝（slug 就应是文件名主干）", okward is False, str(payload))

        okward, payload = wiki.approve("capex")
        check("正常草稿审批通过", okward is True, str(payload))
        check("落到了 wiki/<domain>/<slug>.md",
              (root / "wiki" / "invest" / "capex.md").exists(), str(payload))
        check("草稿已从 .drafts 移走（不是复制）",
              not (root / ".drafts" / "capex.md").exists())
        check("payload 带回 page / domain / sources",
              payload.get("page") == "wiki/invest/capex.md"
              and payload.get("domain") == "invest"
              and payload.get("sources") == ["entry:4907"], str(payload))

        idx = (root / "index.md").read_text(encoding="utf-8")
        check("index 出现该页一行（带页相对路径）", "](wiki/invest/capex.md)" in idx, idx)
        check("index 挂在该 domain 小节下", "## invest" in idx, idx)
        check("index 行带摘要（不是只有标题）",
              re.search(r"\]\(wiki/invest/capex\.md\) — .+", idx) is not None, idx)

        entries = wiki.read_log()
        check("★ log 能 round-trip 解析回来（机器可解析的真判据）",
              any(e["action"] == "approve" and e["target"] == "wiki/invest/capex.md"
                  for e in entries), str(entries))
        check("log 条目带日期与时分",
              entries and re.fullmatch(r"\d{4}-\d{2}-\d{2}", entries[0]["date"] or "")
              and re.fullmatch(r"\d{2}:\d{2}", entries[0]["time"] or ""), str(entries[:1]))

        # 缺字段 / 非法值 / 同名目标
        (root / ".drafts" / "nofm.md").write_text("# 只有正文\n", encoding="utf-8")
        okward, err = wiki.approve("nofm")
        check("★ 缺 frontmatter 字段拒绝（sources 缺了反向映射就断）", okward is False, str(err))
        check("拒绝文案点名缺的字段", "title" in str(err) and "sources" in str(err), str(err))

        bad_conf = GOOD_DRAFT.replace("confidence: medium", "confidence: 大概")
        (root / ".drafts" / "badconf.md").write_text(bad_conf, encoding="utf-8")
        okward, err = wiki.approve("badconf")
        check("confidence 非法值拒绝", okward is False and "confidence" in str(err), str(err))

        (root / ".drafts" / "capex2.md").write_text(GOOD_DRAFT, encoding="utf-8")
        # 手动造一个同名目标（模拟"已审批过一版"）
        (root / "wiki" / "invest" / "capex2.md").write_text(GOOD_DRAFT, encoding="utf-8")
        okward, err = wiki.approve("capex2")
        check("★ 目标页已存在时拒绝（阶段 0 不覆盖人的知识）",
              okward is False and "已存在" in str(err), str(err))

        okward, err = wiki.approve("../escape")
        check("★ 草稿名带 ../ 拒绝（不能逃出 wiki/）", okward is False, str(err))

        print("\n== D. index：幂等 + 按路径认行 ==")
        wiki.index_upsert("wiki/invest/capex.md", "融资与资本开支", "新摘要", "2026-11-01")
        idx = (root / "index.md").read_text(encoding="utf-8")
        check("★ 同一页重复 upsert 只留一行（幂等）",
              idx.count("](wiki/invest/capex.md)") == 1, idx)
        check("upsert 覆盖旧摘要而不是留两行",
              "新摘要" in idx and "新摘要" not in idx.replace("新摘要", "", 1), idx)

        wiki.index_upsert("wiki/ai/capex.md", "融资与资本开支", "同名不同域", "2026-11-01")
        idx = (root / "index.md").read_text(encoding="utf-8")
        check("★ 同名标题的两页各自保留自己的行（按路径认行，不按标题）",
              idx.count("](wiki/invest/capex.md)") == 1
              and idx.count("](wiki/ai/capex.md)") == 1, idx)
        check("新 domain 自动建小节", "## ai" in idx, idx)

        check("index_remove 能删掉该页行",
              wiki.index_remove("wiki/ai/capex.md") is True
              and "](wiki/ai/capex.md)" not in (root / "index.md").read_text(encoding="utf-8"))
        check("index_remove 删不存在页返回 False（不报错）",
              wiki.index_remove("wiki/none/x.md") is False)

        print("\n== E. 反向映射 sources.json ==")
        # 先清掉 B 段为「目标已存在」造的临时页，让映射断言只对真正的页成立
        (root / "wiki" / "invest" / "capex2.md").unlink()
        mapping = wiki.rebuild_sources_index()
        check("来源 entry:4907 → 页列表",
              mapping.get("entry:4907") == ["wiki/invest/capex.md"], str(mapping))
        check("affected_pages 能查到受影响页",
              wiki.affected_pages("entry:4907") == ["wiki/invest/capex.md"])
        check("查不到来源时返回空列表（不是 None，调用方不必再判空）",
              wiki.affected_pages("entry:999999") == [])
        on_disk = (root / "meta" / "sources.json").read_text(encoding="utf-8")
        check("落盘是 JSON（可被外部工具读）",
              "entry:4907" in on_disk and on_disk.strip().startswith("{"), on_disk)

        # 再走一遍真实审批进另一个域 → 映射应有两个页（"来源变了要复查哪几页"的正面用例）
        (root / ".drafts" / "capex-ai.md").write_text(
            GOOD_DRAFT.replace("domain: invest", "domain: ai"), encoding="utf-8")
        okward, payload = wiki.approve("capex-ai")
        check("★ 审批时自动建出新的域目录（软分区不必预建）",
              okward is True and (root / "wiki" / "ai" / "capex-ai.md").exists(), str(payload))
        mapping = wiki.rebuild_sources_index()
        check("★ 一个来源可映射到多页（跨域同源）",
              sorted(mapping.get("entry:4907") or []) ==
              ["wiki/ai/capex-ai.md", "wiki/invest/capex.md"], str(mapping))

        print("\n== F. 退草稿：不删只归档 ==")
        (root / ".drafts" / "junk.md").write_text(GOOD_DRAFT, encoding="utf-8")
        okward, where = wiki.discard("junk", "重复")
        check("退掉后进 .discarded", okward is True and "discarded" in str(where), str(where))
        check("★ 草稿是移动不是删除（留证据回看）",
              (root / where).exists() and not (root / ".drafts" / "junk.md").exists())
        check("退掉的不出现在待审批列表里",
              all(d["slug"] != "junk" for d in wiki.list_drafts()), str(wiki.list_drafts()))

        print("\n== G. 概览 stats ==")
        st = wiki.stats()
        check("stats 的页数与 pages() 一致", st["pages"] == len(wiki.pages()), str(st))
        check("stats 带 domain 列表与 last 日志", st["domains"] and st["last"], str(st))

print("\n== H. 全局层文件：ontology_text() ==")
with tempfile.TemporaryDirectory() as td:
    fake_root = Path(td)
    (fake_root / "Meta").mkdir(parents=True)
    with override_settings(PROJECT_ROOT=fake_root):
        check("缺失时返回空串（不抛异常）", promptspec.ontology_text() == "")
        (fake_root / "Meta" / "ontology.md").write_text("# 本体\n#invest\n", encoding="utf-8")
        check("存在时原样读出并 strip",
              promptspec.ontology_text() == "# 本体\n#invest", repr(promptspec.ontology_text()))
        check("ontology_hash 随内容变",
              promptspec.ontology_hash() == hashlib.sha256(
                  "# 本体\n#invest".encode()).hexdigest()[:16])
        check("★ 本体不设长度上限（截断会毁掉层级）",
              "ONTOLOGY_LIMIT" not in read("core/promptspec.py"))

print("\n== I. 设置页「全局层文件」= 模板 / JS / CSS 契约 ==")
check("模板有 globals 分组（左列 tab）",
      'data-g="globals"' in PANEL and "全局层文件" in PANEL)
check("模板有 textarea.pref-file-box（整篇 markdown 才放得下）",
      "pref-file-box" in PANEL)
check("★ 保存是显式按钮 onclick=prefsSaveFile（长文不能用 onchange 误触即存）",
      'onclick="prefsSaveFile(this)"' in PANEL)
check("textarea 绑 oninput 实时算字数", 'oninput="prefsCountChars(this)"' in PANEL)
check("文件缺失时给 cp 命令（不留空白框）", "{{ row.cp_cmd }}" in PANEL)
check("文件路径只读展示，不提供输入框（路径不可由前端指定）",
      "Meta/{{ row.file }}" in PANEL and "name=\"path\"" not in PANEL)
check("app.js 定义 window.prefsSaveFile", "window.prefsSaveFile = function" in JS)
check("app.js 定义 window.prefsCountChars", "window.prefsCountChars = function" in JS)
check("保存打 /prefs/file 端点", 'postForm("/prefs/file"' in JS)
check("★ 保存期间禁用按钮（防连点重复写盘）", "btn.disabled = true" in JS)
check("css 有 .pref-file-box", ".pref-file-box" in CSS)
check("urls 注册 prefs/file", "prefs/file" in URLS)

rf_ctx = RequestFactory()
check("★ 深链 ?g=globals 能直接打开全局层文件组（active 白名单含 globals）",
      prefs_views._ctx(rf_ctx.get("/prefs?g=globals"))["active"] == "globals")
check("未知 ?g= 回落到第一组（不乱展开）",
      prefs_views._ctx(rf_ctx.get("/prefs?g=nope"))["active"]
      == prefs_views.sk.GROUPS[0].key)

txt = render_to_string("prefs/_panel.html", {
    "groups": [],
    "active": "globals",
    "notify": {"bin": "terminal-notifier", "installed": True, "hint": ""},
    "files": [{
        "key": "ontology", "label": "知识本体", "file": "ontology.md",
        "example": "ontology.example.md", "help": "回答「知识分几个域」",
        "hint": "先粗后细", "text": "---\ntitle: 甲\n---\n", "exists": False,
        "chars": 0, "path": "/tmp/Meta/ontology.md",
        "cp_cmd": "cp Meta/ontology.example.md Meta/ontology.md",
    }],
})
check("渲染出 globals section 且默认展开（无 hidden）",
      re.search(r'data-g="globals"\s*>', txt) is not None, txt[:400])
check("渲染出问题文件的 cp 提示", "cp Meta/ontology.example.md Meta/ontology.md" in txt)
check("textarea 里带着当前内容", "title: 甲" in txt)
check("★ 渲染结果没有内联 <script>（innerHTML 注入不执行）", "<script" not in txt.lower())

print("\n== J. 保存端点：三条护栏 ==")
with tempfile.TemporaryDirectory() as td:
    fake_root = Path(td)
    (fake_root / "Meta").mkdir(parents=True)
    (fake_root / "Meta" / "ontology.md").write_text("# 旧本体\n", encoding="utf-8")
    rf = RequestFactory()
    with override_settings(PROJECT_ROOT=fake_root):
        r = prefs_views.save_file(rf.post("/prefs/file", {"key": "", "value": "x"}))
        check("未知/空 key 拒绝", r.status_code == 200 and
              json.loads(r.content)["ok"] is False)

        r = prefs_views.save_file(rf.post(
            "/prefs/file", {"key": "../../etc/passwd", "value": "x"}))
        body = json.loads(r.content)
        check("★ 路径穿越被拒（只认白名单 key，不接前端传路径）", body["ok"] is False, str(body))

        r = prefs_views.save_file(rf.post(
            "/prefs/file", {"key": "ontology", "value": "   \n  "}))
        body = json.loads(r.content)
        check("★ 空内容拒绝（防误清空全局层）", body["ok"] is False, str(body))
        check("被拒时文件内容没变",
              (fake_root / "Meta" / "ontology.md").read_text(encoding="utf-8") == "# 旧本体\n")

        r = prefs_views.save_file(rf.post(
            "/prefs/file", {"key": "ontology", "value": "# 新本体\r\n第二行"}))
        body = json.loads(r.content)
        check("正常保存返回 ok + chars", body["ok"] is True and body["chars"] > 0, str(body))
        text = (fake_root / "Meta" / "ontology.md").read_text(encoding="utf-8")
        check("★ CRLF 归一成 LF 且结尾补单个换行", "\r" not in text and text.endswith("\n"),
              repr(text))
        check("内容正确落盘", text == "# 新本体\n第二行\n", repr(text))
        check("★ 保存前留了旧版备份（防误覆盖）", bool(body.get("backup"))
              and (fake_root / body["backup"]).exists(), str(body))
        check("备份内容 = 旧版",
              (fake_root / body["backup"]).read_text(encoding="utf-8") == "# 旧本体\n")
        check("没有遗留 .tmp 半截文件",
              not list((fake_root / "Meta").glob("*.tmp")), str(list((fake_root / "Meta").iterdir())))

print("\n== K. /wiki 页面与端点（阶段 0 的 0.3）==")
with tempfile.TemporaryDirectory() as td:
    root = Path(td) / "wiki"
    rf = RequestFactory()
    with override_settings(WIKI_DIR=root):
        wiki.ensure_dirs()
        (root / ".drafts" / "capex.md").write_text(GOOD_DRAFT, encoding="utf-8")
        (root / ".drafts" / "broken.md").write_text("# 只有正文\n", encoding="utf-8")

        r = wiki_views.index(rf.get("/wiki"))
        page = r.content.decode()
        check("首页 200 且能渲染", r.status_code == 200 and len(page) > 2000, str(r.status_code))
        # ★ 2026-11-09：首页草稿卡下线，裁决唯一入口是审批浮层；
        #   同日起「编译中途被审批期打断」已去掉（草稿分批产生，跑一半拒编会卡死
        #   流水线），但**有草稿待审批时**按钮仍灰：审批未完成不开新一轮编译
        #   （防草稿越堆越多、审批上下文被打散）。这里造了 2 份草稿，
        #   所以看到的是「待审批」这一支的人话，不是「没候选没痕迹」。
        check("首页不再有并行草稿卡，审批入口在浮层",
              "draft-card" not in page and "打开审批浮层" in page)
        check("★ 有草稿不再打断编译中：没有「审批期间编译暂停」这种文案",
              "审批期间编译暂停" not in page)
        check("★ 有草稿待审批时编译按钮灰着并说清为什么",
              'type="button" disabled' in page and "份草稿待审批" in page)
        r = wiki_views.approval_draft(rf.get("/wiki/approval/draft", {"slug": "broken"}))
        wa_frag = r.content.decode()
        check("★ 缺字段的草稿在浮层工作区**提前**点名缺哪几个",
              "缺 frontmatter 字段" in wa_frag and "title" in wa_frag and "sources" in wa_frag)
        check("★ 浮层工作区有单页退稿入口（唯一退稿 UI）",
              'hx-post="/wiki/discard"' in wa_frag and 'name="reason"' in wa_frag)
        check("空态说明「草稿从哪来」，不是空白箱",
              "还没有入库的页" in page or "待审批" in page)

        # 审批成功：片段 + HX-Trigger（toast）
        r = wiki_views.approve(rf.post("/wiki/approve", {"slug": "capex"}))
        frag = r.content.decode()
        check("审批成功后落到 wiki/<domain>/", (root / "wiki" / "invest" / "capex.md").exists())
        check("★ 返回的是 #wiki-body 片段（htmx outerHTML 要求单一根节点）",
              frag.count('id="wiki-body"') == 1, frag[:120])
        check("片段里已入库列表出现该页",
              "已入库 1" in frag and "wiki/invest/capex.md" in frag, frag[:300])
        trig = r.get("HX-Trigger") or ""
        check("审批带 HX-Trigger（toast 提示，不靠横幅挤动布局）", "wikiFlash" in trig, trig)
        check("★ HX-Trigger 是纯 ASCII（HTTP 头只能 latin-1，中文会抛 UnicodeEncodeError）",
              trig.isascii() and "\\u" in trig, repr(trig))

        # 审批失败：后端原话必须出现在那张卡片里
        r = wiki_views.approve(rf.post("/wiki/approve", {"slug": "broken"}))
        frag = r.content.decode()
        check("★ 审批失败把后端原话显示在页面上（缺哪个字段）",
              "未生效" in frag and "frontmatter 缺字段" in frag, frag[:400])
        check("失败后草稿仍在（没被吞掉）", (root / ".drafts" / "broken.md").exists())

        check("★ approve 只吃 POST：GET 返回 405（否则链接预取就能改文件）",
              wiki_views.approve(rf.get("/wiki/approve")).status_code == 405)

        # 退稿：中文理由
        r = wiki_views.discard(rf.post("/wiki/discard",
                                       {"slug": "broken", "reason": "重复条目，已有正文"}))
        check("退稿进 .discarded 且从待审批消失",
              len(list((root / ".drafts" / ".discarded").glob("broken-*.md"))) == 1
              and not (root / ".drafts" / "broken.md").exists())
        check("★ 中文退稿理由写进了 log（htmx 的 hx-prompt 走头部，中文发不出去）",
              "重复条目" in (root / "log.md").read_text(encoding="utf-8"))

        # 详情页
        r = wiki_views.page(rf.get("/wiki/page", {"p": "wiki/invest/capex.md"}))
        body = r.content.decode()
        check("详情页 200 并渲染正文", r.status_code == 200 and "股价不便宜" in body)
        check("详情页列出 sources（可反查）", "entry:4907" in body)
        check("★ 越权路径 404（?p= 只认 wiki/ 之下的 .md）",
              wiki_views.page(rf.get("/wiki/page",
                                     {"p": "../../Meta/ontology.md"})).status_code == 404)
        check("不存在的页 404 而不是 500",
              wiki_views.page(rf.get("/wiki/page", {"p": "wiki/x/none.md"})).status_code == 404)
        check("空 p 也不炸", wiki_views.page(rf.get("/wiki/page")).status_code == 404)

        # 按来源筛选（反向映射的界面入口）
        (root / ".drafts" / "capex-ai.md").write_text(
            GOOD_DRAFT.replace("domain: invest", "domain: ai"), encoding="utf-8")
        wiki_views.approve(rf.post("/wiki/approve", {"slug": "capex-ai"}))
        r = wiki_views.index(rf.get("/wiki?src=entry:4907"))
        body = r.content.decode()
        check("★ ?src= 能按来源反查（来源变了要知道复查哪几页）",
              "正在按来源筛选" in body and "capex-ai.md" in body, body[:300])
        check("来源筛选下不相关的页不出现",
              "无关页" not in body)

print("\n== L. /wiki 模板契约 ==")
BODY_T = read("templates/wiki/_body.html")
PAGE_T = read("templates/wiki/page.html")
WORKAREA_T = read("templates/wiki/_approval_workarea.html")
TITLE_T = read("templates/wiki/_approval_title.html")
HEAD_T = read("templates/wiki/_approval_head.html")
CHAT_T = read("templates/wiki/_approval_chat.html")
VIEWS_T = read("apps/wiki/views.py")
# ★ 别叫 BASE：脚本顶部 BASE 是「reader 目录」，覆盖掉会让 read() 把文件内容当路径用
BASE_HTML_T = read("templates/base.html")
# 只看**属性**：注释里为解释原因提到了 hx-prompt 这个词，按子串查会误报
# ★ 2026-11-09：退稿 UI 从首页工作台搬进审批浮层工作区，契约跟着搬。
# ★ 2026-11-13：不再整页刷新（会关掉浮层）。旧判据在**整个 views.py** 里找
#   `resp["HX-Refresh"] = "true"` —— compile watch 端点还留着同一串，于是
#   discard 改成不刷新了守卫照样假绿。改成按 def 切片，只认它自己的函数体。
def _fn_src(name, src):
    m = re.search(r"^def %s\(" % re.escape(name), src, re.M)
    if not m:
        return ""
    nxt = re.search(r"^def ", src[m.end():], re.M)
    end = m.end() + (nxt.start() if nxt else len(src) - m.end())
    return src[m.start():end]


def _form_tag(src, marker):
    """切出某个 <form ...> 开标签：判据只看属性，别被注释里的字样骗。"""
    m = re.search(r"<form[^>]*" + re.escape(marker) + r"[^>]*>", src, re.S)
    return m.group(0) if m else ""


DISCARD_FN = _fn_src("discard", VIEWS_T)
# ★ 只认「给响应头赋值」这个形状：注释/docstring 里为解释原因提到 HX-Refresh
#   不算数，换个变量名也照样抓得到（本脚本 449 行 hx-prompt 踩过同一个坑）。
REFRESH_ASSIGN = re.compile(r"\[\s*['\"]HX-Refresh['\"]\s*\]\s*=")
RELATED_FORM = _form_tag(WORKAREA_T, 'hx-post="/wiki/approval/related"')
DISCARD_FORM = _form_tag(WORKAREA_T, 'class="inline-form approval-discard"')
MERGE_FORM = _form_tag(WORKAREA_T, 'hx-post="/wiki/approval/merge-into"')
check("★ 退稿理由走表单字段，不用 hx-prompt（HTTP 头 latin-1 装不下中文）",
      "hx-prompt=" not in BODY_T and "hx-prompt=" not in WORKAREA_T
      and 'name="reason"' in WORKAREA_T)
check("首页写动作都用 htmx 换 #wiki-body（一处上下文，不做两套）",
      BODY_T.count('hx-target="#wiki-body"') >= 2)
check("★ 退稿成功不整页刷新、失败留在工作区给原因（按 def 切片，不假绿）",
      bool(DISCARD_FN)
      and 'name="from" value="approval"' in WORKAREA_T
      and "discard_err" in WORKAREA_T
      and not REFRESH_ASSIGN.search(DISCARD_FN),
      "discard 里又给整页刷新头赋值 = 把浮层关了")
check("★ 退稿不关浮层：工作区/列表/头部/对话区四个面板一次 OOB 刷全",
      all(x in DISCARD_FN for x in (
          '"wiki/_approval_workarea.html"', '"wiki/_approval_list.html"',
          '"wiki/_approval_head.html"', '"wiki/_approval_chat.html"'))
      and 'chat["oob"] = True' in DISCARD_FN
      and '{% if oob %} hx-swap-oob="true"' in CHAT_T,
      "少刷一个面板 = 界面还指着已归档草稿")
check("★ 找找相关页不弹二次确认；退稿 / 改为并入两处确认仍在",
      bool(RELATED_FORM) and bool(DISCARD_FORM) and bool(MERGE_FORM)
      and "hx-confirm" not in RELATED_FORM
      and "hx-confirm" in DISCARD_FORM and "hx-confirm" in MERGE_FORM,
      "related=%r" % RELATED_FORM[:120])
# ---- 2026-11-13：退稿依赖同步（只读核形状；产物由单测 / e2e 钉） ----
# ★ 真实库 16:58 退 D3 后没同步，18:34 带死链入库、批末又补拉一份草稿。
#   旧实现只在 wiki_agent._commit_writes(archive=) 里解包过「其他草稿」，
#   用户手点退稿完全没同步，已入库页更是没人管。收口到 core.discard 唯一落点。
CORE_T = read("core/wiki.py")
AGENT_T = read("core/wiki_agent.py")
CORE_DISCARD_FN = _fn_src("discard", CORE_T)
SYNC_FN = _fn_src("sync_discard_references", CORE_T)
REWRITE_FN = _fn_src("_rewrite_wikilinks", CORE_T)
check("★ 退稿依赖同步只有一处落点：core.discard 自己调 sync_discard_references",
      bool(CORE_DISCARD_FN) and "sync_discard_references(" in CORE_DISCARD_FN
      and "sync_report" in CORE_DISCARD_FN,
      "同步散回调用点 = 四条退稿路径必漏")
check("★ 同步覆盖草稿正文 + 已入库页（pull_page 审批副本），且零 LLM",
      bool(SYNC_FN)
      and "unwrap_wikilinks(" in SYNC_FN and "pull_page(" in SYNC_FN
      and "DISCARD_REF_FM_KEYS" in SYNC_FN and "guard_errors" in SYNC_FN
      and not any(x in SYNC_FN
                  for x in ("_call(", "_polish_one(", "recommend(")),
      "改已入库页必须走副本；维护路径不许花钱")
check("★ wiki_agent 不再留第二份解包实现（只认调用形状，不认注释字样）",
      "def _unwrap_slug_references" not in AGENT_T
      and "_unwrap_slug_references(" not in AGENT_T)
check("★ 退稿回执把同步结果说清楚（副本数 / 待处理 / 守卫）",
      "sync_report=" in DISCARD_FN and "_discard_flash(" in DISCARD_FN
      and 'open_approval=bool(sync.get("pages"))' in DISCARD_FN,
      "只说『已退稿』= 下次又问『怎么还剩一个 draft』")
check("★ 两套降级显示文本收口：relink 降级也吃归档页标题而非裸 slug",
      "titles.get(target)" in REWRITE_FN)
check("★ 浮层头部两处计数抽成独立 oob 片段，归档/合并后跟着真实数量刷（2026-11-11）",
      'id="approval-head"' in HEAD_T and "{{ count }}" in HEAD_T
      and '_approval_head.html' in read("apps/wiki/views.py"))
check("★ 草稿正文用 mdrender 渲染后再 |safe（先 escape 再套标签）",
      "{{ body_html|safe }}" in WORKAREA_T and "mdrender" in read("apps/wiki/views.py"))
# 转义边界在 mdrender（先 escape 再套标签），不在模板：模板拿到的已经是 HTML。
# 所以钉的是「模型输出 <script> 进去、出来只剩文本」，不是「模板里有 safe」。
_evil = mdrender.render("<script>alert(1)</script>\n\n> <img src=x onerror=1>")
check("★ 渲染层先 escape 再套标签（模型写 <script> 也只显示成文本）",
      "<script" not in _evil.lower() and "&lt;script&gt;" in _evil.lower(), _evil)
check("★ 模板确实对预渲染 html 用 safe（否则正文会显示成转义后的源码）",
      "{{ body_html|safe }}" in WORKAREA_T and "{{ body_html|safe }}" in PAGE_T)
check("详情页把来源 id 做成链接（点进去看同源页）",
      'href="/wiki?src=' in PAGE_T)
# ★ 第十三步（形态 A）：正文 = 纯 polish 一段；精确出处收进折叠「溯源」区
#   （读 IngestCandidate 结构化字段，按 quote_from 分组，必须精确）。
#   ★ 三条可见分支缺任一条都不会报错、只会静默显示错东西 —— 钉模板结构，
#     而不是只钉「有没有某个变量」；这与第十二步「老页是当前主路径」同理。
check("★ 详情页结论 / 老页回退 / 结论还没生成三条分支都在（不静默降级）",
      PAGE_T.count("{{ body_html|safe }}") >= 2
      and "{% if body_blank %}" in PAGE_T
      and "{% elif legacy_page %}" in PAGE_T
      and "结论还没生成" in PAGE_T
      and "清零前的老页" in PAGE_T)
# ★★ 实读纠偏：真实库 31/32 页是 `## 我的判断` 三块、0 页含 `## 候选清单`/
#    `## 通顺话`。只认后两个字形会把唯一主路径静默标成「结论」。判据必须收在
#    `wstore.is_legacy_body` 一处，且按「正文有无结构标题」判，不枚举退役常量。
check("★ 老页判据收在 is_legacy_body 一处，详情页确实调它",
      "wstore.is_legacy_body(body)" in read("apps/wiki/views.py")
      and "def is_legacy_body(" in read("core/wiki.py"))
check("★ 折叠溯源区按候选分组给原话/出处/时间/原文链接，幽灵候选显式可见",
      "溯源 {{ trace.total }} 条" in PAGE_T
      and "trace.groups" in PAGE_T
      and "trace.unknown" in PAGE_T
      and "r.quote" in PAGE_T and "r.published_at" in PAGE_T
      and "r.link" in PAGE_T and "看原文" in PAGE_T)
check("★ 反链区块有反链才渲染，且索引过期有可见提示（不留空壳、不静默）",
      "{% if backlink_rows %}" in PAGE_T and "找不到引用句" in PAGE_T)
# ★ 2026-11-08 实景手感：slug 是存储标识，不进**阅读/列表态**的可见文字。
#   论域必须收窄：hidden input、preview 锚点、断链清单、审批工作区仍合法用 slug，
#   一刀切禁会误伤编辑态；这里只钉「首页页表」这一处显示。
#   （草稿卡 2026-11-09 已随工作台下线，原来钉 d.slug 的那半条跟着撤。）
check("★ 首页页表的 slug 只作 hover，不进可见文字",
      'title="{{ p.slug }}.md"' in BODY_T
      and "{{ p.slug }}.md · " not in BODY_T)
check("★ 编辑态仍显示 slug（拆页新页名 / 目标页重名消歧有职能，别被一起删掉）",
      "{{ slug }}.md" in WORKAREA_T + TITLE_T and "{{ t.slug }}" in WORKAREA_T)
check("★ rail 有 /wiki 入口且带 active 判定",
      'href="/wiki"' in BASE_HTML_T and "'/wiki' in request.path" in BASE_HTML_T)
check("CSS 有禁用态（否则 disabled 按钮长得和能点的一样）", ".btn:disabled" in CSS)
check("CSS 有草稿卡 / 不完整态 / 置信三档",
      all(x in CSS for x in (".draft-card", ".draft-card.incomplete",
                             ".tag.conf-high", ".tag.conf-low")))
check("app.js 监听 wikiFlash（toast）", 'addEventListener("wikiFlash"' in JS)
check("urls 注册 wiki/approve 与 wiki/discard",
      "wiki/approve" in read("apps/wiki/urls.py") and "wiki/discard" in read("apps/wiki/urls.py"))

print("\n== M. 划线 / 批注（阶段 1，core/marks.py + 组件）==")
from core.models import Annotation, Conversation, Highlight  # noqa: E402

Highlight.objects.all().delete()
Annotation.objects.all().delete()

# —— validate_source 是唯一实现（任何写入口都过这里，别再手写判据）——
check("entry 去前导零（004907 → 4907）",
      marks.validate_source("entry", "004907")[:2] == ("entry", "4907"))
check("entry 非正整数拒", "正整数" in marks.validate_source("entry", "abc")[2])
check("report 小写归一（DAILY-… → daily-…）",
      marks.validate_source("report", "DAILY-2026-10-31")[1] == "daily-2026-10-31")
check("report 裸日期拒（必须带 daily-/weekly- 前缀）",
      "形如" in marks.validate_source("report", "2026-10-31")[2])
check("★ web 仍是枚举预留（阶段 4 才有剪藏对象，现在放行=造空引用）",
      "尚未启用" in marks.validate_source("web", "1")[2])
# ★ chat 2026-10-31 启用（对话里的总结/回复也能划线批注）：判据是「这条消息真的
#   在库里」，不是「id 长得像数字」—— 否则能造出指向不存在消息的引用。
check("★ chat 未落库的消息拒（凭空 id 不能写）",
      "不存在" in marks.validate_source("chat", "99999999")[2])
check("chat 非数字 id 拒", "正整数" in marks.validate_source("chat", "abc")[2])
_conv = Conversation.objects.create(source_type="report", source_id="daily-2026-10-31")
_msg = _conv.messages.create(role="assistant", body="一段回复")
check("chat 已存在的消息放行（id 去前导零）",
      marks.validate_source("chat", "%04d" % _msg.id)[:2] == ("chat", str(_msg.id)))
check("未知类型拒", "未知来源类型" in marks.validate_source("bogus", "1")[2])

# —— 增删查 ——
ok_, _ = marks.add_highlight("entry", "4907", "巨头用发债而不是配售", note="有意思")
check("划线落库", ok_ is True and Highlight.objects.count() == 1)
ok_, _ = marks.add_highlight("entry", "4907", "   ")
check("空摘录拒（划线最小语义=摘了一句原文）", ok_ is False)
ok_, _ = marks.add_annotation("report", "daily-2026-10-31", "这期结论偏乐观")
check("批注落库（report 源）", ok_ is True and Annotation.objects.count() == 1)
ok_, _ = marks.add_annotation("web", "1", "x")
check("web 批注拒", ok_ is False)

lst = marks.list_marks("entry", "4907")
check("list_marks 分组返回（划线/批注分开）",
      "highlights" in lst and "annotations" in lst and len(lst["highlights"]) == 1)
check("空 source 返回 err 不炸", marks.list_marks("", "")["err"] != "")

# —— 文本锚点：anchor 落库为 JSON，list_marks 解析回 dict ——
ok_, _ = marks.add_highlight("entry", "4907", "发债不稀释",
                             anchor='{"text":"发债不稀释","prefix":"巨头","suffix":""}')
check("★ anchor JSON 落库且 list_marks 解析回 dict",
      ok_ is True
      and marks.list_marks("entry", "4907")["highlights"][-1]["anchor"] == {"text": "发债不稀释", "prefix": "巨头", "suffix": ""})
ok_, _ = marks.add_highlight("entry", "4907", "垃圾锚点", anchor="不是json")
check("★ 非 JSON 的 anchor 落库时被清空（不留垃圾给渲染层）",
      marks.list_marks("entry", "4907")["highlights"][-1]["anchor"] is None)

# —— 编辑 ——
hid = marks.list_marks("entry", "4907")["highlights"][0]["id"]
ok_, err = marks.edit_mark("highlight", hid, "改过的备注")
check("编辑划线备注", ok_ is True and err == "")
check("编辑后 note 已更新",
      marks.list_marks("entry", "4907")["highlights"][0]["note"] == "改过的备注")
aid = Annotation.objects.first().id
ok_, err = marks.edit_mark("annotation", aid, "改过的批注")
check("编辑批注正文", ok_ is True)
ok_, err = marks.edit_mark("annotation", aid, "   ")
check("★ 批注编辑成空拒（body 空无意义）", ok_ is False and "不能为空" in err)

marks.delete_highlight(hid)
# ★ 只数这个 source 的行，不数全表：全表计数会因库里有别的内容而假失败
#   （"拿当前库状态当期望"是这类脚本最常见的假警报来源）。
check("删划线（删 hid 这条，该源剩 2 条）",
      len(marks.list_marks("entry", "4907")["highlights"]) == 2)
Highlight.objects.all().delete(); Annotation.objects.all().delete()

# —— 端点：失败把后端原话显示在片段里，而不是只 toast ——
rf2 = RequestFactory()
r = wiki_views.highlight(rf2.post("/wiki/highlight", {
    "source_type": "entry", "source_id": "4907", "quoted_text": "发债不稀释"}))
frag = r.content.decode()
check("highlight 端点返回 #marks-… 片段（htmx outerHTML 单一根）",
      'id="marks-entry-4907"' in frag and "发债不稀释" in frag)
check("★ HX-Trigger 是纯 ASCII（转义成 unicode 码点，HTTP 头 latin-1）",
      r.get("HX-Trigger", "").isascii())
r = wiki_views.highlight(rf2.post("/wiki/highlight", {
    "source_type": "web", "source_id": "1", "quoted_text": "x"}))
check("非法来源的错误显示在片段里（不吞）", "尚未启用" in r.content.decode())
r = wiki_views.highlight(rf2.post("/wiki/highlight", {
    "source_type": "entry", "source_id": "4907", "quoted_text": "  "}))
check("空摘录的错误显示在片段里", "不能为空" in r.content.decode())
check("★ 划线/批注只吃 POST（GET 不建行）",
      wiki_views.highlight(rf2.get("/wiki/highlight")).status_code == 405)
Highlight.objects.all().delete(); Annotation.objects.all().delete()

# —— 组件挂载：三处复用同一个 partial，锚 (source_type, source_id) ——
TOOLS_T = read("templates/wiki/_article_tools.html")
MARKS_T = read("templates/wiki/_marks.html")
check("组件 partial 锚定 source_type/source_id（不硬编码条目或报告）",
      "source_type" in TOOLS_T and "source_id" in TOOLS_T)
check("marks 容器 id 唯一稳定（= marks-<type>-<id>，htmx target）",
      'id="marks-{{ source_type }}-{{ source_id }}"' in MARKS_T)
check("★ source 信息走 data 属性（JS 读取后 POST，不走 hx-prompt 头）",
      'data-source-type="{{ source_type }}"' in MARKS_T
      and 'data-source-id="{{ source_id }}"' in MARKS_T
      and "hx-prompt" not in MARKS_T)
check("★ 组件不再有手填表单（选中正文即划线/批注，不拷贝到底部）",
      'name="quoted_text"' not in MARKS_T and "mark-add" not in MARKS_T)
check("★ 删除按钮走 data-mark-del（列表被 htmx 整块替换，事件委托）",
      'data-mark-del' in MARKS_T)
check("★ 列表只显示「正文里落不了位」的标记（undisplayed），不再做全量索引",
      "undisplayed" in MARKS_T and "h.id in undisplayed" in MARKS_T
      and "a.id in undisplayed" in MARKS_T)
check("★ 全部落位时整块不占位（.marks.is-empty 隐藏，不留一条空横线）",
      "is-empty" in MARKS_T and ".marks.is-empty" in CSS)
check("★ 失配/无锚点的标记 id 被收集（静默降级必须有出口，否则看不见也删不掉）",
      "undisplayed.append" in read("core/markrender.py")
      and "undisplayed" in read("core/marks.py"))
# ★ 顺序契约：必须先渲染正文拿到 undisplayed，再渲染列表 —— 反了列表永远是空的
# ★ 按 `def _mark_fragment(`（带左括号）切：只切 `def _mark_fragment` 会先命中
#   `def _mark_fragment_chat`，于是校验的是对话那条分支，断言当场失效。
_FRAG_SRC = read("apps/wiki/views.py").split("def _mark_fragment(")[1].split("def highlight")[0]
check("★ _mark_fragment 先算正文 undisplayed 再渲染列表（顺序反了列表恒空）",
      _FRAG_SRC.index("_render_body(") < _FRAG_SRC.index('"wiki/_marks.html"'))
check("★ 对话消息的标记走单独分支（消息级 source=chat），不混进文章路径",
      'source_type or "").strip() == "chat"' in _FRAG_SRC
      and "def _mark_fragment_chat" in read("apps/wiki/views.py"))
# —— 选中即标注（app.js + 正文 inline 高亮回填）——
check("app.js 监听 mouseup 浮出选中 toolbar", "marksOnSelect" in JS
      and 'addEventListener("mouseup", marksOnSelect)' in JS)
check("app.js 生成文本锚点（prefix/suffix 指纹，非 DOM 路径）",
      "marksAnchor" in JS and "prefix" in JS and "suffix" in JS)
check("app.js 走 /wiki/highlight 与 /wiki/annotation（带 anchor JSON）",
      '"/wiki/highlight"' in JS and '"/wiki/annotation"' in JS
      and "JSON.stringify(anchor)" in JS)
check("★ 划线可带批注 note（add_highlight 接受 note 字段）",
      "note" in read("core/marks.py"))
check("★ anchor 落库前校验为 JSON（前端传的锚点不二次解析判据）",
      "json.loads" in read("core/marks.py") and "_anchor_json" in read("core/marks.py"))
check("★ 新增 markrender：文本节点层面回填 inline 高亮（不破坏标签）",
      "apply_marks" in read("core/markrender.py")
      and "_text_nodes" in read("core/markrender.py"))
check("★ inline 高亮标签 class=wiki-hl（正文原地高亮，非底部列表）",
      'class="wiki-hl"' in read("core/markrender.py"))
check("CSS 有选中 toolbar 与 inline 高亮样式",
      all(x in CSS for x in (".mark-toolbar", ".mark-annotate-pop", ".wiki-hl")))
check("正文回填 inline 高亮：视图层调用 annotate_html（三处详情）",
      "annotate_html" in read("apps/reader/views.py")
      and "annotate_html" in read("apps/summary/views.py"))
ENTRY_DETAIL = read("templates/reader/_entry_detail.html")
DAILY_DETAIL = read("templates/summary/detail.html")
WEEKLY_DETAIL = read("templates/summary/weekly_detail.html")
check("★ 组件挂到三处：文章详情 + 日报详情 + 周报详情",
      'include "wiki/_article_tools.html"' in ENTRY_DETAIL
      and 'include "wiki/_article_tools.html"' in DAILY_DETAIL
      and 'include "wiki/_article_tools.html"' in WEEKLY_DETAIL)
check("文章详情锚 entry（source_type=entry）",
      'source_type="entry"' in ENTRY_DETAIL)
check("日报/周报详情锚 report（source_id 由视图算好传入，模板不拼字符串）",
      'source_type="report"' in DAILY_DETAIL and 'source_id=marks_source_id' in DAILY_DETAIL
      and 'source_type="report"' in WEEKLY_DETAIL)
check("CSS 有划线/批注组件样式（否则整篇级摘录没有视觉区隔）",
      all(x in CSS for x in (".marks", ".marks-head", ".mark-hl", ".mark-an")))
check("urls 注册 highlight / annotation / mark/delete",
      all(x in read("apps/wiki/urls.py") for x in
          ("wiki/highlight", "wiki/annotation", "wiki/mark/delete")))
check("★ 可编辑：urls 注册 mark/edit 且 core 有 edit_mark",
      "wiki/mark/edit" in read("apps/wiki/urls.py")
      and "edit_mark" in read("core/marks.py"))
check("★ 可编辑：列表项带 data-mark-edit（事件委托，列表被整块替换）",
      "data-mark-edit" in MARKS_T)

# —— 选中即标注的「不打断阅读」契约（二次迭代）——
#   用户实测三个问题：划线后跳顶部（整页 reload）、点批注无输入框（mousedown
#   冒泡时序）、删除不自然。根因都是前端用 fetch+location.reload 绕过 htmx。
#   契约：marks 端点返回「列表 + 正文」两个 OOB 片段，前端走 htmx 局部刷新。
WIKI_VIEWS = read("apps/wiki/views.py")
check("★ 正文 OOB：marks 端点返回 article-body 片段（htmx 局部刷新正文高亮）",
      'hx-swap-oob="true"' in WIKI_VIEWS and "article-body-" in WIKI_VIEWS)
check("★ 正文重渲染按类型分发（_render_body，不复制 reader/summary 判据）",
      "_render_body" in WIKI_VIEWS)
check("★ 正文容器带稳定 id（article-body-<type>-<id>，OOB 命中目标）",
      'id="article-body-entry-{{ entry.id }}"' in ENTRY_DETAIL
      and 'id="article-body-report-{{ marks_source_id }}"' in DAILY_DETAIL
      and 'id="article-body-report-{{ marks_source_id }}"' in WEEKLY_DETAIL)
check("★ 前端走 htmx 局部刷新（marks 交互不再 location.reload 整页跳顶）",
      "marksHxPost" in JS and "htmx.ajax(\"POST\"" in JS)
check("★ 批注按钮用 click+stopPropagation（修 mousedown 冒泡吞掉输入框）",
      'btnAn.addEventListener("click"' in JS and "stopPropagation" in JS)
check("★ 删除带确认（不可逆，先 confirm 再删）",
      "confirm(\"删除这条" in JS)
check("★ marks 端点 POST 带 CSRF 头（htmx.ajax headers）",
      'X-CSRFToken' in JS and "headers:" in JS)
check("★ OOB 正文片段保持原标签+class（div 换 article 会丢样式，实测坑）",
      '"report"' in WIKI_VIEWS and "hx-swap-oob" in WIKI_VIEWS
      and '"detail-content"' in WIKI_VIEWS and "hx-swap-oob=\"true\"" in WIKI_VIEWS)

# —— markrender：文本锚点回填 inline 高亮（纯函数，零 DB）——
from core import markrender  # noqa: E402
_hl = markrender.apply_marks(
    "<p>巨头用发债而不是配售，说明它们认为自己股价不便宜。</p>",
    [{"id": 9, "kind": "highlight",
      "anchor": {"text": "发债而不是配售", "prefix": "巨头用", "suffix": "说明它们"}}])
check("★ 文本锚点匹配后包进 wiki-hl 标签（inline 高亮）",
      '<mark class="wiki-hl" data-mark="9">发债而不是配售</mark>' in _hl, _hl)
check("批注用 wiki-hl-an 区分（正文红底，列表里看 body）",
      'wiki-hl-an' in markrender.apply_marks(
          "<p>股价不便宜</p>",
          [{"id": 8, "kind": "annotation", "anchor": {"text": "股价不便宜"}}]))
check("★ 锚点匹配不到 → 优雅降级（不报错、不包裹，仍在列表可见）",
      'wiki-hl' not in markrender.apply_marks(
          "<p>正文已变</p>",
          [{"id": 7, "kind": "highlight", "anchor": {"text": "找不到的句子"}}]))
check("★ 包裹不破坏既有标签（在文本节点层面匹配）",
      "<b>加粗</b>" in markrender.apply_marks(
          "<p><b>加粗</b>发债而不是配售</p>",
          [{"id": 6, "kind": "highlight", "anchor": {"text": "发债而不是配售"}}]))

# ---- 交互优化（第三轮）：hover 出文字 + 右上角删除、toolbar 跟鼠标 ----
_note_hl = markrender.apply_marks(
    "<p>股价不便宜</p>",
    [{"id": 5, "kind": "highlight", "anchor": {"text": "股价不便宜"},
      "text": '含"引号"与<b>标签</b>'}])
check("★ hover 文字转义后进 data-note（引号/< 不破坏属性）",
      'data-note="含&quot;引号&quot;与&lt;b&gt;标签&lt;/b&gt;"' in _note_hl, _note_hl)
check("无文字的标记不输出 data-note（前端据此不出空文字卡片）",
      "data-note" not in markrender.apply_marks(
          "<p>股价不便宜</p>",
          [{"id": 4, "kind": "highlight", "anchor": {"text": "股价不便宜"}}]))
check("★ 多行批注换行保留在 data-note（tooltip 用 pre-wrap 还原）",
      "第一行\n第二行" in markrender.apply_marks(
          "<p>股价不便宜</p>",
          [{"id": 3, "kind": "annotation", "anchor": {"text": "股价不便宜"},
            "text": "第一行\n第二行"}]))
check("★ toolbar 用鼠标坐标定位（不再放选区中点，省一次大位移）",
      "e.clientX" in JS and "hasPtr" in JS)
check("★ inline 高亮 hover 浮层：事件委托 + 延时收起（鼠标能挪到浮层上点删除）",
      'closest("mark.wiki-hl")' in JS and "marksHoverBuild" in JS
      and "MARKS_HOVER_HIDE" in JS)
check("★ hover 浮层文字走 textContent（用户文字绝不进 innerHTML）",
      "tx.textContent = text" in JS)
check("★ 按钮组是独立节点、贴高亮右上角（不跟文字卡片一起漂到远处）",
      "marksHoverBtns" in JS and "r.right + 3" in JS)

# ---- 交互优化（第四轮）：hover/点击出「编辑 + 删除」，就地改备注/批注 ----
#   用户实测：划线常是"先划、备注后补"，批注也可能要修；只有 × 时完全没有入口。
#   契约：inline 高亮与底部列表**共用同一个编辑弹框**，且没备注的划线也必须出按钮组。
check("★ 无备注的划线也出编辑按钮（先划后补备注才有入口，不能只出 ×）",
      JS.index("var editBtn = document.createElement") < JS.index("if (!text) return;"))
check("★ inline 编辑与列表编辑共用 marksOpenEditor（两套 UI 必漂）",
      "function marksOpenEditor" in JS
      and "marksOpenEditor({" in JS
      and JS.count("marksOpenEditor(") >= 3)
check("★ inline「编辑」提交到 /wiki/mark/edit（与列表同一端点，改 note/body 由后端定）",
      'marksOpenEditor({' in JS and 'marksHxPost("/wiki/mark/edit"' in JS)
check("★ 点击高亮也弹按钮组（触屏无 hover；快速点击常早于 150ms 延时）",
      'closest("mark.wiki-hl")' in JS and "mk !== marksHoverEl" in JS)
check("★ 编辑弹框被 Ctrl/回车保存后走同一局部刷新（不整页跳顶）",
      "marksHxPost(\"/wiki/mark/edit\"" in JS and "marksPop = pop" in JS)
check("CSS 有按钮组样式（编辑 + 删除同容器，位置只算一次）",
      all(x in CSS for x in (".mark-hover", ".mark-hover-text",
                             ".mark-hover-actions", ".mark-hover-del")))
check("★ hover 删除点一下就生效（路线与列表删除同为 /wiki/mark/delete）",
      'marksHxPost("/wiki/mark/delete"' in JS and "isAn ? \"annotation\" : \"highlight\"" in JS)
check("CSS 有 hover 浮层样式（卡片 / 文字 / 右上角删除按钮）",
      all(x in CSS for x in (".mark-hover", ".mark-hover-text", ".mark-hover-del")))

# ---- 锚点消歧（第五轮）：不假定「text+prefix+suffix 在全文唯一」----
#   用户指出：同一句话在正文里出现两次（并列段落 / 表格行 / "股价不便宜"这类短语），
#   指纹整体重复时旧实现直接废掉。旧 `_best_span` 更是「纯 text 命中即 return」，
#   prefix/suffix 只排在第 2、3 顺位，实际几乎从不生效 —— 划第二处、高亮落在第一处。
#   现在是「枚举全部候选 + 打分排序」，锚点加了 offset/nth 两个位置先验。
_DUP = ("<p>第一部分：分析师认为股价不便宜。</p>"
        "<p>第二部分：另一位分析师也认为股价不便宜，但理由是增长放缓。</p>")
_dup_out = markrender.apply_marks(
    _DUP, [{"id": 11, "kind": "highlight",
            "anchor": {"text": "股价不便宜", "prefix": "另一位分析师也认为",
                       "suffix": "，但理由是"}}])
check("★ 同一句出现两次：按上下文落在**第二处**（不能第一处命中就 return）",
      "也认为<mark class=\"wiki-hl\" data-mark=\"11\">股价不便宜</mark>" in _dup_out
      and "分析师认为股价不便宜" in _dup_out, _dup_out)

_REP = "<p>重复的一句话。</p><p>别的。</p><p>重复的一句话。</p>"
check("★ 整段完全重复（五字段里文字/上下文全同）：靠 offset 落到靠后那处",
      "别的。</p><p><mark" in markrender.apply_marks(
          _REP, [{"id": 12, "kind": "highlight",
                  "anchor": {"text": "重复的一句话", "offset": 20, "nth": 1}}]))
check("整段重复 + offset 指向开头 → 落在第一处（方向也对）",
      markrender.apply_marks(
          _REP, [{"id": 13, "kind": "highlight",
                  "anchor": {"text": "重复的一句话", "offset": 0, "nth": 0}}]).startswith("<p><mark"))
check("★ 同一段里同一字重复 3 次：nth 决定落哪一个",
      "好，好，<mark" in markrender.apply_marks(
          "<p>好，好，好。</p>",
          [{"id": 14, "kind": "highlight", "anchor": {"text": "好", "nth": 2, "offset": 4}}]))
check("★ 选区跨标签（甲<b>乙</b>丙 选「甲乙丙」）：现在能包住，不再整条失配",
      "<mark class=\"wiki-hl\" data-mark=\"15\">甲<b>乙</b>丙</mark>" in markrender.apply_marks(
          "<p>甲<b>乙</b>丙</p>",
          [{"id": 15, "kind": "highlight", "anchor": {"text": "甲乙丙"}}]))
check("★ 正文里的 &nbsp; 解回空白再比对（Miniflux 正文遍地实体，旧实现永远匹配不上）",
      "<mark class=\"wiki-hl\" data-mark=\"16\">营收&nbsp;增长&nbsp;放缓</mark>"
      in markrender.apply_marks(
          "<p>营收&nbsp;增长&nbsp;放缓</p>",
          [{"id": 16, "kind": "highlight", "anchor": {"text": "营收 增长 放缓"}}]))
check("★ 跨段落的选区不硬包（<mark> 会被 </p> 切断，留下孤儿标签）→ 交给底部兜底",
      "<mark" not in markrender.apply_marks(
          "<p>上半句</p><p>下半句</p>",
          [{"id": 17, "kind": "highlight", "anchor": {"text": "上半句下半句"}}]))
_overlap_und = []
_overlap = markrender.apply_marks(
    "<p>分析师认为股价不便宜。</p>",
    [{"id": 18, "kind": "highlight", "anchor": {"text": "分析师认为股价"}},
     {"id": 19, "kind": "annotation", "anchor": {"text": "股价不便宜"}, "text": "注释"}],
    _overlap_und)
check("★ 两条标记区间重叠只落第一条，第二条进 undisplayed（嵌套 mark 会让删改错位）",
      _overlap.count("<mark") == 1 and _overlap_und == [19],
      (_overlap, _overlap_und))
check("★ 前端锚点带 offset/nth（位置先验靠它，只有三字段等于退回旧行为）",
      "offset = before.length" in JS and "nth = n" in JS
      and "{ text: text, prefix: prefix, suffix: suffix, offset: offset, nth: nth }" in JS)
check("★ 前后文指纹 32 字（16 字在雷同段落里分不开）",
      "MARKS_PREFIX_LEN = 32" in JS)
check("★ 不打包票唯一：源码里明确记录「整段完全重复」是文本锚点的上限",
      "理论上限" in read("core/markrender.py"))

# ---- 老锚点自动补全（否则已有划线永远停在弱指纹上，缺陷只修了新数据）----
_res = []
markrender.apply_marks(
    _DUP, [{"id": 20, "kind": "highlight",
            "anchor": {"text": "股价不便宜", "prefix": "另一位分析师也认为",
                       "suffix": "，但理由是"}}], None, _res)
_plain_dup = markrender._stream(_DUP)[0]
_occ = markrender._occurrences(_plain_dup, "股价不便宜")
check("★ 三字段老锚点落位后回报 offset/nth（供存储层补齐，一次性）",
      len(_res) == 1 and _res[0]["offset"] == _occ[1] and _res[0]["nth"] == 1, (_res, _occ))
check("★ 补全用**落位那一处**的位置，不是全文第一次出现的位置",
      _res and _res[0]["offset"] != _occ[0], (_res, _occ))
_res2 = []
markrender.apply_marks(
    _DUP, [{"id": 21, "kind": "highlight",
            "anchor": {"text": "股价不便宜", "offset": _occ[1], "nth": 1}}], None, _res2)
check("已有 offset 的锚点不再回报（否则每次渲染都写一次库）", _res2 == [], _res2)
check("★ annotate_html 落库时补锚点，且失败不影响渲染",
      "_backfill_anchors" in read("core/marks.py")
      and "update_fields=[\"anchor\"]" in read("core/marks.py")
      and "except Exception" in read("core/marks.py"))

# ---- 行内标签配平（第六轮补丁）：`**江山时序**：…` 只亮半句的真凶 ----
#   md 的 `**加粗**` 渲染成 <strong>，用户从加粗字里选到加粗字外时，
#   <mark> 开在 <strong> 内、闭在 <strong> 外 = 非法嵌套；浏览器按收养算法从
#   </strong> 处强行闭合 <mark>，末尾 </mark> 成孤儿 → 只亮前半句。
_bal = markrender.apply_marks(
    "<li><strong>江山时序</strong>：纯原生 JS 的宣纸风格中国历史交互地图</li>",
    [{"id": 30, "kind": "highlight",
      "anchor": {"text": "江山时序：纯原生 JS 的宣纸风格中国历史交互地图"}}])
check("★ 起点落在 <strong> 内、终点在外：配平后整句一个 mark（不是被 </strong> 截断）",
      _bal.count("<mark") == 1
      and '<mark class="wiki-hl" data-mark="30"><strong>江山时序</strong>' in _bal
      and _bal.index("</mark>") > _bal.index("地图"), _bal)
check("★ 终点落在 <strong> 内、起点在外：终点推到 </strong> 之后（不能提前闭合）",
      "<mark class=\"wiki-hl\" data-mark=\"31\">字<strong>重点</strong></mark>"
      in markrender.apply_marks(
          "<p>文字<strong>重点</strong>尾</p>",
          [{"id": 31, "kind": "highlight", "anchor": {"text": "字重点"}}]))
check("★ 起点落在 <a> 内：起点回退到 <a> 之前（链接里的字不能只亮一半）",
      '<mark class="wiki-hl" data-mark="32"><a href="#">链接</a>后面</mark>'
      in markrender.apply_marks(
          '<p><a href="#">链接</a>后面</p>',
          [{"id": 32, "kind": "highlight", "anchor": {"text": "接后面"}}]))
check("★ 两层嵌套（strong > em）也能一路配平",
      "<mark class=\"wiki-hl\" data-mark=\"33\"><strong>甲<em>乙</em>丙</strong>丁</mark>"
      in markrender.apply_marks(
          "<p><strong>甲<em>乙</em>丙</strong>丁</p>",
          [{"id": 33, "kind": "highlight", "anchor": {"text": "乙丙丁"}}]))
check("配平不得把区间扩成跨块（宁可整条不显示，也不能留孤儿标签）",
      "<mark" not in markrender.apply_marks(
          "<p>上半句</p><p>下半句</p>",
          [{"id": 34, "kind": "highlight", "anchor": {"text": "上半句下半句"}}]))

# ---- 并入 / 替换草稿审批：候选要区分「本次新增」与「历史已在页里」（2026-11-13）----
#   ★ 并入一个已经积累了很多候选的页时，审批者面对一整页不分新旧，没法聚焦本批
#     真正新来的判断。判据只看源码事实：视图把 new/historical 拆分交给模板，模板
#     给新增候选打「新增」徽标、CSS 给卡片加蓝边；任一被误删都能被这条守住。
_VIEWS = read("apps/wiki/views.py")
_WORKAREA = read("templates/wiki/_approval_workarea.html")
check("★ 并入/替换：视图把新增/历史候选数交给模板（merge_new_n / merge_hist_n）",
      "merge_new_n" in _VIEWS and "merge_hist_n" in _VIEWS)
check("★ 并入/替换：模板按 is_new 给新增候选打「新增」徽标",
      "badge-new" in _WORKAREA and "c.is_new" in _WORKAREA)
check("★ 并入/替换：CSS 给新增候选卡加视觉区隔（蓝边 + 徽标底色）",
      ".badge-new" in CSS and ".cand-card.is-new" in CSS)

# ---- 2026-11-15：相关页推荐「默认调用」（自动 POST，渲染仍零 LLM） ----
AUTO_FORM = _form_tag(WORKAREA_T, 'id="related-auto-form"')
AUTO_BLOCK = (WORKAREA_T.split('id="related-auto-form"')[1].split("</form>")[0]
              if 'id="related-auto-form"' in WORKAREA_T else "")
AUTO_IF = re.search(r'\{% if related_state != "fresh"[^%]*%\}', WORKAREA_T)
RELATED_FN = _fn_src("approval_related", VIEWS_T)
CTX_FN = _fn_src("_approval_workarea_ctx", VIEWS_T)
check("★ 默认调用：自动表单用 load 触发同一个 POST（不是渲染里起模型）",
      bool(AUTO_FORM) and 'hx-trigger="load"' in AUTO_FORM
      and 'hx-post="/wiki/approval/related"' in AUTO_FORM
      and 'hx-target="#approval-workarea"' in AUTO_FORM
      and '{% csrf_token %}' in AUTO_BLOCK
      and 'name="slug" value="{{ slug }}"' in AUTO_BLOCK
      and 'name="auto" value="1"' in AUTO_BLOCK,
      AUTO_FORM[:160])
check("★ 自动表单排在手动按钮之后（既有 RELATED_FORM 守卫仍切到手动那份）",
      bool(AUTO_FORM) and 'id="related-auto-form"' in WORKAREA_T
      and WORKAREA_T.index('id="related-auto-form"')
      > WORKAREA_T.index('id="related-refresh"'))
check("★★ 防循环：四道闸齐全（fresh / err / mutex / related_auto）",
      bool(AUTO_IF) and all(x in AUTO_IF.group(0) for x in (
          'related_state != "fresh"', 'not related_err',
          'not mutex_msg', 'not related_auto')),
      AUTO_IF.group(0) if AUTO_IF else "NO-IF")
check("★★ 三处 return 都显式给 related_auto=False（缺变量会被模板当空值）",
      bool(CTX_FN) and CTX_FN.count('"related_auto": False') == 3,
      CTX_FN.count('"related_auto": False'))
check("★★ 自动 POST 先查缓存再花一次钱（OOB 反复点火不重复花钱）",
      bool(RELATED_FN) and 'request.POST.get("auto")' in RELATED_FN
      and 'wiki_related.load(slug, info)[1] == "fresh"' in RELATED_FN
      and RELATED_FN.index('wiki_related.load(')
      < RELATED_FN.index('wiki_related.recommend('),
      "缺缓存短路 = 刷一次花一次钱")
check("★★ 防循环闸门：本视图所有回包都过 respond 打 related_auto=True",
      bool(RELATED_FN) and 'ctx["related_auto"] = True' in RELATED_FN
      # HttpResponse 只允许在 respond 一处；任何新增回包都必须走 respond，
      # 否则那个片段里 related_auto 还是 False → 自动表单重新点火。
      and RELATED_FN.count('HttpResponse(') == 1
      and RELATED_FN.count('return respond(ctx)') >= 3
      and RELATED_FN.count('recommend(slug)') == 1,
      "有绕过 respond 的返回 = 回包会重新自动点火")
check("★★ 渲染路径仍零 LLM（默认调用 ≠ 把模型塞进 13 个渲染点）",
      bool(CTX_FN) and 'wiki_related.load(' in CTX_FN
      and not any(x in CTX_FN for x in ('recommend(', '_call(')),
      "13 个渲染点任何一个起模型都是刷一次花一次钱")
check("★ 自动失败不静默重试、成本可见（用户定的边界）",
      bool(RELATED_FN) and '不再自动重试' in RELATED_FN
      and '已自动查过（一次模型调用）' in RELATED_FN
      and '已过期' in WORKAREA_T and '重新查一次' in WORKAREA_T)

# ---- 2026-11-23：相关页推荐 2→5 条（用户：多显示几个反而减少心智负担） ----
RELATED_T = read("core/wiki_related.py")
SETTINGS_T = read("config/settings.py")
check("★ 相关页上限放宽到 5（固定 2 条太窄）",
      "RELATED_MAX = 5" in RELATED_T)
check("★★ 上限只有一处真值：prompt 插值，不留写死的「最多 2 条」",
      "最多列 {MAX} 条" in RELATED_T
      and '.replace("{MAX}", str(RELATED_MAX))' in RELATED_T
      and "最多 2 条" not in RELATED_T)
check("★★ 上限进指纹（放宽后旧缓存自动 stale，否则浮层仍显示旧 2 条）",
      '"limit": RELATED_MAX' in RELATED_T)
check("★ 预算随条数抬到 4096（3072 历史 85 次撞顶 1 次）",
      '"WIKI_RELATED_MAX_TOKENS", "4096"' in SETTINGS_T)

# ------------------------------------------------ 对话长文分片（2026-11-15）
# 用户报的原话：「正文超 6000 字被截断……长文总结不可用了」。
# ★ 行为判据在 `core/tests/test_conversation_chunked.py`（那边验「全文真被读到」）。
#   这里钉**源码形状**，防的是另一类事故：有人为了省调用把 `ask()` 改回单次，
#   顺手把单测也改回「断言截断」—— 那样行为判据会跟着一起绿。
CONV_FILE = os.path.join(BASE, "core", "conversation.py")
CONV_T = Path(CONV_FILE).read_text(encoding="utf-8") if os.path.exists(CONV_FILE) else ""
ASK_FN = _fn_src("ask", CONV_T)
MATERIAL_FN = _fn_src("_material", CONV_T)
CHUNKED_FN = _fn_src("_ask_chunked", CONV_T)
SPLIT_FN = _fn_src("split_body", CONV_T)
BALANCE_FN = _fn_src("_balance_quotes", CONV_T)

check("★★ 对话链路里不再有正文截断调用（clip_quotes 已从 ask/_material/分片里消失）",
      bool(ASK_FN) and bool(MATERIAL_FN) and bool(CHUNKED_FN)
      and not any("reporttext.clip_quotes(" in x
                  for x in (ASK_FN, MATERIAL_FN, CHUNKED_FN)),
      "任何一处重新剪正文 = 长文后半篇又没了")
check("★★ ask() 先切片再分流：单片走老路径，多片走分片汇总",
      bool(ASK_FN) and "split_body(" in ASK_FN
      and "len(chunks) == 1" in ASK_FN
      and "_ask_chunked(blk, body, chunks" in ASK_FN,
      "缺分流 = 要么长文仍截断，要么短文也走多片白花钱")
check("★ 分片路径恰好两次调用点：逐片取材 + 一次汇总",
      bool(CHUNKED_FN) and CHUNKED_FN.count("citeguard._call(") == 2,
      CHUNKED_FN.count("citeguard._call("))
check("★★ 分片与汇总共用同一份 stats（预算闸才能一片撞顶、后面全停）",
      bool(CHUNKED_FN) and CHUNKED_FN.count("_stats()") == 1,
      "两次 _stats() = 预算闸各自为政，撞顶后照样继续花钱")
check("★ 分片调用上限留出「每次调用可翻倍重试」的余量（2*(n+1)）",
      bool(CHUNKED_FN) and "2 * (n + 1)" in CHUNKED_FN)
check("★★ split_body 是精确划分：按切点切片、拼回原文（不是取前缀）",
      bool(SPLIT_FN) and "body[prev:cut]" in SPLIT_FN
      and "min(cap" in SPLIT_FN
      and "body[:" not in SPLIT_FN,
      "出现 body[:N] = 又变成截断")
check("★★ 单片引文边界按**深度**补（含「本片起点已在引文内」）",
      bool(BALANCE_FN) and "starts_inside" in BALANCE_FN
      and "_quote_depth(body[:pos])" in CHUNKED_FN
      # 算出来还得**传进去**：变异测试里只删调用点的实参，前两条依然成立。
      and "starts_inside))" in CHUNKED_FN,
      "只按本片内标记数配对 ⇒ 引文中间那片 0/0 假平衡；参数漏传同样会漏")
check("★★ 取材片不下结论（否则汇总 = 拼 N 份以偏概全的总结）",
      "不要下全文结论" in CONV_T and "分片取材，不是最终作答" in CONV_T)
check("★★ 某片失败要三处声明缺口（笔记 / 汇总 prompt / 回答开头）",
      bool(CHUNKED_FN) and "本片取材失败" in CHUNKED_FN
      and "片没读到" in CHUNKED_FN
      and "有 %d 片取材失败" in CONV_T)

print("\n== O. 只收高质量信号（2026-11-16）==")
INGEST_FILE = os.path.join(BASE, "core", "ingest.py")
INGEST_T = Path(INGEST_FILE).read_text(encoding="utf-8") if os.path.exists(INGEST_FILE) else ""
INVENTORY_FN = _fn_src("inventory", INGEST_T)
EXTRACT_FN = _fn_src("extract", INGEST_T)
EXTRACT_THEN_FN = _fn_src("extract_then_compile", INGEST_T)
# ★ 剥掉注释行再判：解释「不要重写 'draftbatch'」的注释本身含这个字样，
#   按子串查会把说明当成复发（假红）。同理，注释里提到 APPROVAL_SOURCE_TYPE
#   也不等于代码真的用了它 —— 变异测试 B（把赋值改成 "entry"）就是这么
#   骗过初版守卫的：字样还在注释里，守卫全绿。
INVENTORY_CODE = "\n".join(
    ln for ln in INVENTORY_FN.splitlines() if not ln.lstrip().startswith("#"))
URLS_T = read("config/urls.py") + read("apps/wiki/urls.py")
INGEST_RUN_FN = _fn_src("ingest_run", VIEWS_T)

# ★ 用户 2026-11-16：「审批对话压根不该进待提炼清单」。
#   真实库实测：`draftbatch:current`（37 条消息 / 14 条是用户的话）会成为
#   `inventory()` 里**唯一**的 todo，于是每次点编译都白跑一次 extract、
#   报一条「1 处没提炼成：draftbatch:current」，永远消不掉（失败不写台账
#   ⇒ 永远 state=new），还占掉 EXTRACT_MAX_SOURCES 一个名额。
check("★★ 审批对话不进待提炼清单（inventory 扫对话时跳过 draftbatch）",
      bool(INVENTORY_CODE)
      and "APPROVAL_SOURCE_TYPE" in INVENTORY_CODE
      and "if c.source_type == approval_type" in INVENTORY_CODE
      and "continue" in INVENTORY_CODE,
      "不跳过 ⇒ 每轮编译都报「1 处没提炼成」且白占提炼名额")
check("★ 跳过判据取 wiki_agent 的常量（注释里提到不算，要看赋值形状）",
      bool(INVENTORY_CODE)
      and "approval_type = wag.APPROVAL_SOURCE_TYPE" in INVENTORY_CODE
      and '"draftbatch"' not in INVENTORY_CODE
      and "'draftbatch'" not in INVENTORY_CODE,
      "同一口径两处写 = 改一处漏一处；写死或写错来源都会跳错东西")

# ★ 用户 2026-11-16：「文章的发送到 wiki 的入口还是隐藏掉，文章内的噪声太大了，
#   考虑 wiki 只接收划线、批注等高质量内容」。按**形状**判，不按字样 ——
#   两处注释里都写着「发送到 wiki」，按子串查会把自己写的说明当成入口。
check("★★ 阅读列表不再有「发送到 wiki」入口（菜单项与 handler 一起撤）",
      not re.search(r'act:\s*["\']send-to-wiki["\']', JS)
      and not re.search(r'item\.act\s*===\s*["\']send-to-wiki["\']', JS)
      and 'postForm("/wiki/ingest"' not in JS,
      "入口复活 = 一点就走「整篇正文提候选」的低质量路径")
check("★ 后端 /wiki/ingest 仍在（脚本/测试用），且 force 默认 False",
      "wiki/ingest" in URLS_T
      and bool(EXTRACT_FN) and "force: bool = False" in EXTRACT_FN,
      "撤 UI ≠ 删能力；但也绝不能把 force 变成默认开")
check("★ ingest_run 写明撤销原因（不是悄悄少了个按钮）",
      "发送到 wiki" in INGEST_RUN_FN and "2026-11-16" in INGEST_RUN_FN,
      "读代码的人得知道「为什么没有这个入口」")
check("★ 一条龙里的 extract 不带 force（只收高质量信号）",
      bool(EXTRACT_THEN_FN)
      and 'extract(r["source_type"], r["source_id"])' in EXTRACT_THEN_FN
      and "force=True" not in EXTRACT_THEN_FN,
      "带 force = 没划线也能从正文提候选，正是 2026-11-16 关掉的那条路")

# ================================================== P. 标签收敛与可见（2026-11-16）
# ★ 用户：「wiki 除了分类之外，之前有探讨过 tag 作为补充」→ 先做收敛、再做展示。
#   背景事实（只读实测）：真实库 32 页全带 3–4 个标签，但**界面上一个都没显示**，
#   且 96 个词里 93 个只出现一次 —— 词表没收敛，faceted 筛选做出来也筛不出东西。
TAG_POOL_FN = _fn_src("_tag_pool", INGEST_T)
PLAN_PAYLOAD_FN = _fn_src("_plan_payload", INGEST_T)
PLAN_GROUPS_FN = _fn_src("plan_groups", INGEST_T)
# ★ 首页与 htmx 片段共用的那个 `_ctx`（不是审批工作区的 `_approval_workarea_ctx`，
#   `_fn_src` 认 `^def 名字(` 全字匹配，两者不会串）。
PAGE_CTX_FN = _fn_src("_ctx", VIEWS_T)
_S1_PROMPT_M = re.search(r'COMPILE_S1_SYSTEM = """(.*?)"""', INGEST_T, re.S)
S1_PROMPT = _S1_PROMPT_M.group(1) if _S1_PROMPT_M else ""

check("★★ _tag_pool 按使用次数降序、且有上限（顺序即「哪些词常用」的信号）",
      bool(TAG_POOL_FN) and "COMPILE_TAG_POOL" in TAG_POOL_FN
      and "tag_counts(pages" in TAG_POOL_FN
      and "-kv[1]" in _fn_src("tag_counts", CORE_T),
      "不排序 = 池里谁先谁后随机，模型看不出哪些词常用，收敛就没有着力点；"
      "排序改成去 `wstore.tag_counts` 一处实现（审批页下拉读同一份）")
check("★★ 空池**不写** tag_pool 键（空与没有是两个事实）",
      bool(PLAN_PAYLOAD_FN)
      and re.search(r"if tag_pool:\s*\n\s*obj\[\"tag_pool\"\]", PLAN_PAYLOAD_FN) is not None,
      "无条件写 `tag_pool: []` ⇒ 模型以为「库里有标签只是没给我看」，照样自由造词")
check("★★ S1 材料真的带上池（且用全量 pages，不是只列 80 页的 catalog）",
      bool(PLAN_GROUPS_FN) and "_tag_pool(pages)" in PLAN_GROUPS_FN
      and "_tag_pool(catalog" not in PLAN_GROUPS_FN
      # ★ 不能用 `[^)]*`：参数里有 `len(full_catalog)`，一遇到那个右括号就断，
      #   守卫会假红（本轮真踩到）—— 改成允许跨行的有限宽度。
      and re.search(r"_plan_payload\(.{0,160}?tag_pool", PLAN_GROUPS_FN, re.S) is not None,
      "只统计最近 80 页 = 老页的词汇被无视；不传进去 = 池白算")
check("★ prompt 要求「逐字照抄」，并交代「没有这个字段」的情形",
      # ★ 必须锁「标签语境」：prompt 里「逐字照抄」共 3 处，另两处讲的是 slug /
      #   target（`"existing:<slug>"` 要逐字照抄）。只查全文任意一处 ⇒ 把标签这句
      #   改成「尽量对齐」后，另两句照样让守卫绿（变异 D 当场揭穿）。
      any("逐字照抄" in ln and "tag_pool" in ln for ln in S1_PROMPT.splitlines())
      and "没有** `tag_pool` 这个字段" in S1_PROMPT,
      "不写「照抄」模型仍会换近义词；不写「没有字段」它会把空库当成不许用标签")
check("★ 复用/新造有程序事实（不能只是一句提示词承诺）",
      # ★ 必须认出「notes.append 里带『新造』」这个**形状**：只查全函数有没有
      #   「新造」两个字，把统计改成局部变量（不再 append）照样绿 —— 变异 E 揭穿。
      re.search(r'notes\.append\("标签：[^"]*新造', PLAN_GROUPS_FN) is not None,
      "没人统计就没人知道膨胀还在不在发生（真实库已经 93/96 只出现一次）")
check("★ 首页页表渲染标签（最多 4 个 + 诚实的「+N」）",
      'class="wiki-tag"' in BODY_T and "p.tags|slice" in BODY_T
      and "add:\"-4\"" in BODY_T,
      "截断不报数 = 静默少显示；不截断 = 一排 8 个把后面几列挤出屏幕")
check("★ 详情页头部渲染标签", 'class="wiki-tag"' in PAGE_T and "pg.fm.tags" in PAGE_T,
      "标签只在列表可见、详情页看不见，点进去就断了")
check("★★ 标签是筛选链接（`?tag=` 真存在，两处 chip 都得可点）",
      # ★ 认 `<a class="wiki-tag" href="/wiki?tag=` 这个**形状**，不是「文件里有没有
      #   href」。上一版这里写的是反向判据（`not in`）—— 那时 `?tag=` 确实不存在；
      #   筛选做了之后，同一句话就从「守正确」变成「守错误」：旧判据不删，改产品的人
      #   会被它挡住；删了不换，就没人守新行为。
      'a class="wiki-tag" href="/wiki?tag=' in BODY_T
      and 'a class="wiki-tag" href="/wiki?tag=' in PAGE_T
      and 'span class="wiki-tag"' not in BODY_T
      and 'span class="wiki-tag"' not in PAGE_T,
      "点不动的 chip 等于把标签唯一的职能（横切聚合）阉掉；残留 span = 有的能点有的不能")
check("★★ `?tag=` 在上下文里真过滤（不是接了参数就丢）",
      # 只查「有 tag 变量」会被空壳骗：接了参数不筛，页数照样是 32，人以为标签没数据。
      re.search(r'if tag:\s*\n\s*pages = \[p for p in pages if tag in', PAGE_CTX_FN)
      is not None,
      "接了 tag 不筛 = 点标签没反应，比不可点更让人以为库坏了")
check("★ 空结果分因（「库里没页」≠「没页带这个词」）",
      "没有页带「{{ tag }}」这个标签了" in BODY_T and "pages_all_total" in BODY_T,
      "混成一句「还没有入库的页」会让人以为数据丢了")

print("\n== Q. 审批页就地改标签（2026-11-16）==")
# 用户要求：「审核页面应该有编辑 tags 的地方，参考微信好友 tag —— 输入 tag 的时候
# 要能选择已有的 tag」。UI 只占一半，要害在三条：①收敛判据只有一份；②chip 的 HTML
# 只有一份（初始渲染与 POST 回包共用）；③前端换的节点 id / 端点地址与模板和路由一致。
# 行为判据在 core/tests/test_approval_tags.py（20 条），这里钉源码形状。
def _no_hash_comments(src):
    """剥掉整行 `#` 注释再判形状。

    ★ 2026-11-16 变异 C 的教训：`_tag_pool` 里那句注释本身写着「`not pages` 不是
      ……」，于是删掉真正的 `not pages` 判据后守卫照样绿。**注释里提到 ≠ 代码里有**，
      这一段的每个判据都得先过这一层（同 11-11「守卫只认形状不认注释字样」）。
    """
    return "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))


def _no_tpl_comments(src):
    """剥掉 `{% comment %}…{% endcomment %}` 再判形状（变异 D：id 的说明写在注释里）。"""
    return re.sub(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}", "", src, flags=re.S)


WA_T = read("core/wiki_agent.py")
CLEAN_FN = _fn_src("clean_tags", WA_T)
SET_FIELD_FN = _fn_src("_set_field", WA_T)
SET_FM_FN = _fn_src("_set_frontmatter", WA_T)
WA_CTX_FN = _fn_src("_approval_workarea_ctx", VIEWS_T)
TAGS_FN = _fn_src("approval_tags", VIEWS_T)
TAGS_T = read("templates/wiki/_approval_tags.html")
CHIPS_T = read("templates/wiki/_approval_tags_chips.html")
WORKAREA_T = read("templates/wiki/_approval_workarea.html")

check("★ 收敛判据只有一份：两条写盘路径都调 clean_tags",
      bool(CLEAN_FN) and "clean_tags(val)" in SET_FIELD_FN
      and 'clean_tags(fields["tags"])' in SET_FM_FN,
      "只拦一条 = 留了个后门（模型恰好用 fields 写法就能写坏列表）")
check("★★ 超限拒收而不是截断", "最多" in CLEAN_FN and "TAG_MAX_N" in CLEAN_FN
      and "[:TAG_MAX_N]" not in CLEAN_FN,
      "静默截断会让人以为第 9 个标签生效了")
check("★★ clean_tags 必须用 builtins.set：本模块的 `set` 是函数",
      "builtins.set()" in CLEAN_FN
      and re.search(r"(?<![.\w])set\(\)", CLEAN_FN) is None,
      "写 set() 会 TypeError: missing 2 required positional arguments（本轮真踩）")
check("★★ 空 pages 的池子必须为空，不许回落去读真实库",
      "not pages" in _no_hash_comments(TAG_POOL_FN)
      and "tag_counts(pages" in TAG_POOL_FN,
      "None 交给 tag_counts 会现读真实库 —— 空库跑出 40 个真实标签（全量单测抓到过）")
check("★ 标签计数只有一份（_tag_pool 不许自己数）", "Counter(" not in TAG_POOL_FN,
      "两处各数一遍迟早出现「S1 的词表和人看到的不是一套」")

_chip_tpls = [
    p for p in glob.glob(os.path.join(BASE, "templates", "**", "*.html"),
                         recursive=True)
    if 'class="tag-chip"' in Path(p).read_text(encoding="utf-8")]
check("★★ chip 的 HTML 只有一份（初始渲染与 POST 回包共用）",
      len(_chip_tpls) == 1
      and _chip_tpls[0].endswith("_approval_tags_chips.html"),
      "两处各拼一遍 ⇒ 样式/data-tag 漂开，症状是「刚打开能删、改一次就点不动」: %r"
      % _chip_tpls)
check("★ 标签编辑器在工作区里，删页提案不显示",
      re.search(r"{% if not delete %}(.*?){% endif %}", WORKAREA_T, re.S) is not None
      and "_approval_tags.html" in
      re.search(r"{% if not delete %}(.*?){% endif %}", WORKAREA_T, re.S).group(1),
      "页都要没了，改它的标签没有去处")
check("★ 词表/上限/编译中三样都进了模板",
      "data-vocab=" in TAGS_T and "data-max=" in TAGS_T
      and "mutex_msg" in TAGS_T and "disabled" in TAGS_T,
      "缺 data-vocab ⇒ JSON.parse 抛错、下拉永远空（静默降级）")
check("★★ 前端换的节点 id 与模板一致",
      'id="tag-chips"' in _no_tpl_comments(CHIPS_T)
      and 'id="tag-chips"' in JS,
      "id 不一致 ⇒ 回包换不上，点 × 看起来没反应")
check("★ 前端 POST 的地址在路由里注册了",
      '"/wiki/approval/tags"' in JS and "wiki/approval/tags" in URLS_T,
      "地址漂开 ⇒ 每个动作都 404，而 toast 只会说「没保存上」")
check("★ 端点只吃 POST（GET 改文件 = 能被预取/预览链接误触发）",
      re.search(r"@require_POST\s*\ndef approval_tags\(", VIEWS_T) is not None)
check("★ 端点先过编译锁，且不复制「最多几个」这类判据",
      "_compile_running_guard()" in TAGS_FN and "TAG_MAX_N" not in TAGS_FN
      and 'wa.set("tags"' in TAGS_FN,
      "判据复制一份就会漂（页面放行、写盘拒绝）")
check("★ 草稿读不到回 404，绝不回空 chip 集", "404" in TAGS_FN
      and 'wa.read("draft"' in TAGS_FN,
      "回 200 + 空 chips 看起来就是「标签被清空了」")
check("★★ 空态分支也带标签键（缺 key ⇒ data-vocab 为空 ⇒ 前端抛错）",
      _no_hash_comments(WA_CTX_FN).count("tag_vocab_json") >= 2
      and "empty_tags" in WA_CTX_FN,
      "模板缺上下文变量会被 Django 当空值，静默走进坏状态")

print("\n== R. 标签总览页（2026-11-16 晚）==")
# 用户：「我全部重新过了一下 tag，已调整，接下来呢？」
# 实测：32 页 / 93 实例 / 68 词，其中 56 个词只用过 1 次。**看不见分布就没法继续
# 收敛** —— 逐页翻 32 次才知道「AI风险 和 安全 是不是同一批页」。这一页把分布摊开。
TAGPAGE_FN = _fn_src("tags", VIEWS_T)
TAGPAGE_T = read("templates/wiki/tags.html")

check("★★ 总览页按「≥2 次 / 只 1 次」分组，判据是程序给的",
      # ★ 认 `r["n"] > 1` 这个**形状**：只查「有 multi / single 两个名字」会被实现骗
      #   —— 分组条件写成 `n > 0` 照样有两个名字，但那一组会是空的。
      'r["n"] > 1' in TAGPAGE_FN and 'r["n"] == 1' in TAGPAGE_FN,
      "「共用」与「只用过一次」的处理方式完全不同：前者能当导航，后者是待观察池")
check("★★ 排序来自 tag_counts（一次性的在最后，不是随机）",
      # 自己再 Counter 一遍，迟早与 tag_counts 的顺序不一致 —— 那就成了第二份口径，
      # 编译期的池子和人对着一张假词表收敛。
      "tag_counts(pages)" in TAGPAGE_FN and "sorted(" not in TAGPAGE_FN,
      "另写一份排序 = 词表两个口径（同 Q 段「tag_counts 单一来源」）")
check("★★ 复用率不除零（空库要显示「—」而不是崩）",
      re.search(r"reuse_pct.*if inst else None", TAGPAGE_FN, re.S) is not None,
      "0 实例的库一打开总览页就 500，最容易被当成「功能坏了」")
check("★★ 每个词下面挂「哪些页」（判断该不该合靠它）",
      # ★ 必须认出「赋值的来源是 by_tag」这个形状：只要出现 `r["pages"]` 就算数，
      #   把实现改成 `r["pages"] = []` 照样绿 —— 变异 E 揭穿（同 11-16 早
      #   「守卫只查字样不认形状」的第三次复现）。
      'r["pages"] = by_tag.get(' in TAGPAGE_FN and "r.pages" in TAGPAGE_T,
      "只报页数看不出「AI风险 2 页」和「安全 2 页」是不是同一批页")
check("★★ 两组都渲染，且各自有空态分支",
      "multi" in TAGPAGE_T and "single" in TAGPAGE_T
      and TAGPAGE_T.count('<div class="empty">') >= 2,
      "某组为空时页面出现空白块，人会以为渲染失败")
check("★ 总览页不复用 _ctx（它要跑 inventory / 候选分组，全是首页待办的事）",
      "wstore.pages()" in TAGPAGE_FN and "_ctx(request)" not in TAGPAGE_FN,
      "总览页答的是「有哪些词」，不该顺带背一份首页的待办统计。"
      "（2026-11-16 更正：/wiki 真实 HTTP 实测 0.16–0.21 s，此前记的 5.4 秒是"
      "进程内首测含模板首编译，不是线上首屏；这条守卫的理由与那组数字无关。")

print("\n== S. 阅读时露出知识库（2026-11-16 晚）==")
# 用户把标签过完、标签线收口之后问「接下来呢」。只读盘点：32 页 / 6,858 字 /
# 中位 220 字，18 页只有 1 个来源，而知识库只有主动点进去才看得见。
# 这一段守的是「阅读流里的那一行」：本地判据、零模型、可解释、只读。
RELATE_T = read("core/wiki_relate.py")
RELATE_FN = _fn_src("relate", RELATE_T)
RELATE_TOK = _fn_src("_tokens", RELATE_T)
RELATE_TMPL = read("templates/wiki/_relate_read.html")
READER_VIEWS = read("apps/reader/views.py")

check("★ 相关页模块存在，且详情视图真调它（不是写好了没人用）",
      bool(RELATE_FN) and "from core import wiki_relate" in READER_VIEWS
      and 'wiki_relate.relate(' in READER_VIEWS,
      "模块写好了但视图没接 = 一次都没跑过")
check("★★ 渲染路径零 LLM（这里挂在每次展开文章上，一天几百次）",
      RELATE_T and "conversation" not in _no_hash_comments(RELATE_T)
      and "promptbuilder" not in RELATE_T and "_call(" not in RELATE_T,
      "读一篇文章花一次模型钱，成本会失控；判据必须是本地的、可预期的")
check("★★ 拉丁字母按整词取 token，不拆 2 字组合",
      "_LATIN.findall" in RELATE_TOK and "for run in _CJK" in RELATE_TOK
      and "range(len(norm)" not in RELATE_TOK,
      "gemini 的 gram 里 in/em/mi 在任何英文里都有 —— 200 篇实测白嫖出 22% 假命中")
check("★★ 标签只在「标题 + 开头」里找，不在全文找",
      "ntag in lead" in RELATE_FN and "ntag in whole" not in RELATE_FN,
      "长文尾巴提一句「游戏」「安全」不算讲这个话题：实测一篇 NVDA 长文同时命中"
      "「腾讯游戏」和「网络安全风险」")
check("★ 页标题命中分两档，理由文案各说各的实话",
      "文章标题就是" in RELATE_FN and "文章提到" in RELATE_FN
      and "tn in head" in RELATE_FN and "tn in body" in RELATE_FN,
      "一律写「标题里就有」= 骗人：正文某处提一句也被说成标题命中")
check("★ 极短正文不判（有门槛且是前置判断）",
      "MIN_TEXT" in RELATE_FN and "len(body) < MIN_TEXT" in RELATE_FN,
      "「💯」「Yup」这类推文越短越容易把偶然重合当主题重合")
check("★★ 说不出理由的一律不返回",
      "and why" in RELATE_FN,
      "一个说不出理由的推荐，人第一次点进去就不会有第二次")
check("★ 索引缓存按 root 分开且随文件变动失效",
      "_cache[root]" in RELATE_T and "st.st_mtime_ns" in RELATE_T
      and "_cache.get(root)" in RELATE_T,
      "测试都用临时库，共用一个槽会让 A 库的索引答 B 库的问题（隔离串味）")
check("★ 索引只扫 wiki/，不吃 .drafts/",
      'os.path.join(root, "wiki")' in RELATE_T and ".drafts" not in RELATE_T,
      "草稿还没入库，把它当「已有判断」是错的（而且会漏出未审批内容）")
check("★ 只读：模块里没有任何写盘动作",
      not re.search(r"\.write_text\(|\.unlink\(|st\.approve|set_polish", RELATE_T),
      "这是渲染路径上的旁注，出问题不能连累正文")
check("★★ 模板链接一律新标签页（别把正在读的书关掉）",
      'target="_blank"' in RELATE_TMPL and 'rel="noopener"' in RELATE_TMPL,
      "用户 2026-11-16 明确要求：点进 wiki 页要开新窗，而不是在当前阅读列表里跳走")
check("★ 模板空则不渲染（不出现「0 页相关」的噪声）",
      "{% if wiki_relate %}" in _no_tpl_comments(RELATE_TMPL),
      "读一篇文章看到「0 页」是纯噪声；「没显示」本身已经说了没有")
check("★ 详情模板真 include 了这个片段",
      'include "wiki/_relate_read.html"' in ENTRY_DETAIL,
      "片段写好了但没挂 = 又回到「只有主动点进去才看得见」")

# ---- 2026-11-16：审批浮层「自己指定并入页」（模型没推荐 ≠ 不许人决定）----
#   ★ 用户实测 D87（`ai-agent-xie-zuo-pi-lao` → 降噪相关）：推荐缓存 rows=[]，
#     浮层里整块没有按钮；改用对话工具又被拒（整页并入只能走浮层）—— 两头堵死。
#     判据钉四件事：入口在、只给该给的稿、目标只来自已入库页、渲染成本不涨。
_MANUAL_T = _no_tpl_comments(WORKAREA_T)
_MANUAL_FORM = (_MANUAL_T.split('id="merge-manual-form"')[1].split("</form>")[0]
                if 'id="merge-manual-form"' in _MANUAL_T else "")
_CTX_SRC = _fn_src("_approval_workarea_ctx", VIEWS_T)
check("★ 手动并入：浮层真有这块，且是一个能提交的完整表单",
      bool(_MANUAL_FORM)
      and 'hx-post="/wiki/approval/merge-into"' in _MANUAL_FORM
      and 'hx-target="#approval-workarea"' in _MANUAL_FORM
      and "{% csrf_token %}" in _MANUAL_FORM
      and 'name="slug" value="{{ slug }}"' in _MANUAL_FORM
      and 'name="target"' in _MANUAL_FORM,
      _MANUAL_FORM[:160] or "NO-FORM")
check("★ 手动并入：目标下拉列的是已入库页（标题 + 域），不是 slug 堆",
      'value="{{ t.slug }}"' in _MANUAL_FORM
      and "{{ t.title }}" in _MANUAL_FORM and "{{ t.domain }}" in _MANUAL_FORM,
      "只给拼音 slug 等于让人背文件名")
check("★★ 手动并入：候选复用同一次 pages()（渲染不许多扫库）",
      bool(_CTX_SRC) and _CTX_SRC.count("wstore.pages()") == 1,
      "本函数有 13 个调用点，多一次 pages() 就是每次刷新多 40–50 ms")
check("★★ 手动并入：只在普通新页稿给目标（去向已定的稿不给错入口）",
      bool(_CTX_SRC) and bool(re.search(
          r"if ordinary:\s*\n\s*merge_targets = ", _CTX_SRC)),
      "替换 / 删除 / 并入稿再给一个「并进别页」= 让人把确认过的知识再覆盖一次")
check("★ 手动并入：两个空态分支都显式给 merge_targets=[]",
      bool(_CTX_SRC) and _CTX_SRC.count('"merge_targets": []') == 2,
      "缺变量被模板当空值 —— 与 related_auto 同一条教训")
check("★ 手动并入：目标绝不来自 .drafts/（待审稿互相覆盖 = 谁先入库谁吃谁）",
      bool(_CTX_SRC) and ".drafts" not in
      _CTX_SRC.split("merge_targets = []")[1].split("return {")[0],
      "并入目标只能是已入库页")
check("★ 手动并入排在推荐之后（先看模型怎么说，再自己定）",
      'id="merge-manual-form"' in _MANUAL_T
      and _MANUAL_T.index('id="merge-manual-form"') > _MANUAL_T.index('id="related-refresh"'))
check("★ 推荐为空时文案指向手动入口（否则人以为无路可走）",
      "自己指定一页并入" in _no_tpl_comments(WORKAREA_T))
# ★ 拒收话也要指向「不经过模型」的那条路：D87 实测里，工具让人「去浮层」，
#   而浮层当时一个按钮都没有 —— 错误信息指到了另一面墙上。判据查一整句，
#   不查关键词（「改为并入」在别处也出现，查它等于白查）。
_INGEST_SRC = read("core/ingest.py")
_REFUSE = re.search(r"这份草稿只有这些候选.{0,200}", _INGEST_SRC, re.S)
check("★★ 整页并入的拒收话点名「自己指定一页并入」（不带人撞第二面墙）",
      bool(_REFUSE) and "自己指定一页并入" in _REFUSE.group(0),
      (_REFUSE.group(0)[:120] if _REFUSE else "NO-MSG"))

# ---- 2026-11-16：首页页表「来源列 → 标签列」（用户：来源列意义不大）----
#   ★ 判断依据（真实库只读核对）：sources 是 `entry:7703` / `report:daily-…` 这类
#     id，单页最多 9 个 —— 一列 chip 全是噪声；标签是人自己收敛过的词，还能点。
#   ★ 换列**不是**加列：加列会让同一组标签在标题格里再出现一遍（两处显示 = 两处
#     口径，改一处漏一处）。所以判据里既要有「标签在独立格」，也要有「第一格里
#     没有标签」—— 只查前者，两处都渲染也能全绿。
_COLS = _no_tpl_comments(BODY_T)
_HEAD = re.search(r"<thead>(.*?)</thead>", _COLS, re.S)
_HEAD_CELLS = (re.findall(r"<th[^>]*>(.*?)</th>", _HEAD.group(1))
               if _HEAD else [])
check("★ 首页页表四列表头 = 页 / 更新 / 置信 / 标签（来源列下线）",
      _HEAD_CELLS == ["页", "更新", "置信", "标签"], _HEAD_CELLS)
check("★★ 标签在独立格，且标题格里不再来一份（换列 ≠ 两处都放）",
      '<td class="tag-cell">' in _COLS
      and "wiki-tag" not in _COLS.split("{% for p in g.pages %}")[1].split("</td>")[0],
      "两处渲染同一组标签 = 以后改一处漏一处")
check("★ 溯源降级但不丢：列表给「N 源」+ hover 看全，chip 不再上列表",
      "src-chip" not in BODY_T
      and "p.sources|length" in _COLS
      and "来源：{{ p.sources|join" in _COLS,
      "反向映射还在，只是入口从列表挪到详情页")
check("★ 没标签的页给可见占位（空格子会被当成渲染坏了）",
      "{% empty %}<span class=\"meta\">—</span>{% endfor %}" in _COLS)
check("★ 表头列宽有 class（标签列要够宽，否则 4 个 chip 折成两行看不出分组）",
      all(x in CSS for x in (".cgrid .w-page", ".cgrid .w-tags")),
      "只给 th 加 class 不写 CSS = 摆设")

# ---- 2026-11-16：首页不许露目录绝对路径（用户：「路径里的用户名等会泄露隐私」）----
#   ★ 这一处泄露**没有功能症状**：页面照常好用，只是把 `/Users/<用户名>/…/data/wiki`
#     打了出来；而页面会被截图、被投屏、被贴进聊天 —— 路径里的用户名与目录结构
#     跟「知识库讲了什么」毫无关系。**没有症状的缺陷只能靠判据守。**
#   ★ 判两条，缺一不可：
#     ① 首页只许 `root_label`（相对项目根 / 「自定义目录」）；
#     ② **所有** wiki 模板都不许出现未脱敏的 `stats.root` —— 只查 index 的话，
#        以后哪个页「顺手」加一行同样泄，而首页这条照样绿。
#   ★ 必须剥 `{% comment %}` 再判：本段的说明本身写着 `stats.root` 字样（老坑）。
_IDX = _no_tpl_comments(read("templates/wiki/index.html"))
check("★ 首页目录只打相对段 stats.root_label（绝对路径不上页面）",
      "{{ stats.root_label }}" in _IDX)
_leaky = []
for _p in sorted(glob.glob(os.path.join(BASE, "templates/wiki/*.html"))):
    _src = _no_tpl_comments(read(os.path.relpath(_p, BASE)))
    if re.search(r"stats\.root(?!_label)", _src):
        _leaky.append(os.path.basename(_p))
check("★★ 全部 wiki 模板都不出现未脱敏的 stats.root（漏一个页就前功尽弃）",
      not _leaky, _leaky or "")

# ★ 模板读的键必须真的在：Django 模板缺键**静默渲染空串** —— `{{ stats.root_label }}`
#   写对了而 `stats()` 忘了给，页面上那行就变成「目录：（可用 WIKI_DIR…）」，谁都不会
#   报错。这是本项目最贵的那类失败（出错没人知道）。
_STATS_FN = _fn_src("stats", read("core/wiki.py"))
check("★ stats() 真的给了 root_label（缺键时模板静默变空，不会报错）",
      '"root_label": root_label()' in _STATS_FN, _STATS_FN[:80])
# ★ 脱敏的机制是「必须落在项目根内才显示」：换成 `str(root())` / 字符串截断都会
#   在某台机器上重新露出路径。这条钉形状，行为由 core/tests/test_wiki_root_label.py 钉。
_RL_FN = _fn_src("root_label", read("core/wiki.py"))
check("★★ root_label 靠 relative_to 判项目内外（不是截字符串 / 不是直接 str(root())）",
      "relative_to" in _RL_FN and "str(root())" not in _RL_FN, _RL_FN[:80])

# ---------------------------------------------------------------- V. 对话气泡 + 进入滚底
print("\n== V. 对话消息气泡 + 进入即滚底 ==")
_CSS = read("static/app.css")
_JS = read("static/app.js")
# 气泡：我自己发的消息右对齐 + 浅背景，AI 左对齐。class 名不许动。
check("★ .wc-user 右对齐 + 浅背景（我发的消息一眼可辨）",
      ".wc-user { align-self: flex-end; background: #eaf2fb" in _CSS)
check("★ .wc-assistant 左对齐",
      ".wc-assistant { align-self: flex-start; }" in _CSS)
check(".wc-log 仍是 flex column（align-self 才生效）",
      ".wc-log { display: flex; flex-direction: column;" in _CSS)
check("★ 消息 class 名没被气泡改造动过（test_approval_chat 钉死）",
      'class="wc-msg wc-{{ m.role }}"' in read("templates/wiki/_wc_msg.html")
      and 'class="wc-msg wc-user"' in read("core/tests/test_approval_chat.py"))
# 滚底 helper 存在且被三处时机调用
check("★ wcScrollBottom helper 存在",
      "function wcScrollBottom(box)" in _JS and "box.scrollTop = box.scrollHeight" in _JS)
check("★ 审批浮层打开即滚底（openModal 里认 .approval-chat）",
      'layer.querySelector(".approval-chat")' in _JS
      and 'wcScrollBottom(alog)' in _JS)
check("★ 文章浮窗首次加载 settle 后滚底",
      "if (!locateMsgId) wcScrollBottom(body);" in _JS)
check("★ 文章浮窗重开也滚底（aiOpen else 分支）",
      "if (body && !locateMsgId) wcScrollBottom(body);" in _JS)
# 深链不许被滚底覆盖：三处滚底里两处挂了 !locateMsgId 守卫
check("★★ ?msg= 深链待定位时不滚底（否则精确跳转被覆盖）",
      _JS.count("!locateMsgId) wcScrollBottom") >= 2)

# ---------------------------------------------------------------- V2. 去掉身份标签 + 生成期禁用按钮（2026-11-19）
print("\n== V2. 气泡去身份标签 + 生成期禁用发送按钮 ==")
# ★ wc-who 必须在模板 / JS / CSS **三处全部消失**（只删一处 = 别的路径又把标签画回来）
check("★★ 消息模板不再渲染 wc-who 身份标签",
      "wc-who" not in read("templates/wiki/_wc_msg.html"))
check("★★ 前端乐观气泡不再创建 wc-who（streaming 路径）",
      'className = "wc-who"' not in _JS and 'mw.className' not in _JS
      and 'aw.className' not in _JS)
check("★★ CSS 不再有 wc-who 规则（标签行高已移除）",
      ".wc-who" not in read("static/app.css"))
# ★ 生成期禁用发送按钮：自动总结也走 submit → wcStreamSubmit，所以按钮同样变灰
check("★★ 流式提交时禁用发送按钮",
      "sendBtn.disabled = true" in _JS)
check("★★ cleanup 恢复发送按钮（fallback 时 DOM 保留，不解禁会永久发不出）",
      "sendBtn.disabled = false" in _JS)
# ★ 在飞时吞掉 submit，不放行 htmx —— disabled 只挡鼠标，textarea 的 Enter 仍会触发 submit
check("★★ 生成期间 submit 被吞（不放行 htmx 老端点重复花钱）",
      "if (wcStreaming) { evt.preventDefault(); evt.stopPropagation(); return true; }" in _JS)

# ---------------------------------------------------------------- W. 对话 SSE 流式（加法式）
print("\n== W. 对话 SSE 流式（增强而非替换）==")
_LLM = read("core/llm.py")
_CR = read("core/costreport.py")
_CG = read("core/citeguard.py")
_VW = read("apps/wiki/views.py")
_URLS = read("apps/wiki/urls.py")
# 后端：新增流式出口，非流式三函数一行没动（测试桩钉死它们）
check("★ llm.chat_stream 开 include_usage（否则 usage 记 0，破坏记账铁律）",
      '"stream_options": {"include_usage": True}' in _LLM
      and '"stream": True' in _LLM)
check("★ chat_stream 按 index 拼 tool_calls arguments",
      "tools_by_index" in _LLM
      and '["arguments"] += fn["arguments"]' in _LLM)
check("★ 流式记账走 chat_recorded_stream → llm.record（不绕过预算闸）",
      "def chat_recorded_stream" in _CR and "pool_gate(pool)" in _CR
      and "llm.record(kind" in _CR)
check("★ _call_stream 不翻倍重试（字已吐出无法收回）",
      "def _call_stream" in _CG and "不重试" in _CG)
_CG_STREAM_FN = _fn_src("_call_stream", _CG)
_WL_STREAM_FN = _fn_src("_call_stream", read("core/wiki_loop.py"))
check("★★ 流式工具轮把 tool_calls 带回给 loop（否则审批对话的合并永不执行）",
      'stats["tool_calls"]' in _CG_STREAM_FN
      and 'getattr(res, "tool_calls"' in _CG_STREAM_FN
      and "工具轮也走这里" in _WL_STREAM_FN,
      "漏带出 tool_calls ⇒ run() 判「模型没有返回内容」，工具一次都不执行")
check("★★ 非流式老函数仍在（流式是并存不是替换）",
      "def chat(" in _LLM and "def chat_recorded(" in _CR
      and "def _call(" in _CG)
check("★ 两处生成函数都接 on_delta（默认 None → 老路径逐字不变）",
      "def ask(source_type: str, source_id: str, question: str, on_delta=None)" in read("core/conversation.py")
      and "def run(question: str, *, selected_slug: str = None, on_delta=None)" in read("core/wiki_loop.py"))
# 端点：SSE 帧协议 + 线程桥接 + 权威渲染
check("★ _sse_conversation 用后台线程边收边吐（不是算完再返回）",
      "threading.Thread(target=worker" in _VW and "q.get()" in _VW)
check("★ done 帧带服务端渲染的权威片段（前端不自己拼 markdown）",
      '{"t": "done", "html": on_done(payload)}' in _VW)
check("★ nginx 关缓冲，否则逐字效果被吞", 'X-Accel-Buffering' in _VW and '"no"' in _VW)
check("★ 审批流式与老端点共用同一结果渲染（OOB 不复制逻辑）",
      "_approval_chat_result_html(request, res, selected_slug)" in _VW
      and "def _approval_chat_result_html" in _VW)
check("★ 两个 stream/send 路由都在", "chat/stream/send" in _URLS
      and "approval/chat/stream/send" in _URLS)
# 前端：capture 拦截 + 失败回退 + 一次性 done 覆盖
check("★★ 前端 capture 阶段拦 submit（先于 htmx bubble），失败回退老端点",
      '"submit", function (e)' in _JS and "}, true);" in _JS
      and "data-wc-fallback" in _JS and "function fallback()" in _JS)
check("★ 推不出 stream URL 就回退（不硬编码第二份 URL）",
      "function wcStreamUrl" in _JS and "return false;  // 推不出 → 回退" in _JS)
check("★ delta 逐字 append、done outerHTML 覆盖、err 可见（三帧都处理）",
      'msg.t === "delta"' in _JS and 'msg.t === "done"' in _JS
      and 'msg.t === "err"' in _JS and "replaceChild(fresh, panel)" in _JS)
# ★ 2026-11-23 实测：审批对话「并入 D146」写盘成功，列表却还留着 D144 ——
#   流式 done 帧的 HTML 由 JS 插入，htmx 不会替我们执行 hx-swap-oob；
#   服务端拼好的列表/工作区/计数三块被 `firstElementChild` 丢掉了。
DONE_JS = _JS.split("replaceChild(fresh, panel)")[-1][:1400]
check("★★ 流式 done 帧自己应用 OOB（htmx 不处理 JS 插入的节点）",
      'hasAttribute("hx-swap-oob")' in DONE_JS
      and "getElementById(node.id)" in DONE_JS
      and "replaceChild(node, old)" in DONE_JS,
      "只换 chat 面板 = 并入成功后列表不刷新，要点击改选中才消失")
check("★ 同一时刻只允许一条流（防连点叠出两条花钱提问）",
      "var wcStreaming = false" in _JS
      and "if (wcStreaming) { evt.preventDefault(); evt.stopPropagation(); return true; }" in _JS)

# ---- X. 带反馈的重编 + 一键重编按钮（2026-11-22，用户「不要让用户思考」） ----
# ★ 事故现场：并入候选后结论重编写了旧 slug（改名前的 zhong-mei-mo-xing-cha-ju），
#   _check_links 拒写 → 正文留空 + stale → 提交被拒，提示只说「先重编」却没按钮、
#   也没告诉人为什么失败。提示词写「slug 逐字照抄」是承诺，**回喂具体错误**才是事实。
_WL = read("core/wiki_loop.py")
_WK = read("core/wiki.py")
_WV = read("apps/wiki/views.py")
_WU = read("apps/wiki/urls.py")
_WT = read("templates/wiki/_approval_workarea.html")
# 按 def 切片（注释里出现同样字样不算数）
_POLISH_FN = _fn_src("_polish_one", _WL)
_REPOLISH_FN = _fn_src("approval_repolish", _WV)
_SET_POLISH_FN = _fn_src("set_polish", _WK)
# 剥注释：模板/源码里大段「为什么」注释会把关键字喂给假绿判据
def _strip_comments(src):
    return re.sub(r"\{% comment %\}.*?\{% endcomment %\}", "", src, flags=re.S)
_WT_CODE = _strip_comments(_WT)
_POLISH_CODE = re.sub(r"^\s*#.*$", "", _POLISH_FN, flags=re.M)

check("X-1 失败原因落草稿机器字段，成功同一笔剥掉（不留在人读的页里）",
      'POLISH_ERR_FM = "polish_error"' in _WK
      and "POLISH_ERR_FM" in " ".join(re.findall(r"TRANSIENT_FM_KEYS\s*=.*", _WK))
      and "set_fm={wstore.POLISH_ERR_FM: why}" in _POLISH_CODE
      and "set_fm={}" not in _POLISH_CODE
      and "drop_fm=(STALE_FM, POLISH_ERR_FM)" in _SET_POLISH_FN,
      "只写日志不落盘 = 下次点按钮还是盲重试；只落盘不剥 = 成功页挂假警报")
check("X-2 上次失败原因进 messages（提示词承诺之外确有程序事实）",
      "POLISH_FEEDBACK" in _WL
      and "messages.append({\"role\": \"user\", \"content\": POLISH_FEEDBACK % prior})" in _POLISH_CODE
      and "messages.append({\"role\": \"user\", \"content\": POLISH_FEEDBACK % why})" in _POLISH_CODE,
      "没把 why 回喂 = 盲重试，模型照样按标题猜旧 slug")
check("X-3 只重试可修正的校验失败，生成错误不重试（预算闸确定性）",
      "POLISH_RETRY_MAX = 1" in _WL
      and "if not ok and not prior and POLISH_RETRY_MAX > 0" in _POLISH_CODE
      and _POLISH_CODE.index("if err or not (text or \"\").strip()")
      < _POLISH_CODE.index("if not ok and not prior"),
      "预算不足还重试 = 白花钱；已有反馈还重试 = 空转")
check("X-4 一键重编是显式 POST，落点与审批对话 polish 共用一份判据",
      'path("wiki/approval/repolish", views.approval_repolish' in _WU
      and "wiki_loop._polish_one(slug)" in _REPOLISH_FN
      and "_compile_running_guard()" in _REPOLISH_FN
      and "@require_POST" in _WV[max(0, _WV.index("def approval_repolish") - 40):
                                _WV.index("def approval_repolish")],
      "渲染路径调它 = 刷一次花一次钱；漏互斥 = 编译时能穿透")
check("X-5 按钮就在 stale 警告条里， POST 指到端点、带 slug、失败原因可见",
      "{% if body_stale %}" in _WT_CODE
      and 'hx-post="/wiki/approval/repolish"' in _WT_CODE
      and 'name="slug" value="{{ slug }}"' in _WT_CODE
      and ">重编结论</button>" in _WT_CODE
      and "{% if repolish_err %}" in _WT_CODE
      and _WT_CODE.index("hx-post=\"/wiki/approval/repolish\"")
      > _WT_CODE.index("{% if body_stale %}"),
      "只说『重编后再提交』不给按钮 = 人不知道下一步干什么")
_REPOLISH_HELPER = _fn_src("_repolish_workarea", _WV)
check("X-6 重编回包换工作区 + 列表，错误不藏在 toast 里",
      '"wiki/_approval_workarea.html"' in _REPOLISH_HELPER
      and '"wiki/_approval_list.html"' in _REPOLISH_HELPER
      and 'ctx["repolish_err"] = err' in _REPOLISH_HELPER
      and "_repolish_workarea(request, slug" in _REPOLISH_FN,
      "换错节点 / 只 toast = 页面还指着旧正文")

# ── Y. 每轮审批从零开始 + 审批对话不挂痕迹（2026-11-22 用户拍板） ──────────
_WKB = read("core/wiki_agent.py")
_APPROVAL_CHAT_T = _strip_comments(read("templates/wiki/_approval_chat.html"))
_WC_MSG_T = _strip_comments(read("templates/wiki/_wc_msg.html"))
_CHAT_T = _strip_comments(read("templates/wiki/_chat.html"))
_RESET_FN = _fn_src("_reset_approval_chat", _WK)
_APPROVAL_CTX_FN = _fn_src("_approval_chat_ctx", _WV)
_WC_CTX_FN = _fn_src("_wc_msg_ctx", _WV)
_CHAT_CTX_FN = _fn_src("_chat_ctx", _WV)

check("Y-1 commit 后清空审批对话（每轮从零开始，不再追加批次消息）",
      "chat_reset = _reset_approval_chat()" in _WK
      and '"chat_reset": chat_reset' in _WK
      and "wa.clear_approval_conversation()" in _RESET_FN
      and "wa.archive_approval_conversation(" not in _WK
      and "def archive_approval_conversation(" not in _WKB,
      "还往固定 source_id 追加 = 对话越积越长；旧函数留着会把人带回旧口径")
check("Y-2 审批消息既查不到也不挂痕迹锚点（双断，不是只 CSS 隐藏）",
      "_wc_msg_ctx(m, show_marks=False)" in _APPROVAL_CTX_FN
      and "if show_marks:" in _WC_CTX_FN
      and "mdrender.render(m.body)" in _WC_CTX_FN
      and "show_marks=item.show_marks" in _APPROVAL_CHAT_T
      and "{% if show_marks %}" in _WC_MSG_T
      and 'data-mark-type="chat"' in _WC_MSG_T,
      "只隐藏列表 = 还能选中建看不见的痕迹；只断 DOM = 历史痕迹照查照渲")
check("Y-3 文章对话仍保留痕迹（别把另一条路一起砍了）",
      "show_marks=item.show_marks" in _CHAT_T
      and "_wc_msg_ctx(m)" in _CHAT_CTX_FN
      and '{% include "wiki/_marks.html"' in _WC_MSG_T,
      "文章对话的划线/批注是既有能力，隐藏审批对话不许波及")

# ---------------------------------------------------------------- Z. 并入稿拆候选改投（2026-11-22）
_IG = read("core/ingest.py")
_ATTACH_FN = _fn_src("attach_candidates_to_page", _IG)

check("Z-1 并入稿可拆候选改投别页；破坏性方向与自拆仍拒",
      'if key and key != "merges":' in _ATTACH_FN
      and "if own == target:" in _ATTACH_FN
      and "整页并入的目标就是" in _ATTACH_FN
      and "不能只拆几条候选去并入" in _ATTACH_FN,
      "一刀切拒掉 #386 这种真需求；放开不设自拆闸会把候选加回源稿")

print("\n%d PASS / %d FAIL" % (ok, bad))
if bad:
    print("失败项：")
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("ALL PASS")
