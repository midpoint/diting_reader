"""wiki 首页不展示目录的绝对路径（2026-11-16 用户报隐私泄露）。

原话：「wiki 首页不要显示目录的全路径，路径里的用户名等会泄露隐私」。

问题在 `templates/wiki/index.html` 的一行：`目录：<code>{{ stats.root }}</code>`。
`stats["root"]` 是 `str(root())` —— 真实库下就是
`/Users/<用户名>/Documents/git/fred2045/diting/data/wiki`。页面在本机看着无害，
但它会被截图、被投屏、被贴进聊天，而路径里的用户名与目录结构和「知识库讲了什么」
毫无关系。**这类泄露的特点是没有功能症状**，所以只能靠判据守。

修法两条：
  A. `wstore.root_label()` —— 只回相对项目根的那一段（`data/wiki`）；WIKI_DIR 指到
     项目外（Obsidian vault 这类更贴身的目录）时只回「自定义目录」，连值都不给。
  B. 模板只许用 `root_label`；`stats["root"]` 留给脚本/日志（`verify_wiki.py` 有
     静态守卫钉模板里不出现 `stats.root`）。

判据：
  1. 项目内目录 → 相对段，不含家目录/用户名；
  2. 项目外目录 → 只说「自定义目录」，路径一个字都不出现；
  3. `stats()` 两个键都在，且 `root` 仍是绝对路径（脚本要用，别把内部接口也改坏）；
  4. **真渲染**首页（临时库，路径里故意带 `/Users/zhangsan`）不含用户名、不含
     `Users`、不含 `Path.home()`；
  5. 模板源码剥注释后不出现 `stats.root`（防止有人「顺手」加回去）。
"""
import re
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import Client, TestCase, override_settings

from core import wiki as wstore

PROJECT_ROOT = Path(settings.PROJECT_ROOT)


class RootLabelPureTest(TestCase):
    """不给页面渲染，只钉 `root_label()` 自己的口径。"""

    def test_inside_project_gives_relative_segment(self):
        with override_settings(WIKI_DIR=str(PROJECT_ROOT / "data" / "wiki")):
            self.assertEqual(wstore.root_label(), "data/wiki")

    def test_inside_project_but_other_dir_still_relative(self):
        # 换一个项目内的自定义目录，同样只给相对段（别把 data/wiki 写死）
        with override_settings(WIKI_DIR=str(PROJECT_ROOT / "notes")):
            self.assertEqual(wstore.root_label(), "notes")

    def test_outside_project_never_shows_the_path(self):
        with override_settings(WIKI_DIR="/Users/zhangsan/Documents/我的笔记/wiki"):
            label = wstore.root_label()
            self.assertEqual(label, "自定义目录")
            self.assertNotIn("zhangsan", label)
            self.assertNotIn("Documents", label)

    def test_stats_keeps_raw_root_for_scripts(self):
        with override_settings(WIKI_DIR=str(PROJECT_ROOT / "data" / "wiki")):
            st = wstore.stats()
            self.assertEqual(st["root_label"], "data/wiki")
            # ★ root 仍是绝对路径：脚本 / 日志 / 排查要用真路径，别把内部接口
            #   也改成相对段 —— 那会让「日志里到底写的哪个目录」变得不可核。
            self.assertEqual(st["root"], str(PROJECT_ROOT / "data" / "wiki"))
            self.assertNotEqual(st["root"], st["root_label"])


class IndexRenderNoPathTest(TestCase):
    """真渲染：库放在一个**带用户名**的临时路径下，首页不许把它说出去。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-priv-")
        # 故意的：路径里塞上「用户名」，模拟真实机器上的 /Users/<name>/...
        cls.wiki = Path(cls.tmp) / "Users" / "zhangsan" / "data" / "wiki"
        cls.ov = override_settings(WIKI_DIR=str(cls.wiki),
                                   ALLOWED_HOSTS=["testserver"])
        cls.ov.enable()

    @classmethod
    def tearDownClass(cls):
        cls.ov.disable()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        shutil.rmtree(str(self.wiki), ignore_errors=True)
        wstore.ensure_dirs()
        p = self.wiki / "wiki" / "ai" / "a.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("---\ntitle: 页甲\ndomain: ai\nsources: [entry:1]\n"
                     "updated: 2026-11-01\nconfidence: medium\ntags: [安全]\n"
                     "candidates: []\n---\n\n正文。\n", encoding="utf-8")
        self.c = Client()

    def html(self):
        resp = self.c.get("/wiki")
        self.assertEqual(resp.status_code, 200)
        return resp.content.decode("utf-8")

    def test_index_never_leaks_username_or_path(self):
        html = self.html()
        self.assertNotIn("zhangsan", html, "首页把目录里的用户名打出来了")
        self.assertNotIn("Users/", html, "首页把目录路径打出来了")
        self.assertNotIn(str(self.tmp), html, "首页把临时库的绝对路径打出来了")
        home = str(Path.home())
        if home and home not in ("/", "/root"):
            self.assertNotIn(home, html, "首页出现了家目录")

    def test_index_says_custom_dir_instead(self):
        html = self.html()
        self.assertIn("自定义目录", html, "项目外的库要说「自定义目录」，不是留空")

    def test_page_still_renders_normally(self):
        # 反证：不是把整段文案删了糊弄过去 —— 目录说明还在，页表也在
        html = self.html()
        self.assertIn("目录：", html)
        self.assertIn("WIKI_DIR", html)
        self.assertIn("页甲", html)


class TemplateSourceGuardTest(TestCase):
    """模板源码级：只许 `root_label`，不许 `stats.root`（剥注释再判）。"""

    def _tpl(self):
        p = Path(settings.BASE_DIR) / "templates" / "wiki" / "index.html"
        src = p.read_text(encoding="utf-8")
        src = re.sub(r"{#.*?#}", "", src, flags=re.S)
        src = re.sub(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}", "", src, flags=re.S)
        return re.sub(r"<!--.*?-->", "", src, flags=re.S)

    def test_template_uses_root_label(self):
        self.assertIn("{{ stats.root_label }}", self._tpl())

    def test_template_has_no_raw_root(self):
        src = self._tpl()
        self.assertNotIn("stats.root }}", src)
        self.assertNotIn("stats['root']", src)
        self.assertNotIn('stats["root"]', src)
