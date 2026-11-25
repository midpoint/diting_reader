"""前端静态守卫：专收「manage.py test 全绿、浏览器里却炸」的那一类。

★ 收第一条的经过（2026-10-31）：
    划线 hover 浮层的变量从 `del` 改名成 `marksHoverBtns` 时漏了两处，留下
    `marksHoverDel` —— 这个名字**从未声明**。非严格模式下**读**未声明变量直接抛
    ReferenceError，于是：
      · `mouseover` 分发器每次移到非 mark 元素都抛（控制台刷屏、后续逻辑中断）；
      · `mousedown` 分发器的 `if (!marksHoverTip && !marksHoverDel) return;` 一抛，
        「点到别处就收」整段失效 —— 无备注的划线（没有文字卡片，marksHoverTip 为
        null ⇒ 短路失效 ⇒ 求值 marksHoverDel）浮层会一直挂在页面上。
    没有任何一条 python 断言会跑 JS，所以全绿。是真浏览器脚本
    `scripts/e2e_chat_keys.js` 抓到 `ReferenceError: marksHoverDel is not defined`。

守两件静态可判的事：
  1) `marksHover*` 变量族凡被引用，必须有 var/let/const/function 声明；
  2) 对话输入框的键盘契约（Enter 发送、Shift+Enter 换行）不许被无声改掉。

为什么不做通用「全文件未声明标识符」检查：没有 ESLint，纯正则判断「局部变量/参数/
解构/对象键」误报率高。误报会逼人往豁免清单里塞东西，守卫就废了 —— 只在已知
出过事的变量族上守，宁窄不假。
"""
import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"
TPL = ROOT / "templates"


def _declared(src: str) -> set:
    """收集 var/let/const 声明的名字 + 具名函数。

    ★ 逗号切分的取舍：`var a = null, b = null;` 能正确取出 a、b；
      而 `var x = f(p, q);` 会把 p、q 误当声明（漏报）。这里只在 marksHover 族上
      用（该族声明全是简单字面量），漏报风险可接受、误报风险为 0。
    """
    names = set()
    for m in re.finditer(r"\b(?:var|let|const)\s+([^;]*);", src, re.S):
        for part in m.group(1).split(","):
            n = part.split("=")[0].strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", n):
                names.add(n)
    names |= set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", src))
    return names


def _undefined_of(src: str, prefix: str) -> list:
    used = set(re.findall(r"\b%s[A-Za-z0-9_]*\b" % re.escape(prefix), src))
    return sorted(used - _declared(src))


class AppJsIdentifierTest(SimpleTestCase):
    def _src(self):
        return (STATIC / "app.js").read_text("utf-8")

    def test_marks_hover_vars_all_declared(self):
        missing = _undefined_of(self._src(), "marksHover")
        self.assertEqual(missing, [],
                         "这些 marksHover* 被引用但没有声明 —— 读未声明变量会抛 "
                         "ReferenceError，整段分发器会静默失效")

    def test_guard_catches_a_planted_case(self):
        # 守卫自己要证明会响（否则它可能一直是假绿）
        self.assertEqual(_undefined_of("var marksHoverOk = 1; marksHoverOk; marksHoverGhost;",
                                       "marksHover"),
                         ["marksHoverGhost"])
        # 反向：声明了就不要再报
        self.assertEqual(_undefined_of("var marksHoverA = null, marksHoverB = null;"
                                       "marksHoverA; marksHoverB;", "marksHover"), [])


class DeepLinkOpenContractTest(SimpleTestCase):
    """`?open=<id>` 落在列表窗口外时，**不许静默什么都不做**（2026-11-03）。

    事故：列表只有最近 ENTRY_WINDOW(200) 条，`openFromUrl()` 只在
    `#row-<id>` 已存在时才展开。从 L3 通知点进一条旧文时它 `return` 一句了事 ——
    正文没开、不报错、也不说原因，看起来就是"通知坏了"，而日志里一切正常。
    现在它必须去 `/hx/row/<id>` 把那一行取回来。服务端那一半由
    `test_deeplink_row.py` 守，这里守前端不许退回老写法。
    """

    @staticmethod
    def _fn_src(src: str, name: str) -> str:
        """取 `function name(...) { ... }` 的**平衡括号体**（不靠缩进/正则贪婪）。"""
        start = src.index("function %s(" % name)
        i = src.index("{", start)
        depth, j = 0, i
        while j < len(src):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    return src[i:j + 1]
            j += 1
        raise AssertionError("函数 %s 的括号没闭合" % name)

    def _open_body(self) -> str:
        return self._fn_src((STATIC / "app.js").read_text("utf-8"), "openFromUrl")

    def test_missing_row_falls_back_to_server(self):
        body = self._open_body()
        self.assertIn("/hx/row/", body,
                      "找不到行时必须去服务端补一行，不能原地放弃")

    def test_no_silent_give_up(self):
        """老写法的形状：`if (!row) return;` —— 守卫要能指出它。"""
        body = self._open_body()
        self.assertIsNone(re.search(r"if\s*\(\s*!row\s*\)\s*return", body),
                          "openFromUrl 不许在找不到行时静默 return")

    def test_guard_catches_the_old_shape(self):
        # ★ 守卫自己会响：把老写法喂进来必须判红（否则这条测试只是装饰）
        old = ("function openFromUrl() { var row = null; if (!row) return; "
               "pendingOpenId = 1; }")
        self.assertIsNotNone(re.search(r"if\s*\(\s*!row\s*\)\s*return",
                                       self._fn_src(old, "openFromUrl")))
        self.assertNotIn("/hx/row/", self._fn_src(old, "openFromUrl"))


class ChatKeyContractTest(SimpleTestCase):
    """Enter 发送 / Shift+Enter 换行（用户 2026-10-31 指定）。

    为什么钉静态契约：可见行为由 scripts/e2e_chat_keys.js 实测（打桩、不花钱），
    但那个脚本不会在每次 `manage.py test` 里跑。这里守住三处关键判断不许被
    顺手改掉，实测脚本负责证明它们真的生效。
    """

    def test_enter_sends_and_shift_enter_newlines(self):
        src = (STATIC / "app.js").read_text("utf-8")
        self.assertIn('ta.matches(".wc-form textarea")', src)
        self.assertIn("requestSubmit", src)      # 和点「发送」是同一条路径，htmx 才拦得住
        self.assertIn("isComposing", src)        # 输入法组字时不能发
        # ★ 必须从对话输入框那段往后找 shiftKey：文件里另有一处（批注编辑浮层的
        #   ⌘/Ctrl+Enter 提交）也写着 e.shiftKey，裸 index() 会命中错对象、断言假绿。
        start = src.index('ta.matches(".wc-form textarea")')
        i = src.index("e.shiftKey", start)
        self.assertIn("return", src[i:i + 140],
                      "Shift 必须提前 return，把换行交回浏览器默认行为")

    def test_placeholder_matches_behaviour(self):
        src = (TPL / "wiki" / "_chat.html").read_text("utf-8")
        self.assertIn("Enter 发送", src)
        self.assertIn("Shift+Enter 换行", src)
        self.assertNotIn("点发送才提交", src)


#: 覆盖前二次确认的前端契约（后端在 `apps/summary/views.py::_rerun_guard`）。
def handles_need_confirm(src: str) -> bool:
    """异步表单是否接住 `need_confirm`：弹确认 + 带 `confirm=1` 重发。

    ★ 为什么值得一条静态守卫：后端返回 need_confirm 时若前端只当普通失败，
      用户看到的是「没跑起来：…」而不是确认框 —— 功能等于没有，
      而 Python 测试全绿（后端行为是对的）。
    """
    return "need_confirm" in src and 'body.confirm = "1"' in src


class NeedConfirmWiringTest(SimpleTestCase):
    def test_appjs_wires_need_confirm(self):
        src = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertTrue(handles_need_confirm(src),
                        "app.js 没接 need_confirm：重跑被拦下时前端只会报错，不会二次确认")

    def test_guard_catches_the_old_shape(self):
        """守卫自己会响：删掉带 confirm 重发那行，判据必须变 False。"""
        src = (STATIC / "app.js").read_text(encoding="utf-8")
        broken = src.replace('body.confirm = "1";', "")
        self.assertNotEqual(broken, src, "反证没生效：源码里找不到那行")
        self.assertFalse(handles_need_confirm(broken))
