#!/usr/bin/env python
"""Windows 通知 shim 离线校验（**零真实通知、零 winotify 依赖**）。

做法：
  · 直接 import `core.notify_win` 的纯函数（parse_args / translate / map_sound /
    normalize_icon），逐字段断言 terminal-notifier 方言 → winotify 参数的翻译；
  · 用 monkeypatch 塞一个假的 `winotify` 模块进 sys.modules，调 `main()`，
    断言它按规范构造 Notification（launch/tag/audio/show），全程不弹真通知；
  · 断言 `core.notify` 的后端选择：`_backend()/_launcher()` 在 win 下用
    `sys.executable + shim`，mac 下仍是 terminal-notifier —— 逐参数守住
    "list 传参、shell=False" 这条底线。

用法：python scripts/verify_notify_win.py
"""
import os
import sys
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402
django.setup()

from django.conf import settings  # noqa: E402

from core import notify, notify_win  # noqa: E402

ok = bad = 0
fails = []


def check(name, cond, detail=""):
    global ok, bad
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        bad += 1
        fails.append(name)
        print(f"  FAIL  {name}  <<< {detail}")


class _FakeSound:
    def __init__(self, name):
        self.name = name

    def __str__(self):
        return "sound:" + self.name


def main():
    print("A. parse_args：terminal-notifier 的 -flag value 方言")
    spec = notify_win.parse_args(
        ["-title", "谛听 · L3 点名", "-message", "ABC 生物 Q3 超预期",
         "-subtitle", "雪球 · ABC", "-open", "http://x/1", "-group", "diting-l3-4321",
         "-sound", "Basso", "-appIcon", "file:///tmp/icon.png", "-ignoreDnD"])
    check("标题/正文原样取到",
          spec["title"] == "谛听 · L3 点名" and spec["message"] == "ABC 生物 Q3 超预期",
          str(spec))
    check("subtitle / open / group / sound 各归其位",
          spec["subtitle"] == "雪球 · ABC" and spec["url"] == "http://x/1"
          and spec["group"] == "diting-l3-4321" and spec["sound"] == "Basso",
          str(spec))
    check("ignoreDnD 是无值 flag", spec["ignore_dnd"] is True)

    print("\nB. ★ 含 '-' 开头的标题不被误判成新 flag（不用 argparse 的理由）")
    spec2 = notify_win.parse_args(["-title", "-message 不是新参数", "-message", "正文"])
    check("以 '-' 开头的标题值原样保留", spec2["title"] == "-message 不是新参数",
          spec2["title"])
    check("紧随其后的 -message 仍被正确解析", spec2["message"] == "正文", spec2["message"])

    print("\nC. 特殊字符不被解释（list 传参，逐字保真）")
    evil = '标题 "双引号" $HOME `whoami` \'单引号\' 换行\n第二行'
    spec3 = notify_win.translate(["-title", evil, "-message", evil])
    check("引号/反引号/$/换行原样保留", spec3["title"] == evil and spec3["message"] == evil)

    print("\nD. translate：subtitle 并进正文，信息不丢")
    t = notify_win.translate(["-title", "T", "-message", "M", "-subtitle", "S"])
    check("subtitle 在前、正文在后，用换行分隔", t["message"] == "S\nM", t["message"])
    t2 = notify_win.translate(["-title", "T", "-subtitle", "只有副标题"])
    check("只有 subtitle 时正文就是它", t2["message"] == "只有副标题", t2["message"])

    print("\nE. 音效映射（空 = 静音；Basso → Reminder；未知 → Default）")
    check("None → 静音", notify_win.map_sound(None) is None)
    check("空串 → 静音", notify_win.map_sound("") is None)
    check("default → Default", notify_win.map_sound("default") == "Default")
    check("★ Basso（失败音）→ Reminder", notify_win.map_sound("Basso") == "Reminder")
    check("未知 → Default", notify_win.map_sound("奇怪的音") == "Default")

    print("\nF. 图标路径：file:// 剥离 + 不存在就留空（winotify 要求绝对且存在）")
    check("相对路径留空", notify_win.normalize_icon("favicon.png") == "")
    check("不存在的绝对路径留空",
          notify_win.normalize_icon("file:///no/such/diting-icon.png") == "")
    real = os.path.join(BASE, "static", "favicon-256.png")
    if os.path.exists(real):
        check("file:///C:/... 形态剥掉多余斜杠后存在 → 给绝对路径",
              notify_win.normalize_icon("file://" + real) == os.path.abspath(real),
              notify_win.normalize_icon("file://" + real))
        check("真实白犬图标存在（否则 Windows 侧图标静默失效）", True)
    else:
        check("真实白犬图标存在", False, real)

    print("\nG. ★ main() 用假 winotify：断言构造参数（零真实通知）")
    calls = []

    class FakeNotification:
        def __init__(self, **kw):
            self.kw = kw
            self.tag = None
            self.audio = None
            self.shown = 0
            calls.append(self)

        def set_audio(self, sound, loop=False):
            self.audio = (sound.name, loop)

        def show(self):
            self.shown += 1

    fake = types.ModuleType("winotify")
    fake.Notification = FakeNotification
    fake.audio = types.SimpleNamespace(
        Default=_FakeSound("Default"), Mail=_FakeSound("Mail"),
        Reminder=_FakeSound("Reminder"), SMS=_FakeSound("SMS"),
        Silent=_FakeSound("Silent"))
    saved = sys.modules.get("winotify")
    sys.modules["winotify"] = fake
    try:
        rc = notify_win.main(
            ["-title", "谛听 · L3 点名", "-message", "正文",
             "-open", "http://x/1?sel=feed:1&open=2#row-2",
             "-group", "diting-l3-2", "-sound", "Basso"])
    finally:
        if saved is not None:
            sys.modules["winotify"] = saved
        else:
            sys.modules.pop("winotify", None)

    check("main() 返回 0", rc == 0, str(rc))
    check("恰好构造 1 个 Notification", len(calls) == 1, str(len(calls)))
    if calls:
        n = calls[0]
        check("点击通知体打开深链（launch=-open 的 URL）",
              n.kw.get("launch") == "http://x/1?sel=feed:1&open=2#row-2", str(n.kw))
        check("group 覆盖默认 tag（同条重复会替换，不堆叠）",
              n.tag == "diting-l3-2", str(n.tag))
        check("Basso → Reminder 音效", n.audio == ("Reminder", False), str(n.audio))
        check("show() 被调用一次", n.shown == 1, str(n.shown))

    print("\nH. 没装 winotify 时返回 2（notify.py 会记 warning，不静默）")
    saved = sys.modules.pop("winotify", None)
    try:
        rc2 = notify_win.main(["-title", "T", "-message", "M"])
        check("ImportError → 退出码 2", rc2 == 2, str(rc2))
    finally:
        if saved is not None:
            sys.modules["winotify"] = saved

    print("\nI. ★ 后端选择：mac 路径一字不变，win 走 sys.executable + shim")
    saved_backend = getattr(settings, "NOTIFY_BACKEND", "auto")
    try:
        settings.NOTIFY_BACKEND = "mac"
        check("mac → _backend()=='mac'", notify._backend() == "mac")
        check("mac → launcher 仍是单个 terminal-notifier",
              notify._launcher() == [settings.NOTIFY_BIN], str(notify._launcher()))
        check("mac → launcher 里没有 python 解释器（行为不变）",
              sys.executable not in notify._launcher())
        settings.NOTIFY_BACKEND = "win"
        check("win → _backend()=='win'", notify._backend() == "win")
        la = notify._launcher()
        check("★ win → [sys.executable, shim.py]（同 venv，import 得到 winotify）",
              len(la) == 2 and la[0] == sys.executable and la[1].endswith("notify_win.py"),
              str(la))
        check("win → shim 文件确实存在", os.path.exists(la[1]), la[1] if la else "")
        check("★ 全程 list 传参（源码里没有 shell=True）",
              "shell=True" not in open(os.path.join(BASE, "core", "notify_win.py"),
                                       encoding="utf-8").read())
        check("★ 仍同步发（notify.py 不做丢线程）",
              "threading" not in open(os.path.join(BASE, "core", "notify.py"),
                                      encoding="utf-8").read())
    finally:
        settings.NOTIFY_BACKEND = saved_backend

    print("\nJ. 源码契约：设置项齐全")
    cfg = open(os.path.join(BASE, "config", "settings.py"), encoding="utf-8").read()
    check("settings 有 NOTIFY_BACKEND / NOTIFY_SHIM",
          "NOTIFY_BACKEND" in cfg and "NOTIFY_SHIM" in cfg)
    check("notify.py 有 _backend/_launcher/_shim",
          all(f"def {x}" in open(os.path.join(BASE, "core", "notify.py"),
                                encoding="utf-8").read()
              for x in ("_backend", "_launcher", "_shim", "_ready")))

    print("\nK. ★ 依赖单文件：winotify 带 PEP 508 标记，只对 win32 装")
    req_path = os.path.join(BASE, "requirements.txt")  # 与 reader/requirements.txt 同目录
    check("找到 requirements.txt", os.path.exists(req_path), req_path)
    if req_path:
        req = open(req_path, encoding="utf-8").read()
        lines = [ln for ln in req.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        wino = [ln for ln in lines if ln.lower().startswith("winotify")]
        check("winotify 恰好声明一次（单文件，没有 requirements-win.txt 对拷）",
              len(wino) == 1, str(wino))
        check("★ winotify 带 sys_platform == win32 标记（mac/linux 自动跳过）",
              bool(wino) and "sys_platform" in wino[0]
              and "win32" in wino[0], str(wino))
        # 不该冒出第二个 requirements 文件造成双份维护
        import glob
        extra = glob.glob(os.path.join(os.path.dirname(req_path), "requirements-win*.txt"))
        check("不存在单独的 requirements-win.txt（避免依赖同步漂移）",
              not extra, str(extra))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        bad += 1
        fails.append(f"异常: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()

    print("\n" + "=" * 68)
    print(f"结果：{ok} 通过 / {bad} 失败")
    if fails:
        print("失败项：", fails)
    print("=" * 68)
    sys.exit(1 if bad else 0)
