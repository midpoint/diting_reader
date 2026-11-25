"""验证脚本不得静默改真实库 —— 把「隔离」变成可被机器检查的契约。

★ 为什么需要这个测试（2026-10-31 事故）：
   事故的根因不是"某个脚本写错了一行"，而是**没有任何机制拦住它**。
   `verify_wiki.py` 直接对真实 `data/reader.sqlite3` 执行
   `Highlight.objects.all().delete()`，于是每跑一次验证就清空用户的划线；
   而脚本自己是全绿的 —— 它连"我删了用户的数据"都不认为是个问题。
   代价：96 条划线 + 28 条批注，`.recover` 从 freelist 一行都捞不回来。

   只把出事的那一个脚本改掉是不够的：下周新写一个脚本，同样的事会再来一次，
   而且失败依旧是静默的（"验证全绿"正是最糟的那种成功）。
   所以这里把纪律写成断言 —— 新增脚本忘了隔离，`manage.py test` 当场红。

★ 判定口径：
   - 只读脚本（没有写库调用）不需要隔离，跳过；
   - 有写库调用的，必须在 `django.setup()` 之后、`from core.models import` 之前
     调用 `_dbtest.isolate()`；
   - 确实不能隔离的（要用真实数据渲染页面才测得准），列进 EXEMPT 并写明理由，
     同时必须自己保证「写前快照、写后精确还原」。
"""

import re
from pathlib import Path

from django.test import SimpleTestCase

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

#: 豁免清单。每一条都要说明**为什么不能切内存库**，否则豁免会变成后门。
EXEMPT = {
    "verify_ui_shell.py": (
        "它用 Django test client 渲染真实页面来验页面外壳（表格列数、导航高亮、"
        "周报行样式），切到空库这些断言全部退化成「空页面没有 XX」，等于没测。"
        "纪律是只读 + 写前快照、写后按原 pk 全字段还原（见脚本 _probe 段的 try/finally）"
    ),
}

#: 会改变数据库的调用。注意 `filter(...).delete()` 也要抓到 ——
#: 第一版排查时只写了 `objects.all().delete()`，漏掉了这种，差点放过 verify_weekly。
WRITE_RE = re.compile(r"\.(delete|create|update|bulk_create|get_or_create|save)\s*\(")

#: 长得像、但**不是**写库的调用，必须排除，否则守卫会自己变成噪音：
#:   - `cache.delete("settingkit:...")` 是 Django 缓存（verify_notify / verify_triage_state 都有）
#:   - `dict.update({...})` 是普通字典（统计累加）
#: 一开始没排除，测试当场把这两个脚本报成"未隔离"—— 误报比漏报更糟：
#: 一旦有人为了让它变绿而把脚本塞进豁免清单，守卫就废了。
NOT_DB_RE = re.compile(r"\bcache\.delete\s*\(|\.update\s*\(\s*\{")

#: 会真发系统通知（或走进会发通知的分支）的调用。`wk.run()` 这类不是直接
#: notify.send，但 `weekly._fail()` 会调到它 —— 2026-11-05 就是它每跑一次回归
#: 就在桌面弹一条「周报未生成」。
NOTIFY_RE = re.compile(
    r"notify\.send\s*\(|notify\.(daily|daily_failed|l3|weekly)\s*\("
    r"|notify_weekly_failed\s*\(|\b(?:wk|weekly|summ|summary)\.run\s*\(")

#: 不隔离却调 notify 的脚本，必须逐条写明「为什么不会弹真通知」，
#: 否则豁免就是后门（与 EXEMPT 同一纪律）。
NOTIFY_EXEMPT = {
    "verify_notify.py": (
        "把 NOTIFY_BIN 指向自建的假 terminal-notifier（只把 argv 写进 JSONL），"
        "逐参数断言命令组装；全程零真实通知，隔离它反而会让命令断言失效。"
    ),
}

def isolate_silences_notifications(text: str) -> bool:
    """`_dbtest.py` 的 isolate() 里是否真的装了静默桩（判据只此一份）。

    抽成函数是为了让守卫自己能被反证：把那行删掉喂进来，必须返回 False。
    ★ 只认隔离入口这一处 —— 生产代码（core/notify.py）里加任何"测试态就静默"
      的判断都算违规：失败通知是 FOMO 防线，不该被测试的存在改掉。
    """
    if "def isolate(" not in text:
        return False
    body = text.split("def isolate(", 1)[1].split("\ndef ", 1)[0]
    return "mute_notifications()" in body


def isolate_redirects_files(text: str) -> bool:
    """`isolate()` 里是否真的切了文件写入点（判据只此一份，供反证）。"""
    if "def isolate(" not in text:
        return False
    body = text.split("def isolate(", 1)[1].split("\ndef ", 1)[0]
    return "isolate_files()" in body


def _load_dbtest():
    """按路径装载 `scripts/_dbtest.py`（scripts/ 不在测试进程的 sys.path 上）。"""
    import importlib.util
    path = SCRIPTS / "_dbtest.py"
    spec = importlib.util.spec_from_file_location("_dbtest_probe", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _writes_db(path):
    for line in _code_lines(path):
        if WRITE_RE.search(line) and not NOT_DB_RE.search(line):
            return True
    return False

MODELS_IMPORT_RE = re.compile(r"^from core\.models import", re.M)


def _code_lines(path):
    """排除注释行：注释里提到 `.delete()` 不算真调用（否则到处都是误报）。"""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        yield line


class ScriptIsolationTest(SimpleTestCase):

    def test_scripts_that_write_must_isolate(self):
        offenders = []
        for path in sorted(SCRIPTS.glob("verify_*.py")):
            if path.name in EXEMPT:
                continue
            if not _writes_db(path):
                continue                       # 只读脚本，无需隔离
            if "_dbtest.isolate()" not in "\n".join(_code_lines(path)):
                offenders.append(path.name)
        self.assertEqual(
            offenders, [],
            "这些验证脚本会写数据库却没隔离，跑一次就会改动真实数据：%s"
            "（修复：django.setup() 之后加 `import _dbtest; _dbtest.isolate()`；"
            "确实不能隔离的，加进本文件的 EXEMPT 并写明理由）" % offenders)

    def test_isolate_happens_before_models_are_imported(self):
        """isolate() 必须在 import models 之前，否则切换测试库的时机晚于取连接。"""
        for path in sorted(SCRIPTS.glob("verify_*.py")):
            if path.name in EXEMPT:
                continue
            text = path.read_text(encoding="utf-8")
            if "_dbtest.isolate()" not in text:
                continue
            at = text.index("_dbtest.isolate()")
            m = MODELS_IMPORT_RE.search(text)
            if m and m.start() < at:
                self.fail("%s：先 `from core.models import` 才 isolate()，"
                          "隔离对已取到的连接不生效 —— 把 isolate 提到 models 之前"
                          % path.name)

    def test_exempted_scripts_restore_instead_of_deleting(self):
        """拿了豁免的脚本不能借豁免权裸删：必须留下「原样还原」的断言。"""
        for name in EXEMPT:
            path = SCRIPTS / name
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            self.assertIn("原样还原", text,
                          "%s 在 EXEMPT 里，必须以「快照 + 原样还原」替代删除，"
                          "并在脚本里断言还原结果" % name)

    def test_exempt_list_has_reasons(self):
        """豁免必须带理由，否则它就是个后门。"""
        for name, reason in EXEMPT.items():
            self.assertGreaterEqual(len(reason.strip()), 30,
                                    "EXEMPT[%r] 的理由太短，说清为什么不能隔离" % name)


class NotificationSilenceTest(SimpleTestCase):
    """跑验证不许在桌面弹真通知；**只静默验证进程，生产通知一行不动**。

    2026-11-05：`verify_weekly.py` 的「缺历史日报」分支真调 `weekly._fail()`，
    每跑一次回归弹一条「谛听 · 周报未生成」。修法是在 `_dbtest.isolate()`
    （所有验证脚本的单一入口）里把 `core.notify.send` 换成只记账不发进程的桩。
    """

    def test_scripts_that_notify_must_isolate_or_be_exempt(self):
        """扫全集：会发通知的脚本必须走 isolate()，否则进豁免并写明理由。"""
        offenders = []
        for path in sorted(SCRIPTS.glob("verify_*.py")):
            if path.name in NOTIFY_EXEMPT:
                continue
            code = "\n".join(_code_lines(path))
            if NOTIFY_RE.search(code) and "_dbtest.isolate()" not in code:
                offenders.append(path.name)
        self.assertEqual(
            offenders, [],
            "这些验证脚本会发真实系统通知却没隔离，跑一次就弹一次窗：%s"
            "（修复：django.setup() 之后加 `import _dbtest; _dbtest.isolate()`；"
            "确实零真实通知的，加进 NOTIFY_EXEMPT 并写明理由）" % offenders)

    def test_notify_exempt_has_reasons(self):
        for name, reason in NOTIFY_EXEMPT.items():
            self.assertGreaterEqual(len(reason.strip()), 30,
                                    "NOTIFY_EXEMPT[%r] 理由太短，说清为什么零真实通知" % name)
            self.assertTrue((SCRIPTS / name).exists(),
                            "NOTIFY_EXEMPT 里的 %r 不存在 —— 别留过期豁免" % name)

    def test_isolate_installs_the_mute_stub(self):
        """守卫自己会响：判据认得出现在的实现，也认得出被删掉的实现。

        ★ 反证不能省：只断言「installed=True」的守卫，在有人把
          `mute_notifications()` 从 isolate() 里删掉后依然可能靠一句注释骗过。
          这里把那行真删掉喂进同一个判据，必须返回 False。
        """
        src = (SCRIPTS / "_dbtest.py").read_text(encoding="utf-8")
        self.assertTrue(isolate_silences_notifications(src),
                        "_dbtest.isolate() 没装静默桩，跑验证会弹真通知")
        broken = src.replace("mute_notifications()     #", "pass                     #")
        self.assertNotEqual(broken, src, "反证没生效：源码里找不到那行调用")
        self.assertFalse(isolate_silences_notifications(broken),
                         "守卫自己不会响：删掉静默桩后判据仍返回 True")

    def test_production_notify_has_no_test_switches(self):
        """生产通知代码里不许出现测试态判断 —— 失败通知是 FOMO 防线。"""
        src = (SCRIPTS.parent / "core" / "notify.py").read_text(encoding="utf-8")
        for banned in ("_dbtest", "MUTED_NOTIFICATIONS", "setup_test_environment",
                       "PYTEST_CURRENT_TEST", "DJANGO_TEST"):
            self.assertNotIn(banned, src,
                             "core/notify.py 混进了测试态判断 %r：静默只能发生在验证进程"
                             % banned)


class FileIsolationTest(SimpleTestCase):
    """验证脚本不许写真实**文件**目录（2026-11-05 事故的守卫）。

    事故：隔离只切了数据库，`REPORT_DIR` / `SUMMARY_CACHE_DIR` 还是真实的，
    一次补跑把 4 份日报换成规则版、写了 40 个缓存文件。
    ★ 与「不碰 DB」是同一类错的两面：只想着数据库，忘了文件也是用户的数据。
      这条守卫扫的是**写入点清单是否齐全**，不是"记得改过没有"。
    """

    def test_isolate_calls_file_redirection(self):
        src = (SCRIPTS / "_dbtest.py").read_text(encoding="utf-8")
        self.assertTrue(isolate_redirects_files(src),
                        "isolate() 没切文件目录：验证脚本一跑就可能写真实 data/")
        broken = src.replace("    isolate_files()          #", "    pass                     #")
        self.assertNotEqual(broken, src, "反证没生效：源码里找不到那行调用")
        self.assertFalse(isolate_redirects_files(broken),
                         "守卫自己不会响：删掉 isolate_files() 后判据仍返回 True")

    def test_write_targets_are_all_enumerated(self):
        """写入点清单必须齐全 —— 漏一个 = 那个目录照旧被写。"""
        mod = _load_dbtest()
        self.assertEqual(set(mod._FILE_DIRS),
                         {"REPORT_DIR", "WIKI_DIR", "SUMMARY_CACHE_DIR"},
                         "文件写入点清单变了：新增写入点要一起加进 _FILE_DIRS")

    def test_isolate_files_actually_redirects_settings(self):
        """行为反证：调用后三个 settings 都指向临时目录，且目录真的建出来了。"""
        import os

        from django.conf import settings

        mod = _load_dbtest()
        orig = {n: getattr(settings, n) for n in mod._FILE_DIRS}
        try:
            tmp = mod.isolate_files()
            for name in mod._FILE_DIRS:
                now = str(getattr(settings, name))
                self.assertTrue(now.startswith(tmp),
                                "%s 没被切到临时目录：%s" % (name, now))
                self.assertTrue(os.path.isdir(now), "%s 目录没建出来：%s" % (name, now))
        finally:
            for name, val in orig.items():
                setattr(settings, name, val)
            mod._cleanup_files()
