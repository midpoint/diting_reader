"""模板层守卫：专收「测试全绿、页面却是坏的」那一类错误。

★ 没收第一条的经过（2026-10-31，同一天踩了两次）：
    Django 的 `{# #}` **只吃单行**。写成多行时它不会被当注释，而是整段
    渲染成正文 —— 第一次出现在 /costs 的预算卡（每张卡都印着注释段落），
    第二次出现在文章底部的对话区（页面上多出一行 `{# 对话区…`）。
    两次 `manage.py test` 都是绿的：断言「模板里有 p.note」「有对话 div」
    当然会过 —— 它们根本不检查「有没有多余的文字跑出来」。

    所以这条检查必须扫**所有**模板，不是我上次动过的那一个。只修一处等于
    把同一颗雷留在别处（这次的第二颗就是我自己刚埋的）。
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

TPL = Path(__file__).resolve().parents[2] / "templates"


def _multiline_hash_comments(path: Path) -> list:
    """返回 [(行号, 行内容)]：`{#` 开了却没在同一行 `#}` 收（漏进正文）。

    `{% comment %}…{% endcomment %}` 块内不检查：那块内容本来就整体丢弃，
    里面写 `{#` 也不会渲染出来（误报会逼人往豁免清单里塞东西，守卫就废了）。
    """
    out, in_comment = [], False
    for i, line in enumerate(path.read_text("utf-8").splitlines(), 1):
        if "{% comment %}" in line:
            in_comment = True
        if "{% endcomment %}" in line:
            in_comment = False
            continue
        if in_comment:
            continue
        if "{#" in line and "#}" not in line.split("{#", 1)[1]:
            out.append((i, line.strip()))
    return out


class TemplateHygieneTest(SimpleTestCase):
    def test_no_multiline_hash_comment(self):
        bad = []
        for f in sorted(TPL.rglob("*.html")):
            for line_no, _ in _multiline_hash_comments(f):
                bad.append("%s:%d" % (f.relative_to(TPL), line_no))
        self.assertEqual(bad, [], "多行 {# #} 会当正文渲染 —— 改用 {% comment %}")

    def test_guard_catches_a_planted_case(self):
        # 守卫自己要证明会响：拿一个已知有问题的字符串跑同一套判据
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False,
                                         encoding="utf-8") as fh:
            fh.write("{# 第一行\n   第二行 #}\n")
            name = fh.name
        try:
            self.assertEqual(len(_multiline_hash_comments(Path(name))), 1)
        finally:
            Path(name).unlink()


class AiEntryTest(SimpleTestCase):
    """「问AI」入口的位置与文字（用户 2026-10-31 明确指定的三件事）。

    为什么值得守：入口原本是个悬浮 icon，靠 app.js 每帧算位置，**改坏了也
    只是"位置别扭"，断言和报错都不会有**。用户提的三条（文字统一、别用浮动
    图标、feed 文章放「原文」左边）只有在模板里才看得见，所以钉在模板层。
    """

    def _tpl(self, name):
        return (TPL / name).read_text("utf-8")

    def test_trigger_text_is_wen_ai(self):
        src = self._tpl("wiki/_ai_trigger.html")
        # 可见文字必须是「问AI」这一种说法（图标是 aria-hidden 的 ✦，不算文字）
        self.assertIn("问AI</button>", src.replace("\n", ""))
        for old in ("和 AI 聊这篇", ">AI<", ">总结<"):
            self.assertNotIn(old, src)

    def test_no_floating_fab_anymore(self):
        # 浮动 icon 已按要求去掉：任何模板里都不该再有 .ai-fab
        hits = [f.relative_to(TPL) for f in TPL.rglob("*.html")
                if "ai-fab" in f.read_text("utf-8")]
        self.assertEqual(hits, [], "入口改成页头内联按钮，不再有浮动 icon")

    def test_three_detail_pages_include_trigger(self):
        for name in ("reader/_entry_detail.html", "summary/detail.html",
                     "summary/weekly_detail.html"):
            self.assertIn('wiki/_ai_trigger.html', self._tpl(name), name)

    def test_entry_trigger_is_left_of_orig_link(self):
        # feed 文章：入口在「原文 ↗」**左边**（用户指定）
        src = self._tpl("reader/_entry_detail.html")
        self.assertLess(src.index('wiki/_ai_trigger.html'),
                        src.index('class="orig-btn"'))

    def test_panel_is_draggable_and_resizable(self):
        src = self._tpl("wiki/_article_tools.html")
        self.assertIn("data-ai-drag", src)        # 顶栏拖动
        self.assertIn("data-ai-resize", src)      # 右下角缩放
        # 入口与浮窗已拆开，靠 id 关联；按钮不在这个模板里
        self.assertIn('id="ai-panel-{{ source_type }}-{{ source_id }}"', src)
        self.assertNotIn("data-ai-toggle", src)

    def test_panel_body_has_no_htmx_attributes(self):
        """★★ 浮窗内容区**绝不能**带 hx-get / hx-trigger 这类 htmx 属性。

        为什么（2026-11-01 实测）：只写 `hx-get` 不写 `hx-trigger` 时，htmx 的
        默认触发器是 **click** —— 于是在浮窗里**选一段文字**（浏览器松手后仍会派发
        click）就会重新 GET /wiki/chat 并整块替换 innerHTML：选区当场清空、内容
        重排往下滚一行，划线再也点不到。实测抓到 `ai-body-entry-*` 被 swap。
        懒加载改为 aiOpen() 显式 htmx.ajax()，URL 放 `data-chat-url`（htmx 不认）。

        ★ 这条是「无声回归」的守卫：删/改 hx-trigger 时页面上完全看不出问题，
          只有真去选字才复现 —— 所以必须在模板层钉死属性本身。
        """
        src = self._tpl("wiki/_article_tools.html")
        # 截出 .ai-panel-body 那一段（从 class 到闭合 >），只查它自己
        i = src.index('class="ai-panel-body"')
        seg = src[i:i + 600]
        end = seg.index(">")
        seg = seg[:end]
        for bad in ("hx-get", "hx-trigger", "hx-swap", "hx-post"):
            self.assertNotIn(bad, seg, f"浮窗内容区不该带 {bad}（htmx 会绑默认 click 触发）")
        self.assertIn("data-chat-url", seg, "懒加载 URL 必须放在 data-chat-url 给 JS 读")



# 花钱 + 慢的端点：点一次 = 一整轮模型调用，而且十几秒到几分钟才回来。
# ★ 只圈这些，不是「所有 hx-post」：采纳/丢弃/退稿是本地移文件、立刻返回，
#   强制它们也禁用只会让快速连批变得别扭。守卫要盯的是「重复点会重复花钱」。
# ★ 2026-11-23 补 repolish：它此前正因不在名单里，按钮漏了禁用也一路全绿。
_PAID_POSTS = ('hx-post="/wiki/compile"', 'hx-post="/wiki/ingest"',
               'hx-post="/wiki/approval/repolish"')


def _paid_forms_without_disabled(text: str) -> list:
    """返回缺 `hx-disabled-elt` 的花钱按钮表单（整张 `<form …>` 开启标签）。"""
    bad = []
    for tag in re.findall(r"<form\b[^>]*>", text, re.S):
        if any(p in tag for p in _PAID_POSTS) and "hx-disabled-elt" not in tag:
            bad.append(tag.replace("\n", " ")[:120])
    return bad


def _paid_forms(text: str) -> list:
    """所有指向花钱端点的 `<form …>` 开启标签（守卫覆盖率自检用）。"""
    return [tag for tag in re.findall(r"<form\b[^>]*>", text, re.S)
            if any(p in tag for p in _PAID_POSTS)]


class PaidFormGuardTest(SimpleTestCase):
    """花钱按钮必须自带「请求期间禁用」。

    ★ 为什么要有这条（2026-11-02，用户的原话：「很容易点多次」）：
      编译一次要跑 1–3 分钟，按钮不禁用的话，人会以为没点上、再点一次 ——
      每点一次都是一整轮模型调用（真花钱），还会出两套同名草稿。
      而这类毛病**在任何断言里都不出现**：页面能开、流程能走完，只是钱多花了。
    ★ 为什么扫全部模板：只给「编译」加属性，下一个花钱按钮照样会漏 ——
      这不是那一个按钮的手误，是「加按钮时想不到」这个类别。
    """

    def test_every_paid_form_disables_its_button(self):
        bad = []
        for f in sorted(TPL.rglob("*.html")):
            for tag in _paid_forms_without_disabled(f.read_text("utf-8")):
                bad.append("%s: %s" % (f.relative_to(TPL), tag))
        self.assertEqual(bad, [], "花钱按钮缺 hx-disabled-elt：连点会重复扣钱")

    def test_guard_actually_sees_the_paid_forms(self):
        """扫全集必须**真扫到了**东西。

        ★ 反证的意义：「一个违规都没找到」有两种可能 —— 大家都合规，或者
          正则压根没匹配上（改了个属性名/引号就静默为空）。后者是最贵的假绿：
          守卫从此永远通过。
        ★ 第十一步：逐来源提炼表单随首页重排下线，「编译」不再是唯一一枚。
          所以下限保持 1，但补具体断言「编译表单确实被认出来」——
          否则守卫只扫到别的东西也会绿。`/wiki/ingest` 端点仍在代码层（一条龙
          内部与脚本备用），守卫继续把它圈在监控范围里。
        ★ 2026-11-23 补「重编结论」：它在模板里真实存在，守卫必须认得出它，
          否则「全绿」只说明扫到了编译，新增花钱按钮仍会静默漏禁。
        """
        seen = []
        for f in sorted(TPL.rglob("*.html")):
            seen += [(f, t) for t in _paid_forms(f.read_text("utf-8"))]
        self.assertGreaterEqual(len(seen), 1,
                                "只扫到 %d 个花钱表单 —— 判据/写法变了，守卫已经空转" % len(seen))
        self.assertTrue(any('/wiki/compile' in t for _, t in seen),
                        "编译表单没被认出来 —— 守卫可能已经空转")
        self.assertTrue(any('/wiki/approval/repolish' in t for _, t in seen),
                        "重编结论表单没被认出来 —— 守卫可能已经空转")

    def test_guard_itself_rings(self):
        # 守卫要自证会响：拿两个已知的字符串跑同一套判据
        planted = ('<form class="inline-form" hx-post="/wiki/compile"\n'
                   '      hx-target="#wiki-body">')
        self.assertEqual(len(_paid_forms_without_disabled(planted)), 1)
        fixed = ('<form hx-post="/wiki/compile" hx-disabled-elt="find button">')
        self.assertEqual(_paid_forms_without_disabled(fixed), [])
        # 新增的 repolish 端点：同样要一红一绿，别只靠 compile 证明判据还活着
        planted_repolish = ('<form hx-post="/wiki/approval/repolish"\n'
                            '      hx-target="#approval-workarea">')
        self.assertEqual(len(_paid_forms_without_disabled(planted_repolish)), 1)
        fixed_repolish = ('<form hx-post="/wiki/approval/repolish"\n'
                          '      hx-disabled-elt="find button">')
        self.assertEqual(_paid_forms_without_disabled(fixed_repolish), [])
        # 免费动作不该被这条守卫误伤（否则人会往豁免清单里塞东西，守卫就废了）
        free = '<form hx-post="/wiki/candidate" hx-target="#wiki-body">'
        self.assertEqual(_paid_forms_without_disabled(free), [])
