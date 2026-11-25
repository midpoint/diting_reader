"""零成本验证「标题翻译」：判定 / 解析 / 池子口径 / 幂等 / 数据模型 / 撞车退避。

用法：python scripts/verify_translate.py

★ 不调 LLM、不碰 Miniflux；只往**假 feed 999999** 写测试行，结束时删干净。
"""
import os
import sys
from datetime import timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "apps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402
django.setup()

import _dbtest  # noqa: E402

# ★ 翻译开关/译文都是真实库里的行；脚本用假 feed id 写它们，必须在测试库里写。
_dbtest.isolate()

from django.conf import settings  # noqa: E402
from django.utils import timezone  # noqa: E402

from core import costreport as cr  # noqa: E402
from core import translate as tr  # noqa: E402
from core.models import EntryTranslation, FeedTranslation, LlmUsage  # noqa: E402

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


print("=" * 68)
print("1. 标题判定：只翻「全英文/拉丁」标题，中文/日文/混排/纯数字跳过")
print("=" * 68)
cases = [
    ("Nvidia beats expectations as AI demand surges", True),
    ("英伟达业绩超预期", False),
    ("Nvidia 业绩超预期", False),          # 中英混排：v1 不翻
    ("半導体の輸出規制が緩和へ", False),      # 日文假名
    ("2026-10-23", False),
    ("https://example.com/a", False),
    ("OK", False),                        # 拉丁字母太少
    ("", False),
]
for title, want in cases:
    got, reason, cleaned = tr.check_title(title)
    check("判定 %r → %s" % (title[:24], "翻" if want else "不翻"),
          got is want, "got=%s reason=%s cleaned=%r" % (got, reason, cleaned))

check("HTML 实体/标签会清洗", tr.clean_title("<b>A &amp; B</b>") == "A & B")
check("超长标题跳过（不截半句）",
      not tr.check_title("A" * (settings.TRANSLATE_CHAR_LIMIT + 1))[0])
check("hash 对空白变化稳定",
      tr.title_hash("A  B") == tr.title_hash(" A B "))

print()
print("=" * 68)
print("2. 模型输出解析：JSON / 围栏 / 夹杂说明 / 缺 id / 非法输入")
print("=" * 68)
got = tr.parse_translations(
    '[{"id": 1, "zh": "英伟达业绩超预期"}, {"id": 2, "zh": "译文：AI 需求旺盛"}]')
check("标准 JSON 数组", got.get(1) == "英伟达业绩超预期" and got.get(2) == "AI 需求旺盛", got)
got = tr.parse_translations('```json\n[{"id": 3, "zh": "标题"}]\n```')
check("剥 markdown 围栏", got.get(3) == "标题", got)
got = tr.parse_translations('好的，结果如下：\n[{"id": 4, "zh": "标题"}]\n以上。')
check("夹在说明文字里也能抓出来", got.get(4) == "标题", got)
got = tr.parse_translations('[{"id": "5", "title_zh": "标题"}]')
check("id 字符串 / title_zh 别名", got.get(5) == "标题", got)
check("非法 JSON → 空 dict", tr.parse_translations("not json") == {})
check("空译文被丢掉", tr.parse_translations('[{"id": 6, "zh": "   "}]') == {})
check("加解释的超长译文判无效",
      tr._invalid_zh("这是一段明显超出标题长度的解释文字" * 10, "Short title"))

print()
print("=" * 68)
print("3. 独立开销池：各池 / translate_title 归属 / 预算键")
print("=" * 68)
check("LlmUsage 有 title 翻译 kind",
      "translate_title" in dict(LlmUsage.KINDS))
# ★ 期望值从源码推导，不写死"三池"：加池子（周报）不该让这里假红。
#   真正要守的机制是"每个 kind 都归属某个池、池之间不重叠、ALL_POOLS 与 POOL_KINDS 同源"。
pooled_kinds = {k for kinds in cr.POOL_KINDS.values() for k in kinds}
check("每个 kind 都归属某个池（新 kind 不会漏记账）",
      {k for k, _ in LlmUsage.KINDS} <= pooled_kinds)
check("池之间不重叠", sum(len(v) for v in cr.POOL_KINDS.values()) == len(pooled_kinds))
check("ALL_POOLS 与 POOL_KINDS 同源", set(cr.ALL_POOLS) == set(cr.POOL_KINDS))
check("translate 池只归 translate_title",
      cr.POOL_KINDS[cr.POOL_TRANSLATE] == ("translate_title",))
check("预算键存在", cr.BUDGET_KEY_TRANSLATE == "translate_budget_cny")
check("全表口径 = 各池之和",
      abs(cr.budget_cny() - sum(cr.pool_budget_cny(p) for p in cr.ALL_POOLS)) < 1e-9)
check("budget_status 返回全部池", len(cr.budget_status()["pools"]) == len(cr.ALL_POOLS))

print()
print("=" * 68)
print("4. 数据模型 / 幂等口径（假 feed 999999，结束删除）")
print("=" * 68)
FAKE = 999999
FAKE_ENTRY = 999999001
now = timezone.now()
try:
    FeedTranslation.objects.filter(feed_id=FAKE).delete()
    EntryTranslation.objects.filter(feed_id=FAKE).delete()
    FeedTranslation.objects.create(feed_id=FAKE, enabled=True, enabled_at=now)
    ft = FeedTranslation.objects.get(feed_id=FAKE)
    check("FeedTranslation 默认进度为 0 / 未完成",
          ft.backfill_done_count == 0 and ft.backfilled_at is None)

    EntryTranslation.objects.create(
        entry_id=FAKE_ENTRY, feed_id=FAKE, status="done",
        source_title="Nvidia beats", source_hash=tr.title_hash("Nvidia beats"),
        title_zh="英伟达超预期")
    check("translations_for 只返回已开启 feed 的译文",
          FAKE_ENTRY in tr.translations_for([FAKE_ENTRY]))
    FeedTranslation.objects.filter(feed_id=FAKE).update(enabled=False)
    check("关闭后 translations_for 返回空",
          tr.translations_for([FAKE_ENTRY]) == {})
    check("关闭后详情也不展示",
          tr.translation_for_entry(FAKE_ENTRY, FAKE) is None)
    FeedTranslation.objects.filter(feed_id=FAKE).update(enabled=True)

    # _collect：created_at 早于 enabled_at = 回填；晚于 = 新文章
    from core.datasource.base import Entry

    def fake_entry(eid, title, created):
        return Entry(id=eid, feed_id=FAKE, feed_title="fake", title=title,
                     url="", published_at=now, created_at=created, status="unread")

    old = fake_entry(FAKE_ENTRY + 1, "Old English title", now - timedelta(days=1))
    new = fake_entry(FAKE_ENTRY + 2, "New English title", now + timedelta(seconds=1))
    skipped = fake_entry(FAKE_ENTRY + 3, "中文标题", now - timedelta(days=1))
    news, backs = tr._collect(ft, [old, new, skipped])
    check("created_at < enabled_at → 回填候选", [x["id"] for x in backs] == [old.id])
    check("created_at >= enabled_at → 新文章候选", [x["id"] for x in news] == [new.id])
    check("中文条目不进候选", skipped.id not in [x["id"] for x in news + backs])

    done = fake_entry(FAKE_ENTRY, "Nvidia beats", now - timedelta(days=1))
    news2, backs2 = tr._collect(ft, [done])
    check("已译且 hash 未变 → 不再翻", news2 == [] and backs2 == [])

    # _collect 跳过原因汇总进 DEBUG 日志（回答「标题为啥没翻」要有迹可寻）
    import logging  # noqa: E402

    class _Cap(logging.Handler):
        def __init__(self):
            super().__init__(level=logging.DEBUG)
            self.msgs = []

        def emit(self, record):
            self.msgs.append(record.getMessage())

    cap = _Cap()
    tr.log.addHandler(cap)
    try:
        tr._collect(ft, [old, new, skipped, done])
        skip_logs = [m for m in cap.msgs if "跳过" in m]
        check("有跳过时记一条 DEBUG 汇总（含原因计数）",
              len(skip_logs) == 1 and "not_needed=1" in skip_logs[0]
              and "done=1" in skip_logs[0], skip_logs)
        cap.msgs.clear()
        tr._collect(ft, [new])
        check("无跳过时不记汇总", not [m for m in cap.msgs if "跳过" in m],
              cap.msgs)
    finally:
        tr.log.removeHandler(cap)

    # 饿死告警：连跳 6 轮 tick → 1 条 WARNING；真跑一轮 → 清零重计
    tr._tick_skip["streak"] = 0
    warn_cap = _Cap()
    tr.log.addHandler(warn_cap)
    try:
        for _i in range(5):
            tr._note_tick_skipped("triage 正在跑")
        check("连跳 5 轮未到阈值不告警",
              not [m for m in warn_cap.msgs if "连续" in m], warn_cap.msgs)
        tr._note_tick_skipped("triage 正在跑")
        check("连跳 6 轮升 WARNING", len(
            [m for m in warn_cap.msgs if "连续 6 轮" in m]) == 1, warn_cap.msgs)
        tr._note_tick_ran()
        tr._note_tick_skipped("triage 正在跑")
        check("跑过一轮后重新计数",
              len([m for m in warn_cap.msgs if "连续" in m]) == 1, warn_cap.msgs)
    finally:
        tr.log.removeHandler(warn_cap)
        tr._tick_skip["streak"] = 0

    counts = tr.feed_state_map().get(FAKE, {})
    check("feed_state_map 带已译条数与开关",
          counts.get("count") == 1 and counts.get("enabled") is True, counts)
finally:
    EntryTranslation.objects.filter(feed_id=FAKE).delete()
    FeedTranslation.objects.filter(feed_id=FAKE).delete()

print()
print("=" * 68)
print("5. worker 端到端（假 LLM / 假数据源，零网络零费用）")
print("=" * 68)
import json  # noqa: E402
from core.datasource.base import CategoryNode, Entry, FeedNode  # noqa: E402

W = 999998
w_now = timezone.now()
w_entries = [
    Entry(id=W * 10 + i, feed_id=W, feed_title="fake", title=f"English title {i}",
          url="", published_at=w_now - timedelta(hours=i),
          created_at=w_now - timedelta(days=1), status="unread")
    for i in range(3)
]


class _FakeDS:
    def list_entries(self, **kw):
        return list(w_entries)

    def list_tree(self):
        return [CategoryNode(id=7, title="c", feeds=[
            FeedNode(id=W, title="fake", site_url="", category_id=7,
                     category_title="c")])]


_orig = (tr.llm.chat, tr.llm.record, tr._budget_stop, tr.new_datasource)
_w_calls = []


def _fake_chat(messages, **kw):
    ids = [json.loads(line)["id"] for line in messages[1]["content"].splitlines()
           if line.startswith("{")]
    text = json.dumps([{"id": i, "zh": f"中文标题{i % 10}"} for i in ids],
                      ensure_ascii=False)
    # llm.chat 的返回是 (text, usage, err, tries)；tries=1 即一次成功
    return text, {"prompt_tokens": 100, "completion_tokens": 50}, "", 1


def _fake_record(kind, **kw):
    _w_calls.append((kind, kw.get("feed_id"), kw.get("entry_id")))
    return None


try:
    tr.llm.chat = _fake_chat
    tr.llm.record = _fake_record
    tr._budget_stop = lambda: False
    tr.new_datasource = lambda: _FakeDS()
    FeedTranslation.objects.filter(feed_id=W).delete()
    EntryTranslation.objects.filter(feed_id=W).delete()
    FeedTranslation.objects.create(feed_id=W, enabled=True, enabled_at=w_now,
                                   backfill_max=2)
    res = tr._run_feed(W, mode="backfill", ds=_FakeDS(), category_id=7)
    ft = FeedTranslation.objects.get(feed_id=W)
    check("回填只翻 N 条（N=2）", EntryTranslation.objects.filter(feed_id=W).count() == 2)
    check("worker 成功计数", res.get("ok") == 2 and res.get("calls") == 1, res)
    check("回填进度落库 / 标记完成",
          ft.backfill_done_count == 2 and ft.backfilled_at is not None, ft.__dict__)
    check("一次调用只记一行账（entry_id=None）",
          _w_calls == [("translate_title", W, None)], _w_calls)

    # ★ tick 也必须遵守剩余 N（不能一次把 3 条旧条目都翻掉）
    FeedTranslation.objects.filter(feed_id=W).update(
        backfilled_at=None, backfill_done_count=0)
    EntryTranslation.objects.filter(feed_id=W).delete()
    _w_calls.clear()
    tr._run_feed(W, mode="tick", ds=_FakeDS(), category_id=7)
    check("tick 回填也遵守 N=2",
          EntryTranslation.objects.filter(feed_id=W).count() == 2 and len(_w_calls) == 1,
          (_w_calls, EntryTranslation.objects.filter(feed_id=W).count()))
    # ★ 回填完成后，tick 不能再把当初因 N 上限没翻的旧条目继续翻下去
    _w_calls.clear()
    tr._run_feed(W, mode="tick", ds=_FakeDS(), category_id=7)
    check("回填完成后不再翻 N 之外的旧条目",
          EntryTranslation.objects.filter(feed_id=W).count() == 2 and not _w_calls,
          _w_calls)

    # ★ 回填完成后，失败到期的条目仍要重试（不占 N，也不能被「已完成」挡住）
    third = w_entries[2]
    EntryTranslation.objects.update_or_create(
        entry_id=third.id,
        defaults={"feed_id": W, "source_title": third.title,
                  "source_hash": tr.title_hash(third.title), "title_zh": "",
                  "status": "failed", "attempts": 1,
                  "next_try_at": w_now - timedelta(minutes=1), "error": "模型漏 id"})
    _w_calls.clear()
    tr._run_feed(W, mode="tick", ds=_FakeDS(), category_id=7)
    again = EntryTranslation.objects.get(entry_id=third.id)
    check("回填完成后失败条目会重试并成功",
          again.status == "done" and again.title_zh and len(_w_calls) == 1,
          (again.status, again.title_zh, _w_calls))

    # 预算耗尽：不写译文、不置完成、30 分钟后再试
    tr._budget_stop = lambda: True
    FeedTranslation.objects.filter(feed_id=W).update(
        backfilled_at=None, backfill_done_count=0, next_scan_at=None)
    EntryTranslation.objects.filter(feed_id=W).delete()
    _w_calls.clear()
    res2 = tr._run_feed(W, mode="backfill", ds=_FakeDS(), category_id=7)
    ft2 = FeedTranslation.objects.get(feed_id=W)
    check("预算耗尽：零译文 / 零调用",
          EntryTranslation.objects.filter(feed_id=W).count() == 0 and not _w_calls)
    check("预算耗尽：不算回填完成 + 退避 30 分钟",
          ft2.backfilled_at is None and ft2.next_scan_at is not None
          and res2.get("budget_stop") is True)
finally:
    tr.llm.chat, tr.llm.record, tr._budget_stop, tr.new_datasource = _orig
    EntryTranslation.objects.filter(feed_id=W).delete()
    FeedTranslation.objects.filter(feed_id=W).delete()

print()
print("=" * 68)
print("6. 边界：只翻标题、不翻正文")
print("=" * 68)
src = open(os.path.join(BASE, "core", "translate.py"), encoding="utf-8").read()
check("模型提示只要求翻标题", "标题" in tr.SYSTEM_PROMPT and "正文" not in tr.SYSTEM_PROMPT)
check("EntryTranslation 没有 content/body 字段",
      not any(f.name in ("content", "body", "content_zh") for f in
              EntryTranslation._meta.get_fields()))
check("翻译 worker 不引用 entry.content", "entry.content" not in src)

print()
print("=" * 68)
print("7. 撞车退避：tick 被 triage 挡住时先等再试（不整轮放弃）")
print("=" * 68)
import threading  # noqa: E402
import time  # noqa: E402
from core import triage  # noqa: E402

check("triage 空闲时无需等待直接放行",
      tr._wait_triage_done(timeout=0, poll=0.01) is True)
triage._RUN_LOCK.acquire()
try:
    t0 = time.monotonic()
    check("triage 占线时等到 timeout 放弃（不无限等）",
          tr._wait_triage_done(timeout=0.3, poll=0.05) is False
          and 0.2 <= time.monotonic() - t0 < 2,
          "%.2fs" % (time.monotonic() - t0))

    def _release_later():
        time.sleep(0.15)
        triage._RUN_LOCK.release()

    threading.Thread(target=_release_later, daemon=True).start()
    check("triage 中途跑完 → 等到路",
          tr._wait_triage_done(timeout=3, poll=0.05) is True)
finally:
    if triage._RUN_LOCK.locked():
        triage._RUN_LOCK.release()
sched_src = open(os.path.join(BASE, "core", "scheduler.py"),
                 encoding="utf-8").read()
check("注册首跳错开半周期（避让 triage 相位）",
      "TRANSLATE_TICK_SEC // 2" in sched_src and "next_run_time" in sched_src)

print()
print("=" * 68)
print("结果：%s 项通过，%s 项失败" % (ok, bad))
if fails:
    print("失败：")
    for f in fails:
        print("  - " + f)
print("=" * 68)
sys.exit(1 if bad else 0)
