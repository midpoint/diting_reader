"""零成本验证「界面外壳」：左侧导航 / 树按页显示 / L 标记 / 级别多选 / 开销窗口 / 总结页入口。

用法：python scripts/verify_ui_shell.py

背景（2026-10-15 需求）：
  第一轮：把 阅读/开销/总结 提成最左侧一列 icon+文字导航，树只在阅读页出现，底部加设置入口（占位）；
          开销补「今日」窗口；总结页去掉「自动补跑近 N 天」「生成今天」两个入口，重跑改异步
          （不再跳到只有 JSON 的页面）；已配置分级判据的源在树上加一个 L 标记。
  第二轮：L 标记**改低调**（原来是 accent 实心块，太抢眼）；工具栏级别筛选从「L0+/L1+/L2+/L3 阈值」
          改成**多选下拉**（能只看 L0、能 L1+L3）；左下设置入口换成**齿轮**图标。

覆盖：
  A. 模板 / 静态资源源码契约（不依赖网络）
  B. 树模板渲染（喂假数据，完全离线）：L 标记的**位置**与文案
  C. Django test client 渲染：三页的导航高亮 / 树有无 / 入口有无
  D. 级别多选：URL 解析 / 标签 / 查询串合成（纯函数，零依赖）
  E. 数据层：哪些源该被标 L（判据自己写的或分类继承的）
"""
import inspect
import os
import re
import sys
from datetime import timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "apps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402
django.setup()

# ★ 本脚本**不能**切内存测试库（和 verify_weekly/feed_edit 那几个相反）：
#   它是用 Django test client 渲染**真实页面**来验外壳，数据空了一切断言就没意义
#   （"7 根柱子""周报行"都得靠真数据才存在）。所以这里的纪律不是"隔离"，
#   而是**只读 + 写前快照、写后精确还原**（见下方探针段的 try/finally）。

from django.template.loader import render_to_string  # noqa: E402
from django.test import Client, RequestFactory  # noqa: E402
from django.utils import timezone  # noqa: E402

from core import costreport as cr  # noqa: E402
from core import treeorder  # noqa: E402
from core.datasource.base import CategoryNode, FeedNode  # noqa: E402
from reader import views as rv  # noqa: E402
from reader.templatetags.reader_tags import reldt  # noqa: E402
from costs import views as cv  # noqa: E402

ok = bad = skip = 0
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


def soft(name, cond, detail="", why=""):
    """需要外部服务（Miniflux）的检查：拿不到数据时记为 SKIP 而不是失败。"""
    global ok, bad, skip
    if detail == "__SKIP__":
        skip += 1
        print("  SKIP  %s   （%s）" % (name, why))
        return
    check(name, cond, detail)


def read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


BASE_HTML = read("templates/base.html")
TREE_BODY = read("templates/reader/_tree_body.html")
TREE_FEED = read("templates/reader/_tree_feed.html")
INDEX_TMPL = read("templates/reader/index.html")
MARK_FORM = read("templates/reader/_mark_form.html")
CSS = read("static/app.css")
JS = read("static/app.js")
S_INDEX = read("templates/summary/index.html")
S_DETAIL = read("templates/summary/detail.html")


print("\n== A. 模板 / 静态资源源码契约 ==")
check("base.html 有最左一列导航 .rail", 'class="rail"' in BASE_HTML)
for label in ("阅读", "开销", "总结", "设置"):
    check(f"导航含「{label}」", f"<span>{label}</span>" in BASE_HTML)
check("导航是 icon（内联 svg）+ 文字", BASE_HTML.count("<svg") >= 4)
check("设置入口是模态弹窗（openModal('/prefs')，不再跳整页）",
      "openModal('/prefs')" in BASE_HTML
      and 'disabled' not in BASE_HTML.split("openModal('/prefs')")[0][-200:])
check("base.html 已删掉旧的 .side-nav", "side-nav" not in BASE_HTML)
check("树由 show_tree 控制（树只在阅读页）",
      '{% if show_tree %}<aside class="sidebar">' in BASE_HTML)

check("CSS 有 .rail", ".rail {" in CSS)
check("CSS 有 .rail-item.active 高亮", ".rail-item.active" in CSS)
check("CSS 已删掉 .side-nav（无死样式）", ".side-nav" not in CSS)

# --- 品牌位 + favicon：谛听 = 白犬（2026-10-16 二版：全站只此一份 SVG）---
check("导航顶部有品牌位 .brand（32px）",
      'class="brand"' in BASE_HTML
      and 'src="/static/img/diting-logo.svg"' in BASE_HTML
      and 'width="32" height="32"' in BASE_HTML)
check("head 声明了 SVG favicon（与品牌位同一份 diting-logo.svg）",
      'rel="icon" type="image/svg+xml" href="/static/img/diting-logo.svg"' in BASE_HTML)
check("head 有 PNG 32px 兜底（书签/老渲染器不吃 SVG）",
      'rel="icon" type="image/png" sizes="32x32" href="/static/img/favicon-32.png"' in BASE_HTML)
check("head 有 apple-touch-icon（iOS 添加主屏用）",
      'rel="apple-touch-icon" href="/static/img/apple-touch-icon.png"' in BASE_HTML)
check("head 有 theme-color（站点 accent，移动端地址栏染色）",
      'name="theme-color" content="#185fa5"' in BASE_HTML)
check("旧的两版 SVG 都已不在（diting-favicon.svg 已并入 diting-logo.svg）",
      not os.path.exists("static/favicon.svg") and not os.path.exists("static/img/diting-favicon.svg"))
LOGO = read("static/img/diting-logo.svg")
check("logo 是新白犬（浅蓝底 #6E96C9 + 米白犬身 #F4F1EA + 橙点 #E4572E）",
      'fill="#6E96C9"' in LOGO and 'fill="#F4F1EA"' in LOGO and 'fill="#E4572E"' in LOGO)
check("logo 用纯属性写法（不用 <style>：favicon 渲染器支持不一）",
      "<style" not in LOGO)
check("三张 PNG 兜底都在盘上（favicon-32 / apple-touch / 通知用 256）",
      all(os.path.exists(os.path.join(BASE, p)) for p in
          ("static/img/favicon-32.png", "static/img/apple-touch-icon.png",
           "static/favicon-256.png")))
check("CSS 有 .brand 样式（分割线 + 32px 圆角）",
      ".rail .brand img" in CSS)

# --- 设置入口：齿轮图标 ---
check("设置入口用齿轮（feather settings 齿形 path）", "M19.4 15a1.65" in BASE_HTML)
check("设置入口已去掉旧的「圆点放射线」图标", "M12 2.8v2.4" not in BASE_HTML)

# --- 深链自动展开：L3 系统通知点进来（2026-10-16）---
# 通知里的 URL 是 `/?sel=feed:<源>&open=<条目>#row-<条目>`，三段各依赖一个 DOM 锚点，
# 任何一个改名都会让"点通知进去自动展开"静默失效 —— 所以在这里钉住。
_ROW = read("templates/reader/_row.html")
check("行锚点 id=row-<条目>（深链的 #row- 落在它上面）",
      'id="row-{{ row.entry.id }}"' in _ROW)
check("行内有 .entry-head（hx-post 挂在它身上 —— 自动展开点的就是它；"
      "POST + CSRF：GET 不带「标已读」副作用，预取/爬虫不会误触发）",
      'class="entry-head"' in _ROW and 'hx-post="/hx/entry/' in _ROW
      and "X-CSRFToken" in _ROW)
check("正文容器 id=detail-<条目>（与 open 参数用同一个 id 对齐）",
      'class="entry-detail" id="detail-{{ row.entry.id }}"' in _ROW)
check("app.js 读 URL 的 open 参数自动展开那一条",
      "function openFromUrl()" in JS and 'get("open")' in JS)
check("app.js 的自动展开**晚于** htmx 绑定（否则 hx-post 还没挂上，静默无效）",
      'document.addEventListener("DOMContentLoaded", function () {' in JS
      and "openFromUrl();" in JS and "aiFromUrl();" in JS)
check("app.js 自动展开后把该行滚到容器顶部（只针对通知点进来的那一条）",
      "pendingOpenId" in JS and 'target.id === "detail-" + pendingOpenId' in JS)

# --- L 标记 ---
check("CSS 有 .lmark（树上的 L 标记）", ".lmark {" in CSS)
_lm_block = re.search(r"\.lmark \{(.*?)\}", CSS, re.S)
_lm_css = _lm_block.group(1) if _lm_block else ""
check("L 标记低调：不再用 accent 实心填充",
      "background: var(--accent)" not in _lm_css and "background: transparent" in _lm_css,
      _lm_css.strip().replace("\n", " ")[:90])
check("L 标记低调：1px 弱边框 + muted 文字",
      "border: 1px solid var(--line)" in _lm_css and "color: var(--muted)" in _lm_css)
check("L 标记低调：字号 <= 10px", bool(re.search(r"font-size: (\d+)px", _lm_css))
      and int(re.search(r"font-size: (\d+)px", _lm_css).group(1)) <= 10)
check("_tree_body.html 通过 include 复用 _tree_feed.html（裸 feed 与组内 feed 同一段）",
      TREE_BODY.count('{% include "reader/_tree_feed.html" %}') == 2)
check("_tree_feed.html 在 favicon 与标题之间插 L 标记",
      bool(re.search(r'class="favicon"[^>]*>\s*\{% if f\.has_cfg %\}.*?lmark.*?\{% endif %\}\s*<a ',
                     TREE_FEED, re.S)))
check("L 标记文案来自后端标记字段 f.has_cfg / f.cfg_from",
      "f.has_cfg" in TREE_FEED and "f.cfg_from" in TREE_FEED)

# --- 分类 / feed 增改 + 拖动（2026-10-20）---
check("树上 feed 可拖动（draggable + data-cat）",
      'draggable="true"' in TREE_FEED and 'data-cat="{{ cat.id }}"' in TREE_FEED)
check("分类 / feed 写操作接口已接",
      all(x in JS for x in ("/tree/category/create", "/tree/category/rename",
                            "/tree/feed/create", "/tree/feed/move")))
check("树空白处右键有「新建分类…」",
      'act: "cat-create"' in JS and "新建分类…" in JS)
check("分类与分组菜单都有「添加 feed…」", JS.count('act: "feed-add"') == 2,
      JS.count('act: "feed-add"'))
check("跨分类拖动被前端拦住", "跨分类移动暂未开放" in JS)
check("空分类有提示行", "空分类（右键添加 feed / 移除）" in TREE_BODY)
check("分类节点带 data-empty", 'data-empty=' in TREE_BODY)
check("空分类可右键移除", 'act: "cat-delete"' in JS and "/tree/category/delete" in JS)
check("空分类菜单不显示「立即刷新本分类」",
      "if (empty)" in JS and "移除分类…" in JS)
check("拖放落点有高亮样式", "drop-ok" in CSS and ".node.feed.dragging" in CSS)
check("树上「全部文章」下面有星标入口",
      "★ 星标文章" in TREE_BODY and "sel=starred" in TREE_BODY)
check("_scope 支持 starred", rv._scope("starred") == ("starred", None))
check("失效的选中范围会被判为不存在",
      not rv._sel_exists("cat", 99999, [])
      and not rv._sel_exists("feed", 99999, [])
      and not rv._sel_exists("group", 99999, []))
check("星标节点右键不弹菜单", 'classList.contains("star")' in JS)
check("移除当前分类后回「全部文章」", "afterCategoryDelete" in JS)
check("列表按 published_at 排序（不按抓取时间）",
      'order="created_at"' not in inspect.getsource(rv._load_entries))
check("列表显示发布时间", "published_at|reldt" in read("templates/reader/_row.html"))
check("未来时间显示「后」",
      reldt(timezone.now() + timedelta(days=2)).endswith("天后"),
      reldt(timezone.now() + timedelta(days=2)))

# --- 级别多选下拉 ---
check("工具栏不再用 <select name=\"level\"> 阈值控件", 'name="level"' not in INDEX_TMPL)
check("工具栏有级别多选下拉（4 个勾选框，模板循环出来的）",
      INDEX_TMPL.count('type="checkbox" data-lv="{{ value }}"') == 1
      and '{% for value, name, desc in level_filters %}' in INDEX_TMPL,
      INDEX_TMPL.count('data-lv="{{ value }}"'))
check("下拉按钮标签位 data-lv-label（JS 会改）", "data-lv-label" in INDEX_TMPL)
check("提交的是 hidden levels（不是 4 个同名 checkbox）",
      'type="hidden" name="levels"' in INDEX_TMPL)
check("下拉菜单带 data-keep-open（多选时不许点一下就关）",
      "data-keep-open" in INDEX_TMPL)
check("有「全部级别」复位按钮", "data-lv-reset" in INDEX_TMPL)
check("标记为已读表单同步传 levels", 'name="levels"' in MARK_FORM and 'name="level"' not in MARK_FORM)

check("CSS 有 .lv-menu / .lv-opt", ".lv-menu {" in CSS and ".lv-opt {" in CSS)
check("JS 在**捕获阶段**同步级别（必须早于 htmx 读表单）",
      bool(re.search(r'addEventListener\("change",[\s\S]{0,200}?\}, true\)', JS)),
      "没找到 capture=true 的 change 监听")
check("JS 有 syncLevelsFromUrl（前进/后退回填）", "function syncLevelsFromUrl" in JS)
check("JS 拦住「取消最后一个级别」", "至少要保留一个级别" in JS)
check("JS 关闭菜单时跳过 data-keep-open", '.menu[data-keep-open]' in JS)

# --- 模板注释：`{# #}` 只吃单行，多行必须写 {% comment %} ---
#   本轮真踩到：base.html 里写了一段多行 {# ... #}，结果那段中文说明**原样渲染**到了页面上
#   （rail 旁边一大段大写文字），HTML 检查全绿、只有截图能看出来。所以固化成断言。
import glob  # noqa: E402

multiline_comment = []
for _p in glob.glob(os.path.join(BASE, "templates", "**", "*.html"), recursive=True):
    for _n, _line in enumerate(open(_p, encoding="utf-8"), 1):
        if "{#" in _line and "#}" not in _line.split("{#", 1)[1]:
            multiline_comment.append("%s:%d" % (os.path.relpath(_p, BASE), _n))
check("模板里没有「多行 {# #}」注释（Django 不认，会渲染成正文）",
      not multiline_comment, multiline_comment)


print("\n== B. 树模板离线渲染：L 标记位置与文案 ==")
tree = [CategoryNode(id=2, title="投资")]
tree[0].feeds = [
    FeedNode(id=2, title="雪球源", site_url="", category_id=2, category_title="投资",
             icon_url="/icon/2", has_cfg=True, cfg_from="分类「投资」"),
    FeedNode(id=99, title="普通源", site_url="", category_id=2, category_title="投资",
             icon_url="/icon/99", has_cfg=False),
]
html = render_to_string("reader/_tree_body.html", {
    # ★ 走 `treeorder.apply()`：树上「分类根下裸着的那一段」渲染的是
    #   `cat.root_feeds`（视图层切的片），和侧栏真实路径一致。
    "tree": treeorder.apply(tree), "sel_kind": "all", "sel_id": None, "total_unread": 0,
    "qs": "status=all", "fast_feeds": {},
})
check("已配置的源渲染出 L 标记", 'class="lmark"' in html)
check("未配置的源没有 L 标记", html.count('class="lmark"') == 1, html.count('class="lmark"'))
check("L 标记紧跟在 favicon 之后",
      bool(re.search(r'class="favicon"[^>]*>\s*<span class="lmark"', html)),
      re.findall(r'class="favicon"[^>]*>\s*<span[^>]*>', html))
check("L 标记在标题 <a> 之前",
      bool(re.search(r'class="lmark"[^>]*>L</span>\s*<a ', html)))
check("L 标记用 title 说明来源",
      'title="已配置分级判据（分类「投资」）"' in html,
      re.findall(r'class="lmark" title="[^"]*"', html))


print("\n== C. 页面渲染（Django test client）==")
# ★ test client 的 host 是 testserver；ALLOWED_HOSTS 默认只听回环（2026-10-24
#   收紧后），不调这个会被 DisallowedHost 打成一片 400。manage.py test 会自动做。
from django.test.utils import setup_test_environment  # noqa: E402

setup_test_environment()
client = Client()


def get(path):
    try:
        r = client.get(path)
        return r.status_code, r.content.decode("utf-8", "replace"), ""
    except Exception as exc:  # noqa: BLE001  外部依赖挂了不该让脚本崩
        return 0, "", str(exc)


code, page, err = get("/costs?days=7")
check("/costs 200", code == 200, f"{code} {err}")
if code == 200:
    check("/costs 无模板标记泄漏（{ { / { % / { # ）",
          not re.search(r"\{[#{%]", page), re.findall(r"\{[#{%]", page)[:3])
    check("/costs 有左侧导航（4 项）", page.count('class="rail-item') >= 4,
          page.count('class="rail-item'))
    check("/costs 导航高亮「开销」",
          bool(re.search(r'class="rail-item active"[^>]*href="/costs"', page)))
    check("/costs 不渲染 feed 树", 'id="tree"' not in page)
    check("/costs 区间预设 = 今天/昨天/7/30/本月/上月",
          re.findall(r'<a class="btn[^"]*" href="/costs\?preset=[a-z0-9_]+[^"]*">([^<]+)</a>',
                     page) == ["今天", "昨天", "近 7 天", "近 30 天", "本月", "上月"],
          re.findall(r'<a class="btn[^"]*" href="/costs\?preset=[^"]+">([^<]+)</a>', page))
    check("/costs 默认区间是近 7 天", "（7 天）" in page)
    check("/costs 默认不显示「清除筛选条件」", "range-clear" not in page)
    check("/costs 多日视图有 7 根可点柱子", page.count("bar-col bar-link") == 7,
          page.count("bar-col bar-link"))
    segs = re.findall(r'<div class="bar-seg k-([a-z_]+)" data-kind="([a-z_]+)"', page)
    check("/costs 每个色块都有 data-kind（hover 高亮用）",
          bool(segs) and all(a == b for a, b in segs), len(segs))
    check("/costs 明细行也有 data-kind",
          bool(re.search(r'<div class="tip-row" data-kind="[a-z_]+"', page)))
    check("/costs 不再写「点击看当天」之类的提示（交给手型光标）",
          "点击柱子" not in page and "tip-hint" not in page)
    # ★ 预算卡数 = 池数 + 1（汇总），从源码推导，不写死 4 —— 周报加了第 4 个池
    #   （2026-10-29）后写死的 4 会假红。
    n_pools = len(cr.ALL_POOLS)
    check("/costs 卡片只保留当前区间（+ 各池预算卡）",
          len(re.findall(r'<div class="card[ "]', page)) == n_pools + 1
          and 'class="card is-current"' in page and "当前区间 ·" in page,
          len(re.findall(r'<div class="card[ "]', page)))
    check("/costs 不再有固定的今日/近7/近30 汇总卡",
          not any(f'<div class="card-label">{x}</div>' in page
                  for x in ("今日", "近 7 天", "近 30 天")))
    check("/costs 顶部不再重复显示区间文字", 'class="range-label"' not in page)
    # ★ 预算「池 → AppSetting 键 / 默认值」必须是单一来源。2026-10-29：保存视图
    #   自己手抄了一份 pool→key 映射，加周报池时漏了 → 页面改「周报池日预算」
    #   实际写进 summary_budget_cny，返回 ok 但周报池纹丝不动，用户看到的是
    #   「两个都保存不了」。下面几条把这张表锁死。
    check("★ 每个池都有 AppSetting 键（POOL_BUDGET_KEYS 覆盖 ALL_POOLS）",
          set(cr.POOL_BUDGET_KEYS) == set(cr.ALL_POOLS),
          sorted(set(cr.POOL_BUDGET_KEYS) ^ set(cr.ALL_POOLS)))
    check("★ 每个池都有默认值（POOL_DEFAULTS 覆盖 ALL_POOLS）",
          set(cr.POOL_DEFAULTS) == set(cr.ALL_POOLS),
          sorted(set(cr.POOL_DEFAULTS) ^ set(cr.ALL_POOLS)))
    check("★ 预算键互不重复（否则两个池会互相覆盖）",
          len(set(cr.POOL_BUDGET_KEYS.values())) == len(cr.ALL_POOLS),
          list(cr.POOL_BUDGET_KEYS.values()))
    check("★ 保存视图查 POOL_BUDGET_KEYS，不手抄映射表",
          "cr.POOL_BUDGET_KEYS.get(pool)" in read("apps/costs/views.py"))
    card_labels = re.findall(r'<div class="card-label">([^<]+)池日预算', page)
    check("★ 预算卡顺序 = ALL_POOLS（分级→标题翻译→日报→周报→知识库）",
          card_labels == [cr.POOL_LABELS[p] for p in cr.ALL_POOLS], card_labels)
    # ★ 超限后果文案来自 POOL_NOTES（单一来源），模板不许按池写 if/elif：
    #   2026-10-31 加知识库池时，模板的 else 分支把它说成「日报降级」。
    check("★ 每张预算卡都有超限后果文案，且不是日报的说法",
          all(cr.POOL_NOTES[p] for p in cr.ALL_POOLS)
          and "超限后对话与编译会被拒绝" in page
          and page.count("超限后本次日报降级为规则版报告") == 1,
          page.count("超限后本次日报降级为规则版报告"))
    # ★ 每个 kind 都必须有配色规则：漏一条 ⇒ 柱子退化成容器背景色，
    #   图例有名字、柱子里看不见（周报 2026-10-29 就这样隐身过）。
    no_color = [k for k in cr.KIND_ORDER if f".k-{k} " not in CSS]
    check("★ KIND_ORDER 里每个 kind 在 app.css 都有 .k-<key> 颜色",
          not no_color, no_color)

code, page, err = get("/costs?preset=today")
check("/costs?preset=today 200", code == 200, f"{code} {err}")
if code == 200:
    check("/costs 单日有清除筛选条件", "range-clear" in page)
    check("/costs 单日切换成 24 小时视图", "bars-hour" in page)
    check("/costs 单日 24 根柱子（不可点）",
          page.count('class="bar-col"') == 24 and "bar-link" not in page,
          page.count('class="bar-col"'))
    check("/costs 单日标题按小时", "按小时开销" in page)
    check("/costs 单日横轴抽稀且最右侧有标签（00/06/12/18/23）",
          re.findall(r'<div class="bar-x">(\d\d)</div>', page)
          == ["00", "06", "12", "18", "23"],
          re.findall(r'<div class="bar-x">([^<]*)</div>', page))
    check("/costs 每根柱子都有 hover 明细", page.count('class="bar-tip"') == 24,
          page.count('class="bar-tip"'))
    check("/costs 不再用原生 title（避免和浮层重复）",
          not re.search(r'class="bar-col[^"]*"[^>]*title=', page))
    check("/costs 清除链接在「应用」右侧",
          page.find("清除筛选条件") > page.find(">应用<"),
          (page.find("清除筛选条件"), page.find(">应用<")))

code, page, err = get("/costs?preset=yesterday")
check("/costs?preset=yesterday 200", code == 200, f"{code} {err}")
if code == 200:
    check("/costs 选昨天也有当前区间汇总卡",
          'class="card is-current"' in page and "当前区间 ·" in page)
    check("/costs 选昨天时卡片数量仍然稳定（1 汇总 + 各池预算）",
          len(re.findall(r'<div class="card[ "]', page)) == len(cr.ALL_POOLS) + 1,
          len(re.findall(r'<div class="card[ "]', page)))

code, page, err = get("/costs/feed/55")
check("/costs/feed/55 200", code == 200, f"{code} {err}")
if code == 200:
    check("/costs 下钻页已改成柱状图（不再用折线）", "spark-line" not in page)
    check("/costs 下钻页也有 hover 明细", page.count('class="bar-tip"') >= 1,
          page.count('class="bar-tip"'))
    # ★ 期望从**页面自身**推导，不钉死某一天：下钻窗口是"近 7 天"，
    #   钉 `10/20` 这种字面量等于把"跑脚本那天"当成了机制 —— 日期一滚动就假红
    #   （2026-10-27 就是这样红的，与任何改动无关）。
    #   真机制是：横轴有标签、时间升序、末尾覆盖到最近（今天或昨天）。
    _xs = re.findall(r'<div class="bar-x">([^<]*)</div>', page)
    _today = timezone.localdate()
    # ★ 页面渲染的日期**不带前导零**（`11/1` 而非 `11/01`），而 `%m/%d` 在
    #   每月 1-9 号会生成 `11/01` —— 两者对不上，月初第一天就假红（2026-11-01
    #   正是这样红的，与任何改动无关）。所以这里同时放带零 / 不带零两种写法，
    #   只判「末位是今天或昨天」这个机制，不判零不零这种纯格式。
    _recent = {f"{_today:%m/%d}", f"{_today - timedelta(days=1):%m/%d}",
               f"{_today.month}/{_today.day}", f"{(_today - timedelta(days=1)).month}/{(_today - timedelta(days=1)).day}"}
    check("/costs 下钻页横轴有标签且末位覆盖到最近（今天或昨天）",
          bool(_xs) and _xs[-1] in _recent, _xs[-3:])
    # ★★ 「升序」不能拿字符串比（2026-11-10 假红，与任何改动无关）：标签是
    #   `11/4` … `11/10`，字典序把 `11/10` 排在 `11/4` **前面** ⇒ 任何跨进
    #   两位数的 7 天窗口都会红（每月 10 号起）。跨月同理（`1/31` 之后是 `2/1`）。
    #   所以先解析成 (月, 日) 再比，只判机制、不判格式。
    def _axis_keys(labels):
        out = []
        for s in labels:
            m, _, d = s.partition("/")
            out.append((int(m), int(d)) if m.isdigit() and d.isdigit() else (0, 0))
        return out

    _keys = _axis_keys(_xs)
    check("/costs 下钻页横轴按时间升序（不倒序、不重复打乱）",
          bool(_keys) and _keys == sorted(_keys) and len(_keys) == len(set(_keys)),
          _xs)
    # ★ 守卫自证：解析型判据最容易变成「永远通过」。倒序样本必须判红才算数。
    _bad = _axis_keys(["11/10", "11/9"])
    check("/costs 升序判据自身会响（倒序样本判红）", _bad != sorted(_bad), _bad)
    check("/costs 下钻页默认也是近 7 天（与总览一致）",
          "（7 天）" in page and "range-clear" not in page,
          ("（7 天）" in page, "range-clear" in page))

code, page, err = get("/summary")
check("/summary 200", code == 200, f"{code} {err}")
if code == 200:
    check("/summary 有左侧导航（4 项）", page.count('class="rail-item') >= 4)
    check("/summary 导航高亮「总结」",
          bool(re.search(r'class="rail-item active"[^>]*href="/summary"', page)))
    check("/summary 不渲染 feed 树", 'id="tree"' not in page)
    check("/summary 没有 page-actions（两个入口已移除）", 'class="page-actions"' not in page)
    check("/summary 没有 /summary/catchup 表单", 'action="/summary/catchup"' not in page)
    check("/summary 没有「生成今天」", "生成今天" not in page)
    check("/summary 没有「自动补跑近」", "自动补跑近" not in page)
    check("/summary 重跑/补跑表单是 data-async",
          page.count('data-async="/summary/run"') == page.count("/summary/run"),
          f"data-async={page.count('data-async=\"/summary/run\"')} 全部={page.count('/summary/run')}")
    check("/summary 没有裸 form action=/summary/run", 'action="/summary/run"' not in page)

    # ---- 日报/周报合并成一张表（2026-10-29 需求：周报不能再被压到滚动之外）----
    def _merged(html):
        """取出含「规模」表头的那张表 → (列数, 每行 td 数)。"""
        for m in re.finditer(r'<table class="cgrid">(.*?)</table>', html, re.S):
            seg = m.group(1)
            # ★ `<th\b` 而不是 `<th`：后者会把 `<thead>` 也算成一个表头
            th = len(re.findall(r"<th\b[^>]*>", seg))
            if th != 8 or "规模" not in seg:
                continue
            tds = [len(re.findall(r"<td[^>]*>", tr))
                   for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S)
                   if "<td" in tr and "colspan" not in tr]
            return th, tds, seg
        return 0, [], ""

    th, tds, seg = _merged(page)
    check("/summary 合并表 8 列（列策略 B：条目+异常并成「规模」）", th == 8, th)
    check("★ 每行 td 数与表头一致（colspan 硬编码漏改会在这里现形）",
          bool(tds) and all(n == th for n in tds), tds)
    check("★ 日报与周报在同一张表里（不再各占一个面板）",
          'data-kind="daily"' in seg and 'data-kind="weekly"' in seg,
          ('daily' in seg, 'weekly' in seg))
    check("★ 表里没有第三方推送列（不该再出现推送状态/重推按钮）",
          "推送状态" not in seg and "notion" not in seg.lower())
    check("「最新报告」面板已移除（正文只在详情页看）", "最新报告" not in page)
    # ★ 缺口面板由 `{% if gaps %}` 控制：库里最近 gap_lookback 天恰好没有缺口时，
    #   它**本来就不该出现**。原断言只覆盖「有缺口」这一种状态
    #   （page.find("天的报告") != -1），库里哪天把缺口补齐了它就翻红 ——
    #   测的是数据状态、不是版式。两种状态各自都有契约，分开断言。
    _gap_i = page.find("天的报告")
    _sec_i = page.find("规模")
    if _gap_i != -1:
        check("有缺口 ⇒ 缺口面板在报告表之上", _gap_i < _sec_i and _sec_i != -1,
              (_gap_i, _sec_i))
        check("缺口面板标题说清「缺 N 天」（不是含糊的提示）",
              bool(re.search(r"缺\s*\d+\s*天的报告", page)))
    else:
        check("无缺口 ⇒ 不渲染空白缺口面板（且报告表照常）",
              _gap_i == -1 and _sec_i != -1, (_gap_i, _sec_i))
    check("★ 周报行有独立标记（淡蓝底 + 周报 tag），不只靠背景色",
          'class="row-weekly"' in page and 'class="tag wk"' in page)
    # 2026-10-29：顶部那段「日报 N 天 · 周报 N 期，按截至日倒序；同日周报在上」
    # 是自明的（表里看得见），只占地方 —— 别再长回来。
    check("顶部不再解释排序/计数（日报 N 天 · 周报 N 期）",
          "天 · 周报" not in page and "排在日报之上" not in page)
    # 2026-10-29：表头也不许挂压缩阈值 / 单源阈值 / 调用上限 —— 底部「分层与记账
    # 口径」面板已完整交代，上限在设置页可改、超限会报错，对使用者都是细节。
    _head = re.search(r"份报告(.{0,400}?)<table", page, re.S)
    _seg = _head.group(1) if _head else ""
    check("报告表头只留「最近 N 份报告」（不挂 S3/单源/调用上限）",
          bool(_head) and not any(x in _seg for x in
                                 ("单源 ≥", "单次最多", "S3 &gt;", "S2")),
          _seg.strip()[:80])

# ---- 失败的周报必须在页面上看得见、能手动重跑（这是本轮的原始诉求）----
from core import weekly as _wk  # noqa: E402
from core.models import SummaryRun  # noqa: E402

_probe = None
for _i in range(1, 10):
    _d = timezone.localdate() - timedelta(days=_i)
    if not _wk.weekly_path(_d).is_file():
        _probe = _d
        break
if _probe is None:
    soft("失败周报在页面上可见", False, "找不到没有周报文件的日期，跳过")
else:
    # ★★ 这里动的是**真实库**：要验"失败周报在页面上看得见、能重跑"，必须真有一行
    #    failed 记录，且这一行得落在你要断言的日期上。原来的写法是
    #      try:   delete(该日期) → create(failed 行)
    #      finally: delete(该日期)
    #    —— finally 是**删除**不是还原：那个日期上原本如果有真实行（比如你手动重跑
    #    过、或者文件被清过而记录还在），跑完验证就没了，而且脚本全绿、毫无提示。
    #    改成先快照整行、finally 按快照**原 pk 原字段**重建，跑完与跑前逐字段一致。
    _snap = list(SummaryRun.all_objects.filter(kind=_wk.KIND, date=_probe).values())
    SummaryRun.all_objects.filter(kind=_wk.KIND, date=_probe).delete()
    try:
        SummaryRun.all_objects.create(
            kind=_wk.KIND, date=_probe, state="failed", source_count=6,
            missing_json=["2026-10-31"], error="缺历史日报：2026-10-31")
        _c = Client()
        _r = _c.get("/summary")
        _h = _r.content.decode("utf-8", "replace")
        # ★ `[^>]*>` 不能省：`<tr>` 上除了 class 还有 data-kind（2026-11-01 加，
        #   用来标行类型 —— 推送列移除后 data-kind 没了落点，挪到了行上）。
        _hit = re.search(r'<tr class="[^"]*row-weekly[^"]*row-bad[^"]*"[^>]*>(.*?)</tr>',
                         _h, re.S)
        check("★ 失败的周报在列表里有一行（不是消失）", bool(_hit),
              _probe.isoformat())
        _row = _hit.group(1) if _hit else ""
        check("失败周报行标了「周报」（不会和同日日报混看）",
              'class="tag wk"' in _row and '周报' in _row)
        check("★ 失败周报有「重跑」表单（可手动救）",
              'data-async="/summary/weekly"' in _row
              and 'name="week_end"' in _row)
        check("★ 没有文件 ⇒ 日期不可点（点了只会 404）",
              f'href="/summary/weekly/{_probe.isoformat()}"' not in _row)
        check("失败原因在页面上（不用去翻日志）",
              "缺历史日报" in _h)
    finally:
        SummaryRun.all_objects.filter(kind=_wk.KIND, date=_probe).delete()
        for _r0 in _snap:
            SummaryRun.all_objects.create(**_r0)   # 原 pk + 全字段，逐字段还原
        check("★ 探针跑完真实库原样还原（不是删干净了事）",
              [dict(x) for x in
               SummaryRun.all_objects.filter(kind=_wk.KIND, date=_probe).values()]
              == _snap,
              _probe.isoformat())

code, page, err = get("/")
check("/ 200", code == 200, f"{code} {err}")


def checked_levels(page_html):
    """页面里被勾上的级别号（顺序按 DOM）。★ 不能用松散的 re.findall —— 属性之间有换行缩进。"""
    out = []
    for m in re.finditer(r'<input type="checkbox" data-lv="(\d)"([^>]*)>', page_html, re.S):
        if re.search(r"\bchecked\b", m.group(2)):
            out.append(m.group(1))
    return out


if code == 200:
    check("/ 有左侧导航（4 项）", page.count('class="rail-item') >= 4)
    check("/ 导航高亮「阅读」",
          bool(re.search(r'class="rail-item active"[^>]*href="/"', page)))
    check("/ 无模板标记泄漏（{ { / { % / { # ）",
          not re.search(r"\{[#{%]", page), re.findall(r"\{[#{%]", page)[:3])
    check("/ 工具栏有级别多选下拉",
          "lv-menu" in page and "data-lv-label" in page and "data-lv-reset" in page)
    check("/ 默认标签是「级别：全部」", ">级别：全部<" in page,
          re.findall(r'<span data-lv-label>([^<]*)</span>', page))
    check("/ 默认四个级别全勾", checked_levels(page) == ["0", "1", "2", "3"],
          checked_levels(page))
    check("/ 默认态 URL 不带 levels", 'name="levels" value=""' in page)
    has_tree = 'id="tree"' in page
    soft("/ 渲染 feed 树（需要 Miniflux）", has_tree,
         "__SKIP__" if not has_tree else "")
    if has_tree:
        check("/ 树上有 L 标记", 'class="lmark"' in page)


code, page, err = get("/?sel=feed:37&levels=1,3")
check("/?levels=1,3 200", code == 200, f"{code} {err}")
if code == 200:
    check("只看 L1+L3 时标签正确", ">级别：L1、L3<" in page,
          re.findall(r'<span data-lv-label>([^<]*)</span>', page))
    check("只看 L1+L3 时勾选框 = L1/L3", checked_levels(page) == ["1", "3"],
          checked_levels(page))
    check("hidden levels 回填 1,3", 'name="levels" value="1,3"' in page)
    check("树链接带着 levels 一起走（换分类不丢筛选）", "levels=1%2C3" in page)
    check("旧的「级别阈值」文案已消失",
          "L2+（全部）" not in page and "L0+（全部）" not in page)


code, page, err = get("/?levels=0")
check("/?levels=0 200（★ 旧阈值 UI 根本点不出来的「只看 L0」）", code == 200, f"{code} {err}")
if code == 200:
    check("只看 L0 时只勾 L0", checked_levels(page) == ["0"], checked_levels(page))
    check("只看 L0 时标签正确", ">级别：L0<" in page,
          re.findall(r'<span data-lv-label>([^<]*)</span>', page))


print("\n== D. 级别多选：URL 解析 / 标签 / 查询串（纯函数）==")
rf = RequestFactory()


def filt(qs):
    return rv._filters(rf.get("/?" + qs))


cases = [
    # (查询串,           期望 levels,  期望标签,       期望 hidden 值)
    ("",                 (0, 1, 2, 3), "级别：全部",     ""),
    ("levels=",          (0, 1, 2, 3), "级别：全部",     ""),
    ("levels=0,1,2,3",   (0, 1, 2, 3), "级别：全部",     ""),
    ("levels=0",         (0,),         "级别：L0",      "0"),
    ("levels=1,3",       (1, 3),       "级别：L1、L3",  "1,3"),
    ("levels=3,1",       (1, 3),       "级别：L1、L3",  "1,3"),
    ("levels=1,1,3",     (1, 3),       "级别：L1、L3",  "1,3"),
    ("levels=9,1",       (1,),         "级别：L1",      "1"),
    ("levels=abc",       (0, 1, 2, 3), "级别：全部",     ""),
]
for qs, want, label, value in cases:
    f = filt(qs)
    got = f["levels"]
    check(f"解析 ?{qs or '(空)'} → {want}", got == want, f"得到 {got}")
    check(f"?{qs or '(空)'} 标签 = {label}",
          rv._level_label(got) == label, rv._level_label(got))
    check(f"?{qs or '(空)'} hidden levels = {value!r}",
          rv._levels_value(got) == value, repr(rv._levels_value(got)))
    composed = rv._query_without(f, "sel")
    if value:
        check(f"?{qs or '(空)'} 合成 qs 带 levels", "levels=" in composed, composed)
    else:
        check(f"?{qs or '(空)'} 合成 qs 不带 levels（默认态干净）",
              "levels=" not in composed, composed)

# 旧阈值写法兼容：老书签 / 老链接不能失效
for old, want in [("level=0", (0, 1, 2, 3)), ("level=1", (1, 2, 3)),
                  ("level=2", (2, 3)), ("level=3", (3,))]:
    got = filt(old)["levels"]
    check(f"兼容旧写法 ?{old} → {want}", got == want, got)
check("新的 levels 优先于旧的 level",
      filt("levels=1&level=2")["levels"] == (1,), filt("levels=1&level=2")["levels"])

# 真正的过滤语义：多选是「包含」而不是「阈值 >=」
fake = [{"level": lv} for lv in (0, 1, 2, 3)]
for levels, wanted in [((0,), [0]), ((1, 3), [1, 3]), ((2,), [2]),
                       ((0, 2), [0, 2]), ((0, 1, 2, 3), [0, 1, 2, 3])]:
    kept = [r["level"] for r in fake if r["level"] in levels]
    check(f"集合语义：levels={levels} 只留 {wanted}", kept == wanted, kept)
check("★ 阈值语义做不到「只看 L0」（这正是改多选的原因）",
      sorted({r["level"] for r in fake if r["level"] >= 0}) == [0, 1, 2, 3]
      and sorted({r["level"] for r in fake if r["level"] >= 1}) != [0])


print("\n== E. 数据层：哪些源该被标 L ==")
try:
    from core import promptspec
    cats_with, feeds_with = promptspec.criteria_index()
except Exception as exc:  # noqa: BLE001
    cats_with, feeds_with = set(), set()
    print("    取分级配置失败：%s" % exc)
# 只有 note / 只有 default_level 不算「已配置判据」
from core.models import CategoryTriageConfig, FeedTriageConfig  # noqa: E402


def _has(row):
    return any((getattr(row, f"l{i}_text", "") or "").strip() for i in range(4))


# ★★ 验机制、不验快照：判据集合必须与「L0–L3 任一段非空」的库内真值**恒等**。
#    旧写法把当前状态硬编码成 `{2}` / `{37}` —— 2026-10-16 给 feed 29 写入 L0/L2/L3
#    判据后立刻假红（历史报告会被重跑洗白，快照类断言同样会被数据变动弄脏）。
_truth_cats = {c.category_id for c in CategoryTriageConfig.objects.all() if _has(c)}
_truth_feeds = {f.feed_id for f in FeedTriageConfig.objects.all() if _has(f)}
check("分类级判据集合 == 库内「任一段非空」真值",
      cats_with == _truth_cats, f"{sorted(cats_with)} vs {sorted(_truth_cats)}")
check("本源级判据集合 == 库内真值",
      feeds_with == _truth_feeds, f"{sorted(feeds_with)} vs {sorted(_truth_feeds)}")
check("feed 29 已配判据（2026-10-16 写入 L0/L2/L3）", 29 in feeds_with,
      sorted(feeds_with))

note_only = [f.feed_id for f in FeedTriageConfig.objects.all()
             if not _has(f) and (f.note or "").strip()]
check("只写了「本源备注」的源不算已配置判据",
      all(fid not in feeds_with for fid in note_only), note_only)

try:
    from core.datasource import get_datasource
    annotated = rv._annotate_cfg(get_datasource().list_tree())
    marked = [(f.id, f.cfg_from) for c in annotated for f in c.feeds if f.has_cfg]
    total = sum(len(c.feeds) for c in annotated)
    # 期望值同样由库内真值推出，不是硬编码条数：
    #   标 L 的源 = 有本源判据的源 ∪ 有分类判据的分类下的全部源
    expect_marked = sorted(_truth_feeds
                           | {f.id for c in annotated for f in c.feeds
                              if c.id in _truth_cats})
    check("树上标 L 的源 == 本源判据源 ∪ 分类判据分类下的源",
          sorted(i for i, _ in marked) == expect_marked,
          f"{len(marked)}/{total}：{sorted(i for i, _ in marked)} vs {expect_marked}")
    check("每个标记都带来源说明", all(w for _, w in marked), marked)
    src = {i: w for i, w in marked}
    if 37 in src:
        check("兼职社区(37) 的来源写作「本源」", src[37] == "本源", src[37])
    if 29 in src:
        check("Alex(29) 的来源写作「本源」", src[29] == "本源", src[29])
    if 2 in src:
        check("雪球(2) 的来源是分类继承", src[2].startswith("分类"), src[2])
except Exception as exc:  # noqa: BLE001
    print("    SKIP 取树失败（Miniflux 不可用）：%s" % exc)


print("\n== F. 「打开原文」的三个入口（命中区）==")
DETAIL_TMPL = read("templates/reader/_entry_detail.html")

# F1. 模板契约：三个入口都在、href 都指向 entry.url、都是新标签页
for cls in ("orig-btn", "orig-title", "orig-foot"):
    check(f"详情页有 .{cls}", f'class="{cls}"' in DETAIL_TMPL, cls)
check("三处 href 都是同一个 entry.url（不做改写）",
      DETAIL_TMPL.count('href="{{ entry.url }}"') == 3,
      DETAIL_TMPL.count('href="{{ entry.url }}"'))
check("三处都是 target=_blank + rel=noopener",
      DETAIL_TMPL.count('target="_blank" rel="noopener"') == 3,
      DETAIL_TMPL.count('target="_blank" rel="noopener"'))
check("旧的「meta 行内裸链接」写法已移除",
      "· <a href=\"{{ entry.url }}\"" not in DETAIL_TMPL)
check("入口 ① 与「收起」并排成动作区",
      'class="detail-actions"' in DETAIL_TMPL
      and DETAIL_TMPL.index('class="orig-btn"') < DETAIL_TMPL.index('class="close"'))
check("★ 多行 {# #} 护栏（只吃单行，多行会渲染成正文）",
      not re.search(r"\{#(?:(?!#\}).)*\n(?:(?!#\}).)*#\}", DETAIL_TMPL))

# F2. CSS 契约：扩大命中区靠「padding + 负 margin 抵消」与「负 margin 铺满」
check("有 .detail-actions 容器", ".detail-actions" in CSS)
_title_css = CSS[CSS.index(".orig-title {"):CSS.index(".orig-title {") + 400]
check("标题链接用 padding 外扩命中区", "padding: 3px 5px" in _title_css)
check("标题链接用负 margin 抵消（布局像素不变）", "margin: -3px -5px" in _title_css)
check("标题链接 hover 变色而非下划线（下划线会横贯整行）",
      ".orig-title:hover { color: var(--accent); }" in CSS
      and "text-decoration: none" in _title_css)
_foot_css = CSS[CSS.index(".detail-foot {"):CSS.index(".detail-foot {") + 200]
check("末尾入口负 margin 抵消 .entry-detail 内边距 ⇒ 整行可点",
      "margin: 18px -20px -20px" in _foot_css)
check("三个入口都有 :focus-visible（键盘可达）",
      ".orig-btn:focus-visible" in CSS and ".orig-title:focus-visible" in CSS
      and ".orig-foot:focus-visible" in CSS)

# F3. host 过滤器（纯函数，零依赖）
from reader.templatetags.reader_tags import host as _host  # noqa: E402

for raw, want in [("https://x.com/alex_chen/status/1", "x.com"),
                  ("https://www.xueqiu.com/123456/412541833", "xueqiu.com"),
                  ("http://user:pw@example.com:8080/a", "example.com"),
                  ("https://www.appinn.com/x/", "appinn.com")]:
    check(f"host({raw[:34]}…) = {want}", _host(raw) == want, _host(raw))
for raw in ("", None, "not a url", "   "):
    check(f"host({raw!r}) 返回空串（不渲染出 None）", _host(raw) == "", repr(_host(raw)))

print("\n== G. 标题翻译（中文在前 / 英文在后 / 右键入口）==")
check("列表有双语标题类", 'class="t-zh"' in _ROW and 'class="t-en"' in _ROW)
check("列表行带 data-translated / data-feed-translate",
      "data-translated=" in _ROW and "data-feed-translate=" in _ROW)
check("feed 行带 data-translate", "data-translate=" in TREE_FEED)
check("JS 有 tr-on / tr-off / tr-re 三个动作",
      all(x in JS for x in ('"tr-on"', '"tr-off"', '"tr-re"')))
check("CSS 有标题翻译色块", ".k-translate_title" in CSS)
check("详情页有英文副标题", "detail-orig-title" in DETAIL_TMPL)

print("\n== H. 展开详情区右键交还浏览器（2026-11-19）==")
# ★ 按形状守卫，不能只查 ".entry-detail" 字样 —— 新加的注释里就写着它，
#   只查字样会假绿。做法：切出 contextmenu handler 的函数体，剥块注释，
#   断言「对 .entry-detail 的判断 + return」出现在 preventDefault 之前。
_cm = re.search(r'addEventListener\("contextmenu",\s*function\s*\(evt\)\s*\{(.*?)\n  \}\);',
                JS, re.S)
_cm_body = re.sub(r"/\*.*?\*/", "", _cm.group(1), flags=re.S) if _cm else ""
check("定位到 contextmenu handler 并剥掉注释", bool(_cm_body), "handler 未匹配")
_i_detail = _cm_body.find('closest(".entry-detail")')
_i_prevent = _cm_body.find("preventDefault()")
check("详情区内右键被单独判断（closest .entry-detail）", _i_detail >= 0,
      "详情区判断缺失")
check("详情区判断在 preventDefault 之前（否则浏览器原生菜单仍被屏蔽）",
      _i_detail >= 0 and _i_prevent >= 0 and _i_detail < _i_prevent,
      "detail=%s prevent=%s" % (_i_detail, _i_prevent))
check("详情区命中后直接 return（不弹自定义菜单）",
      bool(re.search(r'closest\("\.entry-detail"\)\)\s*return\s*;', _cm_body)),
      "命中后未 return")
# 列表标题行的右键菜单必须保留：CTX.entry 仍提供 L0-L3 + 立即/重新判定。
check("列表标题行右键仍在（Lx + 立即/重新判定没被一起删掉）",
      all(x in JS for x in ('"设为 L" + lv', '"立即判定"', '"重新判定"')))
check("详情区仍是 .entry-row 的后代（否则本守卫的前提就不成立）",
      'class="entry-detail" id="detail-{{ row.entry.id }}"' in _ROW)

print("\n== I. 文章右键两个收口（2026-11-20：无判据不显立即判定 / 翻译排队局部刷新）==")
# ★ I-1 无判据源不显示「立即判定」：判据标记必须从后端传到行（口径同源），
#   CTX.entry 里 push 必须被 data-cfg 条件包住。按形状守卫（剥注释），不认字样。
check("I-1a 文章行带 data-cfg（后端 _decorate 传 has_cfg）",
      'data-cfg="{% if row.has_cfg %}1{% else %}0{% endif %}"' in _ROW)
_RV = read("apps/reader/views.py")
_dec = _RV[_RV.find("def _decorate"):_RV.find("def _load_one")]
check("I-1b _decorate 的 has_cfg 口径来自 tree 的 f.has_cfg（不新写判据判断）",
      "cfg_feeds" in _dec and "f.has_cfg" in _dec
      and '"has_cfg": e.feed_id in cfg_feeds' in _dec)
check("I-1c _decorate 不自己摸 l{i}_text（判据口径单一来源）",
      "l{i}_text" not in _dec and "promptspec" not in _dec)
# 切 CTX.entry 函数体，剥注释后按形状断言「判据条件包住 push」
_ce_start = JS.find("entry: function (row)")
_ce = JS[_ce_start:JS.find("return items;", _ce_start)]
_ce_body = re.sub(r"/\*.*?\*/", "", _ce, flags=re.S)
_i_cfg = _ce_body.find('row.getAttribute("data-cfg") === "1"')
_i_rate = _ce_body.find('act: "rate-re"')
check("I-1d CTX.entry 按 data-cfg 条件显示立即判定（条件在判定入口之前）",
      _i_cfg >= 0 and _i_rate > _i_cfg >= 0,
      "cfg=%s rate=%s" % (_i_cfg, _i_rate))

# ★ I-2 翻译排队 + 局部刷新：tr-re 不再整页 reload，改入队；行片段走 local=1。
check("I-2a 翻译动作改为入队（tr-re 分支调 trEnqueue，不再直接 POST + reload）",
      bool(re.search(r'act\s*===\s*"tr-re"[^}]{0,200}trEnqueue', JS, re.S)))
_js_nb = re.sub(r"/\*.*?\*/", "", JS, flags=re.S)  # 剥注释：注释里写了 reload 不算
_tr_seg = _js_nb[_js_nb.find("} else if (item.act === \"tr-re\")"):
                 _js_nb.find("} else if (item.act === \"refresh\")")]
check("I-2b tr-re 处理段里没有 location.reload（是真局部刷新，不是注释谎称）",
      "location.reload" not in _tr_seg, "tr-re 仍在整页刷新")
check("I-2c 翻译队列三件套存在且真串行（入队/泵/局部刷新）",
      all(x in _js_nb for x in ("function trEnqueue(", "function trPump(",
                                 "function trRefreshRow("))
      and "trPumping" in _js_nb and "trQueue.shift()" in _js_nb)
check("I-2d 同一 id 不重复入队（连点不叠请求）",
      'trQueue.indexOf(id) >= 0' in _js_nb)
_i_busy = _js_nb.find('err.indexOf("正在跑")')
_busy_branch = _js_nb[_i_busy:_js_nb.find("} else {", _i_busy)] if _i_busy >= 0 else ""
check("I-2e 遇「正在跑」留队首延迟重试（重试分支不 shift、不弹失败）",
      _i_busy >= 0 and "delay = TR_RETRY_MS" in _busy_branch
      and "trQueue.shift()" not in _busy_branch
      and "翻译失败" not in _busy_branch,
      "busy分支=%r" % _busy_branch[:120])
check("I-2f 局部刷新拿服务端渲染的行（local=1 + 只摘 .col-title）",
      '"/hx/row/" + encodeURIComponent(id) + "?local=1"' in _js_nb
      and 'fresh.querySelector(".col-title")' in _js_nb
      and 'tOld.innerHTML = tNew.innerHTML' in _js_nb)
_hxrow = _RV[_RV.find("def hx_row"):_RV.find("def hx_row") + 1400]
check("I-2g hx_row 的 local=1 让 pinned=False（局部刷新不冒出「单独打开」）",
      'request.GET.get("local") == "1"' in _hxrow
      and '"pinned": not local' in _hxrow)

print("\n== J. 文章右键「抓取全文」（2026-11-20 fetch original content）==")
# ★ 按形状守：菜单项、need_confirm 前后端契约、120s 超时、不存正文副本、路由+migration。
#   剥注释，避免注释里写了字样就假绿（项目铁律：查形状不查字样）。
_js_nb2 = re.sub(r"/\*.*?\*/", "", JS, flags=re.S)
_VV = read("apps/reader/views.py")
_MF = read("core/datasource/miniflux.py")
_BASE = read("core/datasource/base.py")
_URLS = read("apps/reader/urls.py")
_MODELS = read("core/models.py")

check("J-1 菜单项「抓取全文」在 CTX.entry（不是详情区）",
      'label: "抓取全文", act: "fetch-full"' in _js_nb2)
_ce2 = _js_nb2[_js_nb2.find("entry: function (row)"):
               _js_nb2.find("return items;", _js_nb2.find("entry: function (row)"))]
check("J-2 「抓取全文」确实 push 进文章菜单函数体",
      'act: "fetch-full"' in _ce2)

check("J-3 前端先试抓再按 need_confirm 弹框、带 confirm=1 重发",
      '"/hx/fetch-full/"' in _js_nb2
      and "r.need_confirm" in _js_nb2
      and "confirm(r.msg)" in _js_nb2
      and 'confirm: "1"' in _js_nb2)
check("J-4 成功后不整页 reload（只 toast + 必要时重发展开详情）",
      "function ffDone(" in _js_nb2
      and "location.reload" not in _js_nb2[_js_nb2.find("function ffDone("):
                                          _js_nb2.find("function ffDone(") + 700])

check("J-5 后端 need_confirm 权威判（已抓过 + 划线警告，confirm=1 才放行）",
      'def hx_fetch_full(' in _VV
      and 'request.POST.get("confirm") == "1"' in _VV
      and "need_confirm" in _VV
      and "rerun_warning" in _VV
      and "重复抓取" in _VV)
check("J-6 失败给上游人话（不静默、不是未知错误）",
      "抓取全文失败" in _VV and "_upstream_error" in _VV)
check("J-7 只记状态与长度，不存正文副本（仓库硬约束）",
      "class EntryFullFetch" in _MODELS
      and "content_len_before" in _MODELS and "content_len_after" in _MODELS
      and "content = models.TextField" not in _MODELS[_MODELS.find("class EntryFullFetch"):
                                                      _MODELS.find("class StarEvent")])

check("J-8 datasource 抽象层声明 fetch_content（只读实现可继续抛）",
      "def fetch_content(" in _BASE and "raise NotImplementedError" in _BASE)
_mf_fn = _MF[_MF.find("def fetch_content("):_MF.find("def fetch_content(") + 900]
check("J-9b fetch_content 函数体：GET + fetch-content 端点 + update_content=true + 120s",
      "fetch-content" in _mf_fn
      and "timeout=120" in _mf_fn
      and "self._get(" in _mf_fn
      and 'update_content="true"' in _mf_fn
      and "self._put(" not in _mf_fn)  # ★ endpoint 是 GET 不是 PUT（官方文档）
check("J-10 URL 路由存在",
      'path("hx/fetch-full/<int:entry_id>"' in _URLS)
check("J-11 migration 0026 建表存在",
      os.path.exists(os.path.join(BASE, "core/migrations/0026_entryfullfetch.py")))

print("\n== K. 文章右键「标记为未读」（2026-11-24，点开即已读的可逆出口）==")
# ★ 按形状守（沿用剥过注释的 JS）：菜单条件、展开后的状态同步、写回真相层、
#   失败人话。任何一处缺了，界面就会"点过却没生效"或"刚读过的行没有反向入口"。
_ce3 = _js_nb2[_js_nb2.find("entry: function (row)"):
               _js_nb2.find("return items;", _js_nb2.find("entry: function (row)"))]
check("K-1 行模板带 data-unread 真值（菜单条件同源，不猜状态）",
      'data-unread="{% if row.entry.unread %}1{% else %}0{% endif %}"' in _ROW)
_i_unread = _ce3.find('row.getAttribute("data-unread") === "0"')
_i_push = _ce3.find('act: "mark-unread"')
check("K-2 文章菜单只对已读行 push「标记为未读」（条件在 push 之前）",
      _i_unread >= 0 and _i_push > _i_unread >= 0,
      "cond=%s push=%s" % (_i_unread, _i_push))
check("K-3 展开后同步把行翻成已读（否则刚读过的行右键还是没有反向入口）",
      'row.classList.remove("is-unread")' in _js_nb2
      and 'row.setAttribute("data-unread", "0")' in _js_nb2)
_mu_start = _js_nb2.find('item.act === "mark-unread"')
_mu_seg = _js_nb2[_mu_start:_js_nb2.find('item.act === "level"', _mu_start)] \
    if _mu_start >= 0 else ""
check("K-4 handler 真打端点 + 失败给人话 + 成功才刷新",
      _mu_start >= 0 and '"/hx/mark-unread/"' in _mu_seg
      and "r.ok" in _mu_seg and "标记为未读失败" in _mu_seg
      and "location.reload" in _mu_seg)
check("K-5 后端写真相层、错误不静默，且只收 POST",
      "def hx_mark_unread(" in _VV
      and "@require_POST\ndef hx_mark_unread" in _VV
      and "ds.mark_unread([entry_id])" in _VV
      and 'httpkit.ok("已标记为未读"' in _VV
      and 'httpkit.err("标记为未读失败' in _VV)
check("K-6 路由存在（带 entry_id）",
      'path("hx/mark-unread/<int:entry_id>"' in _URLS)
check("K-7 datasource 抽象层声明 mark_unread（不许只改本地显示状态）",
      "def mark_unread(" in _BASE and "raise NotImplementedError" in _BASE)
_mu_i = _MF.find("def mark_unread(")
_mf_mu = _MF[_mu_i:_MF.find("\n    def ", _mu_i + 5)]  # 按 def 切片：别把下一个函数的 _put 算进来
check("K-8 mark_unread 复用唯一写口（status=unread，不另造端点）",
      'self._set_entries_status(entry_ids, "unread")' in _mf_mu
      and "self._put(" not in _mf_mu)
_w_i = _MF.find("def _set_entries_status(")
_mf_writer = _MF[_w_i:_MF.find("\n    def ", _w_i + 5)]
check("K-9 唯一写口打 /v1/entries、带 status、写完失效缓存（read/unread 同口径）",
      '"/v1/entries"' in _mf_writer and '"status": status' in _mf_writer
      and "self._invalidate()" in _mf_writer)

print("\n" + "=" * 68)
print("结果：%d 通过 / %d 失败 / %d 跳过" % (ok, bad, skip))
if fails:
    print("失败项：", fails)
print("=" * 68)
sys.exit(1 if bad else 0)
