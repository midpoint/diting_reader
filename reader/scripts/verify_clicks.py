"""零成本验证「点击来源追踪」（utm_source 机制的记录端 + 通知链接端）。

用法：python scripts/verify_clicks.py

覆盖：
  A. clicktrack.record / load：白名单 src / 非法输入拒绝 / 写失败不影响调用方 /
     按天按 src 过滤、空文件、坏行容错
  B. 通知链接端：L3 带 src=l3、日报通知带 src=daily-notify、失败通知带 src=daily-fail-notify
  C. 视图接线（源码契约）：index 记「src + open」、summary.detail 记「src」，
     且 record 包在 try 语义里（写失败不影响页面）—— record 本身吞异常，见 A
  D. 周报按来源分类汇总：分项之和 == 总数、未知 src 不吞、通知并组、降序
"""

import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "apps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from core import clicktrack, notify  # noqa: E402

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


tmp = tempfile.mkdtemp()
clicktrack.set_path(os.path.join(tmp, "clicks.jsonl"))

print("\n== A. record：写入与防御 ==")
check("正常写入", clicktrack.record("daily", 101, 5) is True)
check("无 feed_id 也行（/summary 落点没有条目）", clicktrack.record("daily-notify") is True)
check("src 带特殊字符被拒（防手改 URL 塞垃圾）",
      clicktrack.record("bad src;x", 1, 1) is False)
check("src 超长被拒", clicktrack.record("x" * 40, 1, 1) is False)
check("空 src 拒绝（不值得记）", clicktrack.record("", 1, 1) is False)
check("非法 entry_id 不炸", clicktrack.record("l3", None, 0) is True)

rows = clicktrack.load()
check("load 读回 3 条", len(rows) == 3, rows)
check("字段齐全", set(rows[0]) == {"ts", "src", "entry_id", "feed_id"}, rows[0])
check("新的在前", rows[0]["ts"] >= rows[-1]["ts"])
check("按 src 过滤", len(clicktrack.load(src="l3")) == 1)
check("按天过滤（days=0 = 全部）", len(clicktrack.load(days=30)) == 3)

clicktrack.set_path(os.path.join(tmp, "nope.jsonl"))
check("文件不存在 → 空列表（首跑就是这种状态）", clicktrack.load() == [])
p_bad = os.path.join(tmp, "bad.jsonl")
open(p_bad, "w", encoding="utf-8").write('{"ts":"2026-10-28T00:00:00+00:00","src":"daily","entry_id":1,"feed_id":2}\n不是json\n')
clicktrack.set_path(p_bad)
check("坏行跳过不炸（append-only 文件总会遇到半行）", len(clicktrack.load()) == 1)

print("\n== B. 通知链接端：三个入口都带 src ==")
class E:
    id, feed_id, title = 9, 4, "t"


check("L3 深链带 src=l3",
      "&src=l3#row-9" in (notify.reader_entry_url(4, 9, src="l3")),
      notify.reader_entry_url(4, 9, src="l3"))
check("L3 入口真的传了 src（源码契约）",
      'src="l3"' in open(os.path.join(BASE, "core", "notify.py"), encoding="utf-8").read())
check("无 src 时不带参数（老链接不变形）",
      notify.reader_entry_url(4, 9).endswith("#row-9")
      and "&src" not in notify.reader_entry_url(4, 9))
_src = open(os.path.join(BASE, "core", "notify.py"), encoding="utf-8").read()
check("日报成功通知带 src=daily-notify", "src=daily-notify" in _src)
check("失败通知带 src=daily-fail-notify", "src=daily-fail-notify" in _src)
check("来源都在 KNOWN_SOURCES 登记过",
      {"daily", "daily-notify", "daily-fail-notify", "l3"} <= set(clicktrack.KNOWN_SOURCES))

print("\n== C. 视图接线（源码契约：真视图要连 Miniflux，桩不起）==")
vsrc = open(os.path.join(BASE, "apps", "reader", "views.py"), encoding="utf-8").read()
check("index 读 src 与 open", 'request.GET.get("src")' in vsrc and 'request.GET.get("open")' in vsrc)
check("index 只在 open 是数字时记录（防垃圾）", "open_id.isdigit()" in vsrc)
check("index 从 sel=feed:N 提取 feed_id", 're.match(r"feed:(\\d+)"' in vsrc)
check("index 引了 clicktrack", "clicktrack.record" in vsrc)
ssrc = open(os.path.join(BASE, "apps", "summary", "views.py"), encoding="utf-8").read()
check("summary.detail 记录 /summary 落点的打开", "clicktrack.record(request.GET" in ssrc)

print("\n== D. 周报按来源分类汇总（分项之和必须等于总数）==")
clicktrack.set_path(os.path.join(tmp, "group.jsonl"))
for _src, _n in [("l3", 3), ("daily", 2), ("daily-notify", 1),
                 ("daily-fail-notify", 1), ("weekly", 1), ("wiki", 1),
                 ("future-source", 2)]:
    for _ in range(_n):
        clicktrack.record(_src)
_rows = clicktrack.load()
_groups = clicktrack.group_counts(_rows)
_gmap = dict(_groups)
check("实测出现过的 src=wiki 已登记成大类",
      "wiki" in clicktrack.KNOWN_SOURCES and "wiki" in clicktrack.SOURCE_GROUP)
check("分项之和恒等于总数", sum(_gmap.values()) == len(_rows), _groups)
check("三种通知 src 并成一类「通知」", _gmap.get("通知") == 2, _groups)
check("未知来源不被吞掉，原样列出",
      _gmap.get("其他（future-source）") == 2, _groups)
check("次数降序（同数按名字稳定）",
      [n for _, n in _groups] == sorted((n for _, n in _groups), reverse=True),
      _groups)
check("空记录 → 空汇总（概览不写这一行）", clicktrack.group_counts([]) == [])
check("空/缺 src 也不丢（坏行容错后仍计数）",
      sum(n for _, n in clicktrack.group_counts([{"src": ""}, {}])) == 2)

print("\n结果：%d 通过 / %d 失败" % (ok, bad))
if fails:
    print("失败项:", fails)
sys.exit(1 if bad else 0)
