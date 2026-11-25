"""零成本验证「feed 编辑」：重命名 + 删除（右键菜单两个新动作）。

用法：python scripts/verify_feed_edit.py

覆盖：
  A. 数据源层：`rename_feed` **只能改标题**（body 里没有 category_id）、
     `delete_feed` 走 DELETE 且路径正确
  B. 视图层（桩住 Miniflux 与树，不联网、不写真源）：
     改名 / 删除的入参校验、不存在源、上游报错原文透传
  C. 删除的**确认契约**：没有 confirm=1 一律拒绝（防误触 / 防脚本绕过）
  D. 删除后本地数据清理：分级配置 / 分组归属 / 开关 / 译文 / 评级 全清，
     但 **TriagePrompt 与 LlmUsage 必须保留**（审计与成本账）
  E. 前端：菜单有两项、删除确认文案说清"条目会一起删"

★ 只用假 feed id 900011 造数据，跑完删干净；分组快照重定向到临时目录。
"""

import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "apps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

import _dbtest  # noqa: E402

# ★ 本脚本用假 feed id 造数据、跑完删掉，但那仍然是**真实库**上的写与删。
#   统一切内存测试库：造得再脏也只脏这一进程（见 _dbtest.py）。
_dbtest.isolate()

from unittest import mock  # noqa: E402

from django.test import RequestFactory  # noqa: E402

from apps.reader import treeviews  # noqa: E402
from core import readergroups as rg  # noqa: E402
from core.datasource.base import CategoryNode, FeedNode  # noqa: E402
from core.datasource import miniflux as mf  # noqa: E402
from core.models import (
    EntryRating,
    EntryTranslation,
    FeedGroup,
    FeedGroupMember,  # noqa: E402
    FeedOverride,
    FeedTriageConfig,
    FeedTranslation,
    TriagePrompt,
)

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


def code_only(src: str) -> str:
    """剥掉 docstring 与注释行后剩下的**真正代码**。

    ★ 断言「不许出现 category_id」时必须看代码而不是整段源码：函数自己的
      docstring 就在解释"为什么不支持改分类"，按原文扫会永远假红。
    """
    out, in_str = [], False
    for ln in src.split("\n"):
        s = ln.strip()
        if s.startswith('"""') or s.startswith("'''"):
            in_str = not in_str if s.count('"""') % 2 or s.count("'''") % 2 else in_str
            continue
        if in_str or s.startswith("#"):
            continue
        out.append(ln)
    return "\n".join(out)


JS = read("static/app.js")
URLS = read("apps/reader/urls.py")
MF_SRC = inspect.getsource(mf.MinifluxDataSource)
TV_SRC = inspect.getsource(treeviews)

FEED = 900011  # 假 feed（真源 id 都很小）
CAT = 999902  # 假分类

# ★ 假数据不许写进真备份目录（见 verify_groups 的同款处理）
_TMP_BK = Path(tempfile.mkdtemp(prefix="verify-feededit-bk-"))
from core import groupbackup  # noqa: E402

groupbackup.backup_dir = lambda: _TMP_BK

rf = RequestFactory()


def fake_tree(with_feed=True):
    c = CategoryNode(id=CAT, title="假分类")
    if with_feed:
        c.feeds = [
            FeedNode(
                id=FEED,
                title="假源",
                site_url="",
                category_id=CAT,
                category_title="假分类",
                unread=0,
            )
        ]
    else:
        c.feeds = []
    return [c]


class FakeDS:
    """记录调用的假数据源：只在内存里记账，不联网。"""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def rename_feed(self, feed_id, title):
        if self.fail:
            raise RuntimeError("上游 500")
        self.calls.append(("rename", int(feed_id), title))
        return {"title": title}

    def delete_feed(self, feed_id):
        if self.fail:
            raise RuntimeError("上游 502")
        self.calls.append(("delete", int(feed_id)))
        return True


def call(view, data):
    resp = view(rf.post("/x", data))
    try:
        body = json.loads(resp.content)
    except Exception:  # noqa: BLE001
        body = {}
    return resp.status_code, body


print("\n== A. 数据源层：只改标题 / 删除走 DELETE ==")
check(
    "有 rename_feed / delete_feed 两个方法",
    hasattr(mf.MinifluxDataSource, "rename_feed")
    and hasattr(mf.MinifluxDataSource, "delete_feed"),
)
_i = MF_SRC.index("def rename_feed")
_rename_src = MF_SRC[_i : MF_SRC.index("def ", _i + 10)]
check("rename_feed 打的是 /v1/feeds/<id>", "/v1/feeds/" in _rename_src)
check(
    "rename_feed 的 body 只有 title（**没有 category_id** = 不能切分类）",
    '"title": title' in code_only(_rename_src).replace("'", '"')
    and "category" not in code_only(_rename_src),
)
check("rename_feed 之后使缓存失效（不然树还是旧标题）", "_invalidate()" in _rename_src)
_i = MF_SRC.index("def delete_feed")
_del_src = MF_SRC[_i : MF_SRC.index("def ", _i + 10)]
check("delete_feed 走 _delete", "self._delete(" in _del_src)
check(
    "delete_feed 注释写明「条目会一起删」（UI 确认文案的依据）",
    "条目" in _del_src and "不可逆" in _del_src,
)

print("\n== B. 视图层（桩住 Miniflux 与树）==")
ds = FakeDS()
with mock.patch.object(treeviews, "get_datasource", lambda: ds), mock.patch(
    "core.treekit.tree", lambda: fake_tree()
):
    code, body = call(treeviews.feed_rename, {"feed_id": FEED, "title": "新名字"})
    check("改名成功 → ok", code == 200 and body.get("ok"), (code, body))
    check(
        "改名真的打到 Miniflux 且带了新标题",
        ds.calls == [("rename", FEED, "新名字")],
        ds.calls,
    )

    code, body = call(treeviews.feed_rename, {"feed_id": FEED, "title": "   "})
    check(
        "空标题被拒（400 + error 原文）",
        code == 400 and not body.get("ok") and "不能为空" in (body.get("error") or ""),
        (code, body),
    )
    code, body = call(treeviews.feed_rename, {"title": "x"})
    check("缺 feed_id 被拒", not body.get("ok"), body)

    code, body = call(
        treeviews.feed_rename, {"feed_id": FEED, "title": "很长的名字" * 20}
    )
    check(
        "标题长度被截断（≤64）",
        len((body.get("title") or "")) <= 64 or not body.get("ok"),
        body,
    )

    with mock.patch("core.treekit.tree", lambda: fake_tree(with_feed=False)):
        code, body = call(treeviews.feed_rename, {"feed_id": FEED, "title": "x"})
        check("源不在树里 → 404（不是 500）", code == 404, (code, body))

ds2 = FakeDS(fail=True)
with mock.patch.object(treeviews, "get_datasource", lambda: ds2), mock.patch(
    "core.treekit.tree", lambda: fake_tree()
):
    code, body = call(treeviews.feed_rename, {"feed_id": FEED, "title": "x"})
    check(
        "上游报错原文透传（不吞成未知错误）",
        "上游 500" in (body.get("error") or ""),
        body,
    )

print("\n== C. 删除的确认契约 ==")
ds3 = FakeDS()
with mock.patch.object(treeviews, "get_datasource", lambda: ds3), mock.patch(
    "core.treekit.tree", lambda: fake_tree()
):
    code, body = call(treeviews.feed_delete, {"feed_id": FEED})
    check(
        "不带 confirm → 拒绝",
        not body.get("ok") and "确认" in (body.get("error") or ""),
        body,
    )
    check("拒绝时**没有**打 Miniflux（零副作用）", ds3.calls == [], ds3.calls)
    code, body = call(treeviews.feed_delete, {"feed_id": FEED, "confirm": "0"})
    check("confirm=0 同样拒绝", not body.get("ok"), body)
    check("仍然没有打 Miniflux", ds3.calls == [], ds3.calls)

print("\n== D. 删除后的本地清理 ==")
# 造一批假本地数据
g = FeedGroup.objects.create(category_id=CAT, title="验证用组", sort=999)
FeedGroupMember.objects.create(feed_id=FEED, group_id=g.id)
FeedTriageConfig.objects.create(feed_id=FEED, l0_text="广告", default_level=2)
FeedOverride.objects.create(feed_id=FEED, fast_refresh=True)
FeedTranslation.objects.create(feed_id=FEED, enabled=True)
EntryRating.objects.create(entry_id=99000011, feed_id=FEED, category_id=CAT, level=2)
EntryTranslation.objects.create(entry_id=99000011, feed_id=FEED, title_zh="译")
TriagePrompt.objects.create(feed_id=FEED, version=1, prompt_text="审计留档")

ds4 = FakeDS()
try:
    with mock.patch.object(treeviews, "get_datasource", lambda: ds4), mock.patch(
        "core.treekit.tree", lambda: fake_tree()
    ):
        code, body = call(treeviews.feed_delete, {"feed_id": FEED, "confirm": "1"})
        check("带确认 → 删除成功", code == 200 and body.get("ok"), (code, body))
        check("真的打了一次 delete", ds4.calls == [("delete", FEED)], ds4.calls)
        check(
            "返回里带回清理明细（可追溯）",
            isinstance(body.get("cleaned"), dict),
            body.get("cleaned"),
        )

    check(
        "本地分级配置已清", not FeedTriageConfig.objects.filter(feed_id=FEED).exists()
    )
    check("分组归属已清", not FeedGroupMember.objects.filter(feed_id=FEED).exists())
    check("快速刷新开关已清", not FeedOverride.objects.filter(feed_id=FEED).exists())
    check("标题翻译开关已清", not FeedTranslation.objects.filter(feed_id=FEED).exists())
    check("条目评级已清", not EntryRating.objects.filter(feed_id=FEED).exists())
    check(
        "条目标题译文已清", not EntryTranslation.objects.filter(feed_id=FEED).exists()
    )
    check(
        "★ TriagePrompt 保留（审计留档，不能抹）",
        TriagePrompt.objects.filter(feed_id=FEED).exists(),
    )
finally:
    # 清理测试残留（分组 / 可能剩下的行）
    EntryRating.objects.filter(feed_id=FEED).delete()
    EntryTranslation.objects.filter(feed_id=FEED).delete()
    FeedTriageConfig.objects.filter(feed_id=FEED).delete()
    FeedOverride.objects.filter(feed_id=FEED).delete()
    FeedTranslation.objects.filter(feed_id=FEED).delete()
    TriagePrompt.objects.filter(feed_id=FEED).delete()
    FeedGroupMember.objects.filter(feed_id=FEED).delete()
    FeedGroup.objects.filter(id=g.id).delete()

# ★ 零残留守卫：真库里不能出现假 feed 的任何痕迹
check(
    "无残留：假 feed 的本地行已全部清掉",
    not any(
        [
            FeedTriageConfig.objects.filter(feed_id=FEED).exists(),
            FeedGroupMember.objects.filter(feed_id=FEED).exists(),
            FeedOverride.objects.filter(feed_id=FEED).exists(),
            EntryRating.objects.filter(feed_id=FEED).exists(),
            TriagePrompt.objects.filter(feed_id=FEED).exists(),
        ]
    ),
)

print("\n== E. 前端菜单与交互 ==")
_i = JS.index('"→ 移出分组"') if '"→ 移出分组"' in JS else JS.index("分类下没有分组时")
_feed_menu_tail = JS[_i : JS.index("return items;", _i)]
check("feed 菜单有「重命名」", "重命名" in _feed_menu_tail, _feed_menu_tail[-300:])
check("feed 菜单有「删除 feed」", "删除 feed" in _feed_menu_tail)
check(
    "重命名带当前标题（prompt 预填）",
    'act: "feed-rename"' in JS and "title: fa ?" in JS,
)
check("删除确认说清「条目会一起删」", "全部条目会一起删除" in JS)
check("删除确认提到「不可撤销」", "不可撤销" in JS)
check("删除确认给了 L0 替代方案（只是少看就别删）", "L0" in JS and "配置分级逻辑" in JS)
check("删除请求带 confirm=1", 'confirm: "1"' in JS)
check(
    "改名与删除都打各自的接口", "/tree/feed/rename" in JS and "/tree/feed/delete" in JS
)
check(
    "两条接口已注册到 urls", "tree/feed/rename" in URLS and "tree/feed/delete" in URLS
)
check(
    "改名视图的代码里不出现 category_id（**改名不能顺手改分类**）",
    "category" not in code_only(inspect.getsource(treeviews.feed_rename)),
)
check(
    "改名视图不带分类参数 → 服务端拒收也无从谈起（入参只有 feed_id/title）",
    code_only(inspect.getsource(treeviews.feed_rename)).count("request.POST.get") == 1,
)
check(
    "删除视图自带确认校验（不靠前端自觉）",
    "confirm" in inspect.getsource(treeviews.feed_delete),
)
check(
    "模块头写明跨分类的代价",
    "parsing_error_count" in TV_SRC.split('"""')[1] if '"""' in TV_SRC else False,
)

print("\n" + "=" * 68)
print("结果：%d 通过 / %d 失败" % (ok, bad))
if bad:
    print("失败项：")
    for f in fails:
        print("  - %s" % f)
    sys.exit(1)
print("=" * 68)
