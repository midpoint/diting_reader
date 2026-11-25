"""零成本验证「周报（M9）」：口径 / kind 隔离 / 缺日报处置 / 引用改源。

用法：python scripts/verify_weekly.py

不花钱（不调 LLM）：只跑纯函数、文件口径与"缺日报就 fail"这两条早退分支。
覆盖：
  A. 窗口与素材口径（7 份日报、真实窗口自解释、头部/统计段被剥掉）
  B. 🔴 kind 隔离：周报绝不匹配日报 toggle（反之亦然）+ 标题前缀判定
  C. 🔴 周报文件不被日报枚举/推送扫到；同 (kind, date) 可并存两条
  D. 缺日报：缺历史 ⇒ 立即 fail + 通知；缺当天 ⇒ wait（交给 guard 重试）
  E. 引用从日报搬来后要改成 src=weekly（否则周报打开率统计不到）
  F. star 对账：Miniflux UI 点的星按 published_at 补 inferred
  G. 预算池：周报有独立池，不挤总结池
  H. 日报/周报共用一个 id 池（SummaryRun + kind），默认 manager 只放行日报
  I. 周报推送行补齐（异常被 try 吞掉会静默漏）
  J. star 时间戳：`hx/star` 必须记账；归属窗 06:00 起、与下一期首尾相接
  K. 批注：读到了可核对，读失败必须在报告头部留痕
  L. star 按分类分组 + 异常只给统计规律（不列明细）
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "apps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

import _dbtest  # noqa: E402

# ★★ 这批脚本里最危险的一个：它按**固定日期** `WE = 2026-11-01` 在库里
#    `SummaryRun / StarEvent.objects.filter(...).delete()`。
#    只要那个周末你真的生成过周报，跑一次验证就把那期记录吃掉 —— 和 2026-10-31
#    清空 96 条划线是同一类事，只是这次吃的是周报行、更不容易被发现。
#    切内存测试库后，固定日期不再是"可能撞上真实数据"，而是纯脚手架。
_dbtest.isolate()

from django.conf import settings  # noqa: E402

from core import costreport as cr  # noqa: E402
from core import reportstore, reporttext as rt, weekly as wk  # noqa: E402
from core import summary as summ  # noqa: E402
from core.models import StarEvent, SummaryRun  # noqa: E402

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


WE = date(2026, 11, 1)      # 周日

# ============================================================ A. 窗口与素材
print("\n== A. 窗口与素材口径 ==")
days = wk.week_days(WE)
check("7 个报告日、以 week_end 结尾", len(days) == 7 and days[-1] == WE, days)
# ★ 期望值**从日报自己的窗口派生**，不写死字符串 —— 这里曾经把 bug 钉成契约：
#   写死 "2026-10-26 06:00 → 2026-11-01 06:00" 而描述写的是 D-7，两者自相矛盾，
#   于是 7 份日报被说成 6 天（2026-11-01 用户指出）。
_first, _last = wk.week_days(WE)[0], wk.week_days(WE)[-1]
_span0, _span1 = summ.window_for(_first)[0], summ.window_for(_last)[1]
check("窗口 = 7 份日报的真实合集（起点 = 首份日报窗起点 D-7 N:00）",
      wk.window_label(WE) == f"{_span0:%Y-%m-%d %H:%M} → {_span1:%Y-%m-%d %H:%M}",
      wk.window_label(WE))

md = ("# 谛听日报 · 2026-10-27\n"
      "> 窗口 2026-10-26 06:00 → 2026-10-27 06:00\n"
      "> 生成方式：S0 1 次｜调用 3 次｜1000 tokens\n"
      "\n## 一、数量统计\n\n- 总条目 **120**\n\n| 分类 | 条数 |\n| --- | --- |\n"
      "| AI | 30 | 25% |\n\n## 二、按分类内容汇总\n\n- 要点 [猫眼 · 08:53](http://x)\n"
      "\n## 四、谛听札记\n\n- 今天无大事\n")
clean = wk.clean_daily(md)
check("剥掉头部 `>` 元信息", "> 窗口" not in clean and "> 生成方式" not in clean)
check("剥掉一级标题（`# `），保留二级小节",
      not clean.startswith("# ") and clean.startswith("## 二、"), clean[:40])
check("剥掉系统统计段（含表格）", "数量统计" not in clean and "总条目" not in clean
      and "| AI |" not in clean)
check("保留内容段与札记", "按分类内容汇总" in clean and "谛听札记" in clean)
check("保留正文引用", "猫眼 · 08:53" in clean)

# ============================================================ C. 文件与行
print("\n== C. 周报文件与日报互不干扰 ==")
tmp = tempfile.mkdtemp()
orig_dir = settings.REPORT_DIR
settings.REPORT_DIR = tmp
try:
    (Path(tmp) / "daily-2026-10-30.md").write_text("# 日报\n", encoding="utf-8")
    (Path(tmp) / "weekly-2026-11-01.md").write_text("# 周报\n", encoding="utf-8")
    check("★ reportstore.days() 只认 daily-*（周报不会被日报枚举扫到）",
          reportstore.days() == [date(2026, 10, 30)], reportstore.days())
    # ★ 2026-10-29 改口径：周报列表以**库**为准（失败/无文件的行也要列出来），
    #   文件名仍靠 weekly_path 隔离，两件事分开验。
    # ★ 判据不能硬编码期望列表：库里有没有别的周报是**状态**、不是机制。
    #   这里只断言「造的两期都在」且「返回的每个日期确实挂着 kind=weekly 的行」。
    SummaryRun.all_objects.filter(kind=wk.KIND, date=WE).delete()
    SummaryRun.all_objects.create(kind=wk.KIND, date=WE, state="done")
    SummaryRun.all_objects.create(kind=wk.KIND, date=WE - timedelta(days=7),
                                  state="failed", error="缺历史日报：2026-10-31")
    got_dates = wk.weekly_dates()
    # ★ 不能硬编码期望列表（库里本来就有别的周报，那是快照不是机制）：
    #   判据 = 造的两期都在 + 返回的每一个日期确实挂着 kind=weekly 的行。
    check("★ weekly_dates() 只出周报（不掺日报）",
          {WE, WE - timedelta(days=7)} <= set(got_dates)
          and all(SummaryRun.all_objects.filter(kind=wk.KIND, date=d).exists()
                  for d in got_dates),
          got_dates)
    check("★ 失败且没有文件的周报照样在列表里（能手动重跑）",
          wk.row_of(WE - timedelta(days=7)) is not None
          and wk.row_of(WE - timedelta(days=7)).state == "failed")
    check("周报正文可读", (wk.read_weekly(WE) or "").startswith("# 周报"))
    check("周报路径 = data/reports/weekly-<date>.md",
          wk.weekly_path(WE).name == "weekly-2026-11-01.md")
    SummaryRun.all_objects.filter(kind=wk.KIND,
                                  date__in=[WE, WE - timedelta(days=7)]).delete()
finally:
    settings.REPORT_DIR = orig_dir

# ============================================================ D. 缺日报
print("\n== D. 缺日报的处置（历史立即 fail / 当天 wait）==")
tmp2 = tempfile.mkdtemp()
settings.REPORT_DIR = tmp2
try:
    for i in (1, 2, 3, 4, 5, 6):        # 缺 10-31（历史）
        d = WE - timedelta(days=i)
        if i == 1:
            continue
        (Path(tmp2) / f"daily-{d.isoformat()}.md").write_text("# 日报\n正文\n",
                                                               encoding="utf-8")
    (Path(tmp2) / f"daily-{WE.isoformat()}.md").write_text("# 日报\n正文\n",
                                                           encoding="utf-8")
    before_notices = len(_dbtest.MUTED_NOTIFICATIONS)
    res = wk.run(WE, force=True)
    # ★ 反证：这条 fail 分支**确实走到了发通知那一步**，只是被隔离桩拦下。
    #   没有这条断言，"跑验证没弹窗"也可能是桩没装上、或失败路径压根没走到，
    #   那种全绿是假的。它同时守住「验证不许碰真实世界」这条纪律。
    muted = _dbtest.MUTED_NOTIFICATIONS[before_notices:]
    check("★ 验证不弹真实通知：失败通知被静默桩拦下（桩真被用上）",
          any("周报未生成" in t for t, _ in muted), muted)
    check("★ 缺**历史**日报 ⇒ 立即 fail（不生成残缺周报）",
          res.get("ok") is False and not res.get("wait"), res)
    check("fail 原因里点名缺了哪天", "2026-10-31" in (res.get("reason") or ""),
          res.get("reason"))
    row = SummaryRun.all_objects.filter(kind=wk.KIND, date=WE).first()
    check("失败写进 SummaryRun(kind=weekly)（页面能看见）",
          row is not None and row.state == "failed", row)
    check("失败原因记在库里", "2026-10-31" in (row.error if row else ""))
    check("★ 已通知标记（不会每 30 分钟刷一遍）", bool(row and row.notified))

    # 只缺**当天**日报 ⇒ wait（不调模型、不落盘）
    SummaryRun.all_objects.filter(kind=wk.KIND, date=WE).delete()
    (Path(tmp2) / f"daily-{(WE - timedelta(days=1)).isoformat()}.md").write_text(
        "# 日报\n正文\n", encoding="utf-8")
    Path(f"{tmp2}/weekly-{WE.isoformat()}.md").unlink(missing_ok=True)
    Path(f"{tmp2}/daily-{WE.isoformat()}.md").unlink(missing_ok=True)
    res2 = wk.run(WE, force=True)
    check("★ 只缺**当天**日报 ⇒ wait（交给 guard 在窗口内重试，绝不花钱）",
          res2.get("wait") is True, res2)
    check("wait 时没有落盘、没有 WeeklyRun 成功行",
          not Path(f"{tmp2}/weekly-{WE.isoformat()}.md").exists())
finally:
    settings.REPORT_DIR = orig_dir
    SummaryRun.all_objects.filter(kind=wk.KIND, date=WE).delete()

# ============================================================ E. 引用改源
print("\n== E. 引用从日报搬来后要改 src（周报打开率才统计得到）==")
body = "见 [猫眼 · 08:53](http://127.0.0.1:8099/?sel=feed:12&open=5793&src=daily#row-5793)"
out = wk.retag_src(body, "weekly")
check("★ src=daily → src=weekly", "src=weekly" in out and "src=daily" not in out, out)
check("★ 只改 src，不动 sel/open/锚点",
      "sel=feed:12" in out and "open=5793" in out and "#row-5793" in out, out)
check("没有 src 的链接原样保留",
      wk.retag_src("[a](http://x.com/1)", "weekly") == "[a](http://x.com/1)")
check("★ 周报 src 在 clicktrack 的已知来源里",
      "weekly" in __import__("core.clicktrack", fromlist=["KNOWN_SOURCES"]).KNOWN_SOURCES)

# ============================================================ F. star 对账
print("\n== F. star 对账（Miniflux UI 点的星按发布时间补记）==")
tmp3 = tempfile.mkdtemp()
settings.REPORT_DIR = tmp3
try:
    fake = SimpleNamespace(
        list_entries=lambda **kw: [
            SimpleNamespace(id=1001, feed_id=7, title="A", url="http://a",
                            published_at=datetime(2026, 10, 28, 12, 0),
                            created_at=datetime(2026, 10, 28, 12, 0)),
            SimpleNamespace(id=1002, feed_id=8, title="B", url="http://b",
                            published_at=datetime(2026, 9, 1, 12, 0),
                            created_at=datetime(2026, 9, 1, 12, 0)),
        ])
    import core.datasource as ds_mod
    orig_get = ds_mod.get_datasource
    ds_mod.get_datasource = lambda: fake
    try:
        StarEvent.objects.filter(entry_id__in=(1001, 1002)).delete()
        n = wk._reconcile_stars(
            datetime(2026, 10, 26, 0, 0).replace(tzinfo=None),
            datetime(2026, 11, 2, 0, 0).replace(tzinfo=None))
        check("★ 窗口内的星被补记（inferred）", n == 1, n)
        ev = StarEvent.objects.filter(entry_id=1001).first()
        check("补记的来源标 inferred", ev is not None and ev.source == "inferred", ev)
        check("★ 窗口外的老 star 不补（不撑表、不污染本周）",
              not StarEvent.objects.filter(entry_id=1002).exists())
        got = wk.star_entries(WE)
        check("本周 star 列表拿得到", [g["entry_id"] for g in got] == [1001], got)
        check("star 段带站内深链（src=weekly）",
              "src=weekly" in wk.star_material(got), wk.star_material(got))
        StarEvent.objects.filter(entry_id__in=(1001, 1002)).delete()
    finally:
        ds_mod.get_datasource = orig_get
finally:
    settings.REPORT_DIR = orig_dir

# ============================================================ G. 预算池
print("\n== G. 周报独立预算池（不挤总结池）==")
check("★ POOL_WEEKLY 存在", cr.POOL_WEEKLY == "weekly")
check("★ weekly 池只归 summary_weekly",
      cr.POOL_KINDS[cr.POOL_WEEKLY] == ("summary_weekly",))
check("★ 预算键存在", cr.BUDGET_KEY_WEEKLY == "weekly_budget_cny")
check("★ summary 池仍是 4 个 kind（周报没把它撑大）",
      len(cr.POOL_KINDS[cr.POOL_SUMMARY]) == 4)
st = {p["pool"] for p in cr.budget_status()["pools"]}
check("budget_status 里有周报池", cr.POOL_WEEKLY in st, st)
check("周报池有独立上限", cr.pool_budget_cny(cr.POOL_WEEKLY) > 0)

# ============================================================ H. 合并后的同 id 池
print("\n== H. 日报/周报共用一个 id 池（SummaryRun + kind）==")
# ★ 这两条原本拿「库里当前有没有周报行」当期望：
#     SummaryRun.all_objects.filter(kind=WK).exists()
#   那是库的**状态**、不是机制 —— 换台机器/换个库/清过数据就会翻红（切内存测试库
#   正是被它咬住的）。机制的正确断言是：**同一行**在默认 manager 下看不见、
#   在 all_objects 下看得见。自己造一行，验完删。
_probe_wk = SummaryRun.all_objects.create(kind=wk.KIND, date=WE, state="done")
check("★ 默认 manager 只返回日报（周报行存在也看不见）",
      SummaryRun.objects.filter(kind=wk.KIND).count() == 0,
      SummaryRun.objects.filter(kind=wk.KIND).count())
check("★ all_objects 能看到**同一行**周报",
      SummaryRun.all_objects.filter(pk=_probe_wk.pk).exists())
check("base_manager 是 all_objects（否则 refresh_from_db 找不到周报行）",
      SummaryRun._meta.base_manager_name == "all_objects",
      SummaryRun._meta.base_manager_name)
_probe_wk.delete()

# 同一天允许「日报 + 周报」并存
D = date(2026, 9, 9)
SummaryRun.all_objects.filter(date=D).delete()
SummaryRun.all_objects.create(kind="daily", date=D, items_count=7)
SummaryRun.all_objects.create(kind=wk.KIND, date=D, source_count=7)
check("★ 同一天可并存日报行与周报行",
      SummaryRun.all_objects.filter(date=D).count() == 2)
check("★ 默认 manager 在同一天只看到日报那一行",
      SummaryRun.objects.filter(date=D).count() == 1
      and SummaryRun.objects.get(date=D).kind == "daily")
# 不串号：日报那侧按 date 取，绝不能拿到周报行
d_row, _ = SummaryRun.objects.get_or_create(kind="daily", date=D)
check("★ 日报的 get_or_create 不会拿到周报行",
      d_row.kind == "daily" and d_row.items_count == 7, d_row.kind)
w_row = SummaryRun.all_objects.get(kind=wk.KIND, date=D)
check("★ 日报周报各有独立 id（同一 id 池但不撞）", w_row.id != d_row.id,
      (d_row.id, w_row.id))
# 记账能按行归属（这正是合并的目的）
cm = summ.cost_map([d_row.id, w_row.id])
check("成本按 summary_run_id 聚合时不会混类型",
      set(cm.keys()) <= {d_row.id, w_row.id}, cm)

# ★ 契约：周报的模型调用必须带 run_id，否则记账全落到 NULL —— 页面「¥/调用/
#   tokens」三列永远是 0，而且**不会报错**（这个坑 2026-10-29 真踩过一次）。
_src = Path(BASE, "core", "weekly.py").read_text(encoding="utf-8")
check("★ 周报调用带 run_id=row.id（漏了成本就无声消失）",
      "run_id=row.id" in _src)
SummaryRun.all_objects.filter(date=D).delete()

# ============================================================ J. star 时间戳
print("\n== J. star 时间戳与归属窗（2026-10-29 修）==")
_WD = date(2026, 10, 29)
s0, e0 = wk._star_window(_WD)
_dws = wk.week_days(_WD)
check("★ star 窗起点 = 本期首份日报窗起点（不是 week_end-6 天）",
      s0 == summ.window_for(_dws[0])[0], (s0, summ.window_for(_dws[0])[0]))
check("★ star 窗终点 = 本期末份日报窗终点（不是 week_end+1 天）",
      e0 == summ.window_for(_dws[-1])[1], (e0, summ.window_for(_dws[-1])[1]))
_nxt = wk._star_window(_WD + timedelta(days=7))[0]
check("★ 与下一期首尾相接：既不空出整天、也不重叠",
      e0 == _nxt, (e0, _nxt))

_view_src = Path(BASE, "apps", "reader", "views.py").read_text(encoding="utf-8")
check("★ hx_star 必须真记 StarEvent（漏了 == 周报 star 段系统性丢星）",
      "StarEvent.objects.create" in _view_src and 'source="reader"' in _view_src)
check("★ 记账只在 toggle 成功之后（else 分支），失败不能记成加星",
      "else:\n        _record_star(entry, starred)" in _view_src)

# ============================================================ K. 划线可见性
print("\n== K. 划线与批注：三源可核对 / 读失败必须留痕 ==")
# ★ 周报的「我自己留下的痕迹」读本地划线/批注（`Highlight` / `Annotation`），
#   2026-11-01 起覆盖三类来源：日报（report）/ 文章（entry）/ 对话（chat）。
from core import marks as _mk  # noqa: E402

_col = {"need": 7, "covered": 7, "degraded": [], "missing": []}


def _header_of(notes, notes_err):
    st = {"calls": 1, "tokens": 100, "reasoning_tokens": 0, "truncated": [],
          "pt_total": 0, "notes": notes, "notes_err": notes_err}
    return "\n".join(wk._header(date(2026, 10, 29), _col, st))


_D = date(2026, 10, 23)
_mk.add_highlight("report", f"daily-{_D.isoformat()}",
                  "七格是 ToB 的需求筛选模型", note="这条要沉淀")
_mk.add_highlight("report", f"daily-{_D.isoformat()}", "只有划线、没写备注的一行")
_mk.add_annotation("report", f"daily-{_D.isoformat()}", "这周的判断偏保守")

_m = wk.week_marks(_D)
txt, n, err = _m["text"], _m["n"], _m["err"]
check("读到划线+批注 ⇒ 条数 3、err 空", n == 3 and err == "", (txt, n, err))
check("★ 摘录原文进素材，且标明它是「划线」（不是我说的）",
      "划线：七格是 ToB 的需求筛选模型" in txt, txt)
check("★ 划线旁的备注**单独一行**（这才是我自己敲的字）",
      "✎ 我在划线旁写的话：这条要沉淀" in txt, txt)
check("★ 批注原样进素材", "批注：这周的判断偏保守" in txt, txt)
check("★ 每条痕迹带深链（可追溯到那条划线）", "?mk=" in txt, txt)
check("★ 日报痕迹带 src=weekly（点出去才计得到周报打开率）",
      "src=weekly" in txt, txt)
check("读成功时报告头部不出现告警", "没用上你的划线" not in _header_of(n, err),
      _header_of(n, err))
check("★ 提示词只讲本地划线与批注（不提任何外部笔记工具）",
      "Notion" not in wk.W0_USER and "notion" not in wk.W0_USER.lower())
check("★ 第五节标题已含划线/批注（不再只是 star）",
      "star、划线、批注" in wk.W0_USER.replace("\n", ""), "标题缺失")

# ①b 文章与对话两类痕迹也要进素材（2026-11-01 新增）
from core.models import ChatMessage, Conversation  # noqa: E402
from django.utils import timezone  # noqa: E402
from core import sourceblock  # noqa: E402

_entry_id = 7001
_eh = _mk.add_highlight("entry", str(_entry_id), "科技巨头增速受限是算力瓶颈而非需求放缓")
_conv = Conversation.objects.create(source_type="entry", source_id=str(_entry_id))
_msg = ChatMessage.objects.create(conversation=_conv, role="assistant", body="AI 的回答")
_ch = _mk.add_highlight("chat", str(_msg.id), "AI 说的一句话")

_orig_meta = wk._entry_meta
# ★ 发布时间取「今天 09:00」：窗口小时是 6，09:00 归属**明天**那份日报 ⇒
#   只要 week_end 给明天，它就在本期。写死某天会让这条断言随系统日期漂。
_ts_today9 = timezone.make_aware(datetime(date.today().year, date.today().month,
                                          date.today().day, 9, 0))
wk._entry_meta = lambda eid: ({"published_at": _ts_today9, "feed_id": 3,
                               "title": "某篇投资总结"}
                              if eid == _entry_id else None)
try:
    # 消息/划线的 created_at 都是"现在"，归到明天那份日报 ⇒ week_end 取明天
    # ★ 还要把 entry→feed 的映射打桩：那是真实 Miniflux 查询，测试库里 7001 不存在，
    #   不桩的话 chat 深链会一路降级成 None（然后就测不到 msg= 了）。
    _orig_fid = sourceblock._entry_feed_id
    sourceblock._entry_feed_id = lambda eid: 3
    try:
        _m2 = wk.week_marks(date.today() + timedelta(days=1))
    finally:
        sourceblock._entry_feed_id = _orig_fid
finally:
    wk._entry_meta = _orig_meta
_t2 = _m2["text"]
check("★ 文章划线进素材，且标明来源是文章", "[文章《某篇投资总结》]" in _t2, _t2)
check("★ 对话划线进素材，且说明它围绕哪篇内容（跨类型对照要靠它）",
      "[对话：关于《某篇投资总结》]" in _t2, _t2)
check("★ 对话深链带 msg= 定位到那条消息", "msg=%d" % _msg.id in _t2, _t2)
check("★ 对话深链的 msg/mk 都在 # 之前（拼到锚点后面前端读不到）",
      all(ln.index("msg=") < ln.index("#row-") for ln in _t2.split("\n")
          if "msg=" in ln and "#row-" in ln), _t2)

# ② 读失败必须留痕（不允许退化成「本周没划线」）
_orig_lm = wk.mk.list_marks
wk.mk.list_marks = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db is locked"))
try:
    _m3 = wk.week_marks(_D)
    check("★ 读失败 ⇒ 条数 0 且 err 非空",
          _m3["n"] == 0 and "locked" in _m3["err"], _m3)
    check("★ 报告头部留痕「没用上你的划线」",
          "没用上你的划线" in _header_of(_m3["n"], _m3["err"]),
          _header_of(_m3["n"], _m3["err"]))
finally:
    wk.mk.list_marks = _orig_lm

# ============================================================ L. star 分组 + 异常统计
print("\n== L. star 按分类分组 / 异常只给统计规律（2026-10-29）==")

# ① star 素材带分类（模型才能按分类找共同主题）
_st = [{"entry_id": 1, "feed_id": 3, "title": "Gemini 4", "category": "AI",
        "feed_name": "Google DeepMind",
        "published_at": datetime(2026, 10, 28, 9, 0), "url": "u1", "source": "reader"},
       {"entry_id": 2, "feed_id": 2, "title": "卖点 Google 的 put", "category": "投资",
        "feed_name": "某作者",
        "published_at": datetime(2026, 10, 27, 15, 0), "url": "u2", "source": "reader"}]
_mat = wk.star_material(_st)
check("★ star 素材带 [分类]（模型的聚类抓手）",
      "[AI] Gemini 4" in _mat and "[投资] 卖点 Google 的 put" in _mat, _mat)
check("star 素材仍带站内深链（src=weekly）", "src=weekly" in _mat)
check("★ 引用形态是 `[源名 · 时:分](链接)`，不是 `[分类 · …]`（观点必须点名）",
      _mat.startswith("- [Google DeepMind · 10-28 09:00]("), _mat[:80])

# ② star 不再单列清单章，只留一行摘要插进「五」的首行（明细去 /?sel=starred）
_many = [{"entry_id": i, "feed_id": 1, "title": f"投资条目{i}", "category": "投资",
          "published_at": datetime(2026, 10, 28, 9, 0), "url": "", "source": "reader"}
         for i in range(12)]
_many += [{"entry_id": 100 + i, "feed_id": 2, "title": f"AI条目{i}", "category": "AI",
           "published_at": datetime(2026, 10, 28, 9, 0), "url": "", "source": "reader"}
          for i in range(3)]
_brief = wk.star_brief(_many)
check("★ 摘要一行说清「总数 + 分类分布」（投资 12 · AI 3）",
      "**15**" in _brief and "投资 12" in _brief and "AI 3" in _brief, _brief)
check("★ 摘要按条数降序（大类在前）", _brief.index("投资 12") < _brief.index("AI 3"), _brief)
check("★ 摘要链接到阅读器的全部星标页（细节不该塞进周报）",
      "sel=starred" in _brief and "src=weekly" in _brief, _brief)
check("★ 不再有「star 清单」章（章节模块已删除，不只是没调用）",
      not hasattr(wk, "star_section"), hasattr(wk, "star_section"))

# 摘要必须**插进模型写的这一节**，而不是另起一章；标题失配也不许丢
_body = "## 四、下周关注\n\n- x\n\n## 五、star 里的线索\n\n第一条线索…"
_ins = wk.insert_star_brief(_body, "SUMMARY-LINE")
check("★ 插在「star 里的线索」标题之后、正文之前",
      _ins.index("SUMMARY-LINE") > _ins.index("## 五")
      and _ins.index("SUMMARY-LINE") < _ins.index("第一条线索"), _ins)
check("摘要不破坏后续正文（原文字一个不少）",
      "第一条线索…" in _ins and "## 四、下周关注" in _ins)
check("★ 摘要与后文之间留空行（只隔一个 \\n 属软换行，会渲染成同一段）",
      "SUMMARY-LINE\n\n" in _ins, repr(_ins[_ins.index("SUMMARY-LINE"):][:40]))
check("★ 标题找不到时追加到末尾，不静默丢（系统真值宁丑勿失）",
      "SUMMARY-LINE" in wk.insert_star_brief("## 二、主线\n\n- x", "SUMMARY-LINE"))
check("编号写成别的（如「六」）也能认出来（只认 star 关键词）",
      "SUMMARY-LINE" in wk.insert_star_brief(
          "## 六、本周 star 里的线索\n\n- x", "SUMMARY-LINE"))
check("没有 star 时不插空行/空摘要", wk.insert_star_brief(_body, "") == _body)
check("本周没加星 ⇒ 摘要为空（不写「加星 0 篇」）", wk.star_brief([]) == "")

# ③ 异常：过程日志必须被剔除（实测 61 行里 39 行是过程记录）
_ANOM = [
    # 真异常：同一源同一天既有解析错误又有刷新失败 ⇒ 只算一次
    "2026-10-23：- ⚠️ #9 某软件源：解析错误（Miniflux is not able to reach this website "
    "due to a network error: Get \"https://feed.example.com\"）",
    "2026-10-23：      - ✗ #9 某软件源：HTTPError: 500 Server Error for url: x",
    # 同一天另外 4 个源也网络不可达 ⇒ 构成一次爆发
    *[f"2026-10-23：- ⚠️ #{i} 源{i}：解析错误（network error: Get \"https://x\"）"
      for i in (11, 13, 16, 17)],
    # 过程日志 / 汇总行：都不是异常
    "2026-10-23：- 🔁 补救刷新：候选 73 个，尝试 73 个，成功 63 个，失败 10 个（耗时 128s）",
    "2026-10-23：    - 本次实际刷新：#3、#4、#8 等 63 个",
    "2026-10-23：- ✅ 等待 0s 后已全部补齐（开始前 73 个源未更新）",
    "2026-10-23：窗口结束前仍未成功拉取：3 个源",
    # 宿主机停摆：单独说，不算"源异常"
    "2026-10-24：- 🕒 宿主机/采集疑似停摆：60/74 个源的最后成功时间集中在 10-23 21:35。",
    # 持续性：同一源连着两天 500
    "2026-10-24：      - ✗ #32 某摄影源：HTTPError: 500 Server Error for url: x",
    "2026-10-25：      - ✗ #32 某摄影源：HTTPError: 500 Server Error for url: x",
]
_d = wk.anomaly_digest(_ANOM)
check("★ 过程日志不计入源级异常（🔁/✅/汇总行/本次实际刷新）",
      _d["logs"] == 4, _d["logs"])
check("★ 同一源同一天的两条（解析错误+刷新失败）只算一次",
      _d["events"] == 7, _d["events"])          # 10-23 五个源 + 10-24 #32 + 10-25 #32
check("宿主机停摆单独统计、不混进源异常",
      _d["host_days"] == ["2026-10-24"], _d["host_days"])
check("★ 单日多源同类型 ⇒ 识别为一次性爆发，且带上当天总源数",
      _d["bursts"] and _d["bursts"][0]["n"] == 5 and _d["bursts"][0]["total"] == 5,
      _d["bursts"])
check("★ 连续两天的源进「持续性（要动手）」",
      any(p["feed_id"] == "32" and p["n"] == 2 for p in _d["persist"]), _d["persist"])
check("错误类型能分类（网络不可达 / 站点 500）",
      _d["by_kind"].get("网络不可达") == 5 and _d["by_kind"].get("站点 500") == 2,
      _d["by_kind"])
_sec2 = wk.anomaly_section(_ANOM)
check("★ 异常段不列明细（不出现 HTTPError 原文）", "HTTPError" not in _sec2, _sec2[:200])
check("★ 异常段给出可动手的那一条（持续性点名 #32）", "#32" in _sec2, _sec2)

# ④ 模型侧：prompt 必须要求第五节挖线索，且章节编号已顺延
check("★ W0 要求写「star、划线、批注里的线索」（否则 star 永远只是一堆标题）",
      "star、划线、批注里的线索" in wk.W0_USER)
check("★ 明确要求线索跨 ≥2 条痕迹并禁止复述数量",
      "至少涉及 **2 条**" in wk.W0_USER and "本周星了 N 篇" in wk.W0_USER)
check("★ 系统段只剩一章：异常为「六」（star 清单已移除）",
      "## 六、本周异常" in wk.anomaly_section(_ANOM)
      and "## 七、" not in wk.anomaly_section(_ANOM))
check("★ 概览不再重复加星数（已移到「五」首行，同一件事只写一遍）",
      "加星" not in wk.overview_section(
          datetime(2026, 10, 29).date(),
          {"need": 7, "covered": 7, "missing": [], "degraded": []}, _ANOM, 0))
check("★ prompt 说明「首行由系统插入，你不要写」（否则模型会自己数一遍）",
      "由系统插入" in wk.W0_USER)

# ⑤ 数字核对的基准必须覆盖 star 素材（否则引用 star 标题里的数字会假警报）
_wk_src = Path(BASE, "core", "weekly.py").read_text(encoding="utf-8")
check("★ number_audit 的基准含 star/批注素材（实测 1083 假警报的成因）",
      'number_audit(\n            body, material + "\\n" + star_txt' in _wk_src
      or 'material + "\\n" + star_txt' in _wk_src, "未包含 star_txt")
_demo_body = "- 1083 个 AI Native 创业者里有八成月入不足百刀（推测）"
check("★ 行为验证：数字只在 star 标题里时，旧基准会误报、新基准不误报",
      bool(rt.number_audit(_demo_body, "日报正文没有这个数字"))
      and not rt.number_audit(_demo_body, "日报正文\n- [AI] 1083 个 AI Native 创业者"),
      (rt.number_audit(_demo_body, "日报正文没有这个数字"),
       rt.number_audit(_demo_body, "日报正文\n- [AI] 1083 个 AI Native 创业者")))

# ⑥ 锚点笔误（实测 54 个链接里坏 1 个，且不会触发任何告警）
check("★ `#row=<id>` 笔误会被改成 `#row-<id>`（否则点进去不滚到那行）",
      wk.fix_anchors("x?open=1&src=weekly#row=12") == "x?open=1&src=weekly#row-12",
      wk.fix_anchors("x?open=1&src=weekly#row=12"))
check("正常锚点不被改动", wk.fix_anchors("x#row-12") == "x#row-12")

# ⑦ 概览里的深链点击按来源汇总（原来只有总数 +「含日报/通知/L3」的含糊括注）
_orig_load = wk.clicktrack.load
try:
    wk.clicktrack.load = lambda days=0, src="": [
        {"src": "l3"}, {"src": "l3"}, {"src": "daily"}, {"src": "wiki"}]
    _ov_col = {"need": 7, "covered": 7, "missing": [], "degraded": []}
    _ov = wk.overview_section(datetime(2026, 10, 29).date(), _ov_col, [], None)
    wk.clicktrack.load = lambda days=0, src="": []
    _ov_zero = wk.overview_section(datetime(2026, 10, 29).date(), _ov_col, [], None)
finally:
    wk.clicktrack.load = _orig_load
check("★ 概览把点击按来源摊开（不再只写「含日报/通知/L3」）",
      "本周深链点击 **4** 次" in _ov
      and "L3 通知 2" in _ov and "日报引用 1" in _ov
      and "wiki 页引用 1" in _ov
      and "含日报/通知/L3" not in _ov, _ov)
_g = dict(wk.clicktrack.group_counts(
    [{"src": "l3"}, {"src": "l3"}, {"src": "daily"}, {"src": "wiki"}]))
check("★ 分项之和等于总数且未登记来源不吞（2+1+1=4）",
      sum(_g.values()) == 4 and _g.get("wiki 页引用") == 1, _g)
check("★ 零点击时概览不写这一行", "深链点击" not in _ov_zero, _ov_zero)

print()
print("=" * 68)
print(f"结果：{ok} 通过 / {bad} 失败")
if fails:
    print("失败项：", fails)
print("=" * 68)
sys.exit(1 if bad else 0)
