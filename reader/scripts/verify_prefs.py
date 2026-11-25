"""零成本验证「设置面板 = 模态弹窗 + 左列分组 / 右列设置项」。

用法：python scripts/verify_prefs.py

背景（2026-10-26 需求）：设置从整页改成模态弹窗，任何页面都能就地打开；左列切分组、
右列是该组设置项。核心风险有三个，本脚本逐个钉住：

  A. **片段里的 <script> 不会执行**：openModal 是把 HTML 塞进 innerHTML，浏览器
     不跑内联脚本。所以交互必须是 onclick/onchange + 全局函数（app.js 里定义）——
     模板里一旦有人改回内联 <script>，保存/切换会静默失效（不报错、点了没反应）。
  B. **保存后不能 reload**：重载会把弹窗一起干掉。所以 save/reset 必须回带生效值，
     前端就地回填；少了 value 字段，回退后输入框会显示旧值。
  C. **csrftoken cookie**：页面没有 <form>，Django 只在渲染 csrf_token 标签时种
     cookie。模板里删掉那个隐藏 form，保存直接 403。

覆盖：
  1. 模板源码契约（面板结构 / 两栏 / 事件绑定 / csrf）
  2. 假数据渲染：每个分组都有 tab 和 section，且**只有一个分组默认展开**
  3. 视图外壳：HX-Request=true → 模态片段；直接访问 → 整页兜底
"""
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "apps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402
django.setup()

from django.template.loader import render_to_string  # noqa: E402
from django.test import RequestFactory  # noqa: E402

from core import settingkit as sk  # noqa: E402
from prefs import views  # noqa: E402

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
PAGE = read("templates/prefs/index.html")
JS = read("static/app.js")
CSS = read("static/app.css")
BASE_HTML = read("templates/base.html")

print("\n== A. 面板源码契约 ==")
check("面板带遮罩 .modal-backdrop（点击关闭）",
      'class="modal-backdrop" onclick="closeModal(this)"' in PANEL)
check("遮罩只在模态外壳下渲染（standalone 时不要）",
      "{% if not standalone %}" in PANEL)
check("外壳是 .modal.modal-prefs", "modal modal-prefs" in PANEL)
check("两栏容器是 .prefs-shell（左 .prefs-side / 右 .prefs-main）",
      'class="prefs-shell"' in PANEL
      and 'class="prefs-side"' in PANEL
      and 'class="prefs-main"' in PANEL)
check("左列每个分组一个 .prefs-tab[data-g]，且绑定 prefsTab",
      PANEL.count('class="prefs-tab') >= 1
      and 'onclick="prefsTab(this)"' in PANEL
      and 'data-g="{{ item.g.key }}"' in PANEL)
check("右列每个分组一个 .pref-group[data-g]（与左列同源循环）",
      'class="pref-group" data-g="{{ item.g.key }}"' in PANEL)
check("保存用 onchange=prefsSave(this)（不是内联脚本绑定）",
      'onchange="prefsSave(this)"' in PANEL)
check("回退用 onclick=prefsReset(this)", 'onclick="prefsReset(this)"' in PANEL)
check("★ 有 {% csrf_token %}（没有它 csrftoken cookie 不下发，保存 403）",
      "{% csrf_token %}" in PANEL)
check("输入框带 data-key（JS 要取键名）", 'class="pref-field" data-key=' in PANEL)

print("\n== B. 交互函数在 app.js 里全局定义 ==")
for fn in ("prefsTab", "prefsSave", "prefsReset"):
    check("app.js 定义 window.%s" % fn, "window.%s = function" % fn in JS)
check("保存/回退走 postForm（带 X-CSRFToken）",
      'postForm("/prefs/save"' in JS and 'postForm("/prefs/reset"' in JS)
check("★ 保存后不 reload（就地回填 res.value）",
      "prefsFill(input, res.value)" in JS and "location.reload" not in JS.split("prefsSave")[1][:600])

print("\n== C. 样式 ==")
check("CSS 有 .prefs-shell 两栏网格", ".prefs-shell" in CSS and "grid-template-columns" in CSS)
check("CSS 有 .prefs-tab.active（当前分组可见）", ".prefs-tab.active" in CSS)
check("★ 模态外框定高（切分组时高度不跳）",
      re.search(r"\.modal-prefs \{[^}]*height: min\(", CSS, re.S) is not None)
check("★ 整页兜底把定高改回 auto（页面本身要能自由撑开）",
      re.search(r"\.modal-standalone \{[^}]*height: auto", CSS, re.S) is not None)
check("★ 切分组时右列滚回顶部（不留上一组的滚动位置）",
      "main.scrollTop = 0" in JS)
check("CSS 有 .modal-standalone（整页兜底去掉浮层）", ".modal-standalone" in CSS)
check("rail 设置入口改成按钮 + openModal", "openModal('/prefs')" in BASE_HTML)

print("\n== D. 假数据渲染：分组一一对应 ==")


class _F:
    def __init__(self, key, label, ftype="int", effect="instant",
                 minimum=None, maximum=None, help=""):
        self.key, self.label, self.type = key, label, ftype
        self.effect, self.minimum, self.maximum, self.help = (
            effect, minimum, maximum, help)


class _G:
    def __init__(self, key, label, fields):
        self.key, self.label, self.fields = key, label, fields


groups = [
    {"g": _G("triage", "分级", [
        _F("triage_enabled", "启用分级", "bool"),
        _F("triage_interval_min", "轮询间隔", "int", "restart", 1, 120)]),
     "fields": []},
    {"g": _G("notify", "系统通知", [_F("notify_enable", "启用通知", "bool")]),
     "fields": []},
]
for item in groups:
    item["fields"] = [{"f": f, "value": False if f.type == "bool" else 5,
                       "overridden": False} for f in item["g"].fields]

html = render_to_string("prefs/_panel.html", {
    "groups": groups, "active": "triage",
    "notify": {"bin": "terminal-notifier", "installed": False,
               "hint": "brew install terminal-notifier"},
})
tabs = re.findall(r'class="prefs-tab[^"]*"[^>]*data-g="([^"]+)"', html)
secs = re.findall(r'<section class="pref-group" data-g="([^"]+)"([^>]*)>', html)
# ★ 期望值 = 注册表分组按顺序 + 末尾固定的 globals（全局层文件不是键值组，
#   在模板里单独一段，见 v2 docstring）。写成"从注册表推导"而不是硬编码 2，
#   否则以后加一个分组就要来改这里一次。
want = [g["g"].key for g in groups] + ["globals"]
check("左列 tab = 注册表分组 + globals（顺序一致）", tabs == want, "tabs=%r" % tabs)
check("右列 section = 注册表分组 + globals",
      [s[0] for s in secs] == want, "secs=%r" % [s[0] for s in secs])
check("tab 与 section 的分组键集合相同",
      set(tabs) == {s[0] for s in secs}, "%r vs %r" % (tabs, secs))
hidden = [k for k, attrs in secs if "hidden" in attrs]
check("★ 只有 active 那个分组展开（其余全部 hidden）",
      set(tabs) - set(hidden) == {"triage"}, "hidden=%r" % hidden)
check("★ 默认展开的是 active 分组", "triage" not in hidden, "hidden=%r" % hidden)
check("未安装通知程序时给提示（含安装命令）",
      "brew install terminal-notifier" in html)
check("bool 项渲染成 checkbox", html.count('type="checkbox"') == 2)
check("restart 项标「重启」、instant 项标「即时」",
      ">重启<" in html and ">即时<" in html)
# ★ 断言放在渲染结果而不是源码上：源码的说明性注释里也会出现 "<script>" 这个字眼
check("★ 渲染结果里没有内联 <script>（innerHTML 注入不会执行）",
      "<script" not in html.lower())
# csrf 隐藏域只在带 request 的真实请求里才渲染出 token，这里只钉模板标签在不在
check("模板里有隐藏 form 包住 {% csrf_token %}",
      "<form hidden>{% csrf_token %}</form>" in PANEL)

print("\n== E. 视图：模态片段 vs 整页兜底 ==")
# ★ 用 RequestFactory 而不是 test Client：本项目的 ALLOWED_HOSTS 不含
#   "testserver"，Client 会直接 400（不是本页的问题，脚本别被它带偏）。
rf = RequestFactory()
frag_resp = views.index(rf.get("/prefs", headers={"HX-Request": "true"}))
frag = frag_resp.content.decode("utf-8")
page = views.index(rf.get("/prefs")).content.decode("utf-8")
check("HX-Request=true → 200", frag_resp.status_code == 200, str(frag_resp.status_code))
check("★ 模态片段不是整份 HTML（没有 <!DOCTYPE>）", "<!DOCTYPE" not in frag[:200])
check("模态片段含 .modal-prefs 与遮罩",
      "modal-prefs" in frag and "modal-backdrop" in frag)
check("直接访问 → 整页（有 base 外壳）", "<!DOCTYPE" in page[:200])
check("整页兜底同样有 .prefs-shell 两栏", "prefs-shell" in page)
check("整页兜底不带遮罩（standalone）", "modal-standalone" in page)
check("注册表里的分组全部出现在左列", all(g.key in page for g in sk.GROUPS))
check("默认展开第一个分组", page.count('data-g="%s"' % sk.GROUPS[0].key) >= 2)

print("\n%d PASS / %d FAIL" % (ok, bad))
if bad:
    print("失败项：")
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("ALL PASS")
