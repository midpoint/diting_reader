"""Windows 通知 shim —— 把 terminal-notifier 的参数翻译成 winotify 调用。

为什么需要它
------------
`core/notify.py` 把通知参数按 terminal-notifier 的方言拼好（`-title` / `-message` /
`-open` / `-group` / …）。Windows 没有 terminal-notifier；本脚本作为**同一次调用的
替身**：`notify.py` 用当前 venv 的 Python 解释器跑这个文件，参数原样传入，这里
翻译成 winotify 的 `Notification(...)`。于是业务代码（l3 / daily / daily_failed）
一个字都不用改，mac 路径也完全不受影响。

为什么是 Python 脚本而不是 .cmd 包装
------------------------------------
Windows 上 `subprocess.run(shell=False)` 走 CreateProcess，**不能直接启动 .bat/.cmd**
（apktool 项目踩过：FileNotFoundError；且系统可能隐式走 shell 重新解析参数，
破坏"参数 list 传、shell=False、标题里的引号/$/换行不被解释"这条底线）。
用 `sys.executable + 本文件` 则全程 list 传参，且天然 import 得到同一个 venv 里
pip 装的 winotify。

依赖：``pip install winotify``（纯 Python，零编译依赖，只要系统自带 PowerShell）。
没有 winotify 时返回退出码 2，并写一行 stderr —— notify.py 会记 warning，不静默。

三条必须如实说的限制（未真机验证，Windows 侧待实测）
------------------------------------------------------
1. winotify 内部用 ``Popen`` 起 PowerShell 后**立即返回**，脚本本身同步跑完 ≠ 通知
   已投递。Windows 正常桌面会话里 PowerShell 子进程独立存活、一般会送达；但这里
   无法像 mac 的 terminal-notifier 那样用退出码自证"已发出"。
2. `-ignoreDnD`（穿透勿扰）：Windows 没有等价能力，收到也忽略。
3. `-sound`：mac 的 Basso/default 映射成 winotify 的近似音效；空 = 静音。
"""
import os
import re
import sys

# terminal-notifier 的 "-flag value" → spec 键。位置敏感，手动解析，不用 argparse
# （argparse 遇到以 "-" 开头的标题/正文会误判成新 flag）。
_VALUE_FLAGS = {
    "-title": "title",
    "-message": "message",
    "-subtitle": "subtitle",
    "-open": "url",
    "-group": "group",
    "-sound": "sound",
    "-appIcon": "icon",
}
_FLAG_FLAGS = {"-ignoreDnD": "ignore_dnd"}

# mac 音效名 → winotify.audio 常量名。未知一律 Default；失败音 Basso 用
# Reminder 近似（winotify 没有低沉提示音）。None/"" = 静音（winotify 默认 silent）。
_SOUND_MAP = {
    "default": "Default",
    "basso": "Reminder",
    "ping": "Default",
    "glass": "Default",
    "mail": "Mail",
    "reminder": "Reminder",
    "sms": "SMS",
}


def parse_args(argv):
    """把 terminal-notifier 风格的 argv 解析成 spec dict。

    取值一律取 flag 后的下一个 token；多余的、不认识的参数跳过（前向兼容）。
    """
    spec = {
        "title": "", "message": "", "subtitle": "", "url": "", "group": "",
        "sound": None, "icon": "", "ignore_dnd": False,
    }
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _VALUE_FLAGS:
            if i + 1 < len(argv):
                spec[_VALUE_FLAGS[a]] = argv[i + 1]
                i += 2
                continue
            i += 1
        elif a in _FLAG_FLAGS:
            spec[_FLAG_FLAGS[a]] = True
            i += 1
        else:
            i += 1
    return spec


def normalize_icon(raw):
    """`file://<path>` → Windows 绝对路径；文件不存在就留空（winotify 会不显示）。

    notify.py 在 Windows 上拼出的是 `file://C:\\...\\favicon-256.png`（可能带一个
    多余的斜杠变 `file:///C:/...`）。两种都处理。
    """
    if not raw:
        return ""
    p = raw[7:] if raw.startswith("file://") else raw
    # file:///C:/... → C:/...
    if re.match(r"^/[A-Za-z]:", p):
        p = p[1:]
    if not os.path.isabs(p):
        return ""
    return os.path.abspath(p) if os.path.exists(p) else ""


def map_sound(raw):
    """音效名 → winotify.audio 常量名（未知→Default；空→None=静音）。"""
    if raw is None or str(raw).strip() == "":
        return None
    return _SOUND_MAP.get(str(raw).strip().lower(), "Default")


def translate(argv):
    """参数翻译（纯函数，便于离线逐字段断言，不碰任何真实通知）。"""
    s = parse_args(argv)
    msg = s["message"]
    if s["subtitle"]:
        # winotify 没有 subtitle 字段；并进正文，信息不丢。
        msg = (s["subtitle"] + "\n" + msg) if msg else s["subtitle"]
    return {
        "title": s["title"],
        "message": msg,
        "url": s["url"],
        "group": s["group"],
        "icon": normalize_icon(s["icon"]),
        "sound": map_sound(s["sound"]),
        "ignore_dnd": s["ignore_dnd"],  # 记录但不做事：Windows 无等价能力
    }


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    spec = translate(argv)
    try:
        from winotify import Notification, audio
    except ImportError:
        sys.stderr.write("diting notify: 需要 winotify（pip install winotify）\n")
        return 2

    try:
        # launch=url → 点击**通知体**打开深链（对应 terminal-notifier 的 -open，
        # 比 add_actions 的按钮更贴近原语义）。没有 url 就传空串。
        toast = Notification(
            app_id="Diting",
            title=spec["title"] or "谛听",
            msg=spec["message"],
            icon=spec["icon"],
            duration="short",
            launch=spec["url"] or "",
        )
        # 用 -group 覆盖 winotify 默认的 tag（默认 = title），使同一条重复通知
        # 在通知中心替换而非堆叠 —— 对齐 mac 端 -group 的去重语义。
        if spec["group"]:
            toast.tag = spec["group"]
        snd = spec["sound"]
        if snd:  # None = 静音（winotify 默认就是 silent）
            toast.set_audio(getattr(audio, snd, audio.Default), loop=False)
        toast.show()
    except Exception as exc:  # noqa: BLE001  通知失败绝不影响调用方
        sys.stderr.write("diting notify: 发送失败 %s: %s\n" % (type(exc).__name__, exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
