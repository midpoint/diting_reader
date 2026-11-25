#!/usr/bin/env python
"""`core/notify.py` 离线校验（**零真实通知**）。

做法：把 `NOTIFY_BIN` 指向一个"假 terminal-notifier"（一个把 argv 写成 JSONL 的
脚本）——于是：

  · 命令是怎么拼的，可以**逐字逐参数**断言（含中文 / 引号 / 换行 / `$` 是否被解释）；
  · 全程不会弹出任何真实通知（假 binary 只写文件）；
  · `send()` 是同步的 ⇒ 返回时 fake log 里必须已经有记录，**断言不靠 sleep 抢时序**。

为什么必须逐参数断言：terminal-notifier 的参数是**位置敏感**的（`-title` 后面
跟的就是标题），拼错一个顺序就是"通知发出来了、但内容错位"，肉眼很难发现。

用法：python scripts/verify_notify.py
"""
import json
import os
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402
django.setup()

from django.conf import settings  # noqa: E402

from core import notify  # noqa: E402

# ★ 本脚本是 **mac 向** 的（用假 terminal-notifier 逐参数断言 mac 调用链），
#   在 Windows 上 notify 走 winotify shim，测不到假 binary，38 项会全失败。
#   Windows 的对应自检是 scripts/verify_notify_win.py，这里直接跳过。
if sys.platform == "win32":
    print("跳过：本脚本校验 macOS terminal-notifier 调用链，"
          "Windows 请运行 scripts/verify_notify_win.py")
    sys.exit(0)

ok = bad = skip = 0
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


def read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


# ==================================================================== 测试脚手架
TMP = tempfile.mkdtemp(prefix="diting-notify-")
FAKE_LOG = os.path.join(TMP, "calls.jsonl")
FAKE_BIN = os.path.join(TMP, "terminal-notifier")
with open(FAKE_BIN, "w", encoding="utf-8") as f:
    f.write(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "with open(os.environ['FAKE_NOTIFY_LOG'], 'a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:], ensure_ascii=False) + '\\n')\n"
        "sys.exit(int(os.environ.get('FAKE_NOTIFY_CODE', '0')))\n"
    )
os.chmod(FAKE_BIN, 0o755)
os.environ["FAKE_NOTIFY_LOG"] = FAKE_LOG


class _E:
    """最小 entry 替身（只要 id / feed_id / title 三个属性）。"""

    def __init__(self, eid=123, fid=7, title="标题", feed_title="源"):
        self.id, self.feed_id, self.title, self.feed_title = eid, fid, title, feed_title


#: 保存真实配置，跑完恢复（这个脚本操作的是**真** settings 对象）
_SAVED = {k: getattr(settings, k) for k in (
    "NOTIFY_ENABLE", "NOTIFY_BIN", "NOTIFY_SOUND", "NOTIFY_TIMEOUT",
    "NOTIFY_L3", "NOTIFY_DAILY", "NOTIFY_DAILY_FAIL", "NOTIFY_FAIL_SOUND",
    "NOTIFY_IGNORE_DND", "NOTIFY_APP_ICON", "NOTIFY_READER_BASE")}


def cfg(**kw):
    """改 settings 值。★ notify.py 的读取点已走 sk.effective()（带 15s 缓存），
    所以每改一项必须同步清缓存，否则后续断言读到的是上一轮的旧值。"""
    from django.core.cache import cache

    for k, v in kw.items():
        setattr(settings, "NOTIFY_" + k, v)
        cache.delete("settingkit:notify_" + k.lower())


def calls():
    """假 binary 收到的所有 argv 列表。"""
    if not os.path.exists(FAKE_LOG):
        return []
    with open(FAKE_LOG, encoding="utf-8") as f:
        return [json.loads(x) for x in f if x.strip()]


def reset():
    if os.path.exists(FAKE_LOG):
        os.remove(FAKE_LOG)


def main():
    print("A. 命令组装：逐参数断言（位置敏感，拼错就是内容错位）")
    cfg(ENABLE=True, BIN=FAKE_BIN, SOUND="default", APP_ICON="", READER_BASE="http://127.0.0.1:8099")
    ICON = "file://" + os.path.join(BASE, "static", "favicon-256.png")
    reset()
    sent = notify.send("T", "M", subtitle="S", url="http://x/1", group="g1")
    c = calls()
    check("send() 返回 True（退出码 0）", sent is True)
    check("★ 同步执行：send() 一返回，fake log 里就该已经有记录（不靠 sleep 抢时序）",
          len(c) == 1, str(len(c)))
    check("假 binary 恰好收到 1 次调用", len(c) == 1, str(c))
    check("参数顺序与取值完全正确",
          c and c[0] == ["-title", "T", "-message", "M", "-subtitle", "S",
                         "-open", "http://x/1", "-group", "g1", "-sound", "default",
                         "-appIcon", ICON],
          str(c[0] if c else None))

    print("\nB. 特殊字符不注入（list 传参，shell=False）")
    reset()
    evil = '标题 "双引号" $HOME `whoami` \'单引号\' 换行\n第二行'
    notify.send(evil, evil)
    c = calls()
    check("含引号/反引号/$/换行的内容原样传递",
          c and c[0][1] == evil and c[0][3] == evil, str(c[0] if c else None))
    check("没有被 shell 解释（参数个数不多不少：-title X -message X -sound default -appIcon X）",
          c and len(c[0]) == 8, str(len(c[0]) if c else 0))

    print("\nC. 可选参数：空值不出现（否则 terminal-notifier 会吃掉下一个参数）")
    reset()
    cfg(SOUND="")
    notify.send("T", "M")
    c = calls()
    check("未给 subtitle/url/group 时，对应开关整体不出现",
          c and c[0] == ["-title", "T", "-message", "M", "-appIcon", ICON],
          str(c[0] if c else None))
    check("NOTIFY_SOUND 为空 → 无 -sound", c and "-sound" not in c[0])
    cfg(SOUND="default")

    print("\nD. 开关：关掉的一类**一次调用都不发**")
    reset()
    cfg(ENABLE=False)
    check("NOTIFY_ENABLE=0 → send 返回 False", notify.send("T", "M") is False)
    check("NOTIFY_ENABLE=0 → 零调用", calls() == [])
    cfg(ENABLE=True)

    reset()
    cfg(L3=False)
    check("NOTIFY_L3=0 → l3() 返回 False", notify.l3(_E()) is False)
    check("NOTIFY_L3=0 → 零调用", calls() == [])
    cfg(L3=True)

    reset()
    cfg(DAILY=False)
    check("NOTIFY_DAILY=0 → daily() 返回 False", notify.daily("2026-10-16") is False)
    check("NOTIFY_DAILY=0 → 零调用", calls() == [])
    cfg(DAILY=True)

    reset()
    cfg(DAILY_FAIL=False)
    check("NOTIFY_DAILY_FAIL=0 → daily_failed() 返回 False",
          notify.daily_failed("2026-10-16", "boom") is False)
    check("NOTIFY_DAILY_FAIL=0 → 零调用", calls() == [])
    cfg(DAILY_FAIL=True)
    check("★ 失败开关与完成开关**互相独立**（关失败不影响完成）",
          notify.daily("2026-10-16") is True)
    reset()

    print("\nE. L3 深链：拼装（定位 + 自动展开）+ group 去重键")
    reset()
    e = _E(eid=4321, fid=12, title="ABC 生物 Q3 超预期", feed_title="雪球 · ABC 生物")
    notify.l3(e)
    c = calls()
    check("L3 打开的是阅读器，定位到该条**并自动展开**（open=<条目>）",
          c and "http://127.0.0.1:8099/?sel=feed:12&open=4321&src=l3#row-4321" in c[0],
          str(c[0] if c else None))
    check("L3 深链带 src=l3（utm 来源标记，clicktrack 记录用）",
          c and "&src=l3" in c[0][c[0].index("-open") + 1], str(c[0] if c else None))
    check("L3 group = diting-l3-<entry_id>（同条重复会替换，不叠加）",
          c and c[0][c[0].index("-group") + 1] == "diting-l3-4321",
          str(c[0] if c else None))
    check("L3 标题带「谛听 · L3 点名」、副标题是源名",
          c and c[0][c[0].index("-title") + 1] == "谛听 · L3 点名"
          and c[0][c[0].index("-subtitle") + 1] == "雪球 · ABC 生物",
          str(c[0] if c else None))
    check("L3 正文是条目标题", c and c[0][c[0].index("-message") + 1] == "ABC 生物 Q3 超预期")

    check("reader_entry_url() 直接可用（无 src 时 URL 与历史格式一致）",
          notify.reader_entry_url(12, 4321)
          == "http://127.0.0.1:8099/?sel=feed:12&open=4321#row-4321",
          notify.reader_entry_url(12, 4321))
    check("★ url 里 `open=` 与 `#row-` 用同一个 id（展开的就是滚到的那条）",
          notify.reader_entry_url(12, 4321).split("&open=")[1].split("#")[0]
          == notify.reader_entry_url(12, 4321).split("#row-")[1])
    check("★ 锚点必须留在最末尾（`#` 之后写参数会被当成片段的一部分）",
          notify.reader_entry_url(1, 2).rindex("#") > notify.reader_entry_url(1, 2).rindex("&open="))
    cfg(READER_BASE="http://127.0.0.1:8099/")
    check("NOTIFY_READER_BASE 末尾带 / 也不会出现 //",
          notify.reader_entry_url(1, 2)
          == "http://127.0.0.1:8099/?sel=feed:1&open=2#row-2",
          notify.reader_entry_url(1, 2))
    cfg(READER_BASE="")

    print("\nF. 日报完成通知：落点是本地日报页")
    reset()
    cfg(READER_BASE="http://127.0.0.1:8099")
    notify.daily("2026-10-16")
    c = calls()
    check("★ 点击直达当天日报页",
          c and c[0][c[0].index("-open") + 1]
          == "http://127.0.0.1:8099/summary/2026-10-16?src=daily-notify",
          str(c[0] if c else None))
    check("日报标题 = 谛听日报 · <日期>",
          c and c[0][c[0].index("-title") + 1] == "谛听日报 · 2026-10-16")
    check("日报正文 = 一句事实「已生成」（不做教学：日期在标题里、通知本身可点）",
          c and c[0][c[0].index("-message") + 1] == "已生成",
          str(c[0] if c else None))
    check("成功通知不带副标题（副标题留给失败通知放状态）",
          c and "-subtitle" not in c[0])
    check("日报 group = diting-daily-<日期>",
          c and c[0][c[0].index("-group") + 1] == "diting-daily-2026-10-16")

    reset()
    notify.daily("2026-10-16", degraded=True)
    c = calls()
    check("★ 规则版降级必须在通知里说出来（否则点开才发现只有条目清单）",
          c and "规则版" in c[0][c[0].index("-message") + 1],
          str(c[0] if c else None))

    reset()
    cfg(READER_BASE="")
    notify.daily("2026-10-16")
    c = calls()
    check("没配 READER_BASE 时通知照发、只是不带链接",
          c and "-open" not in c[0], str(c[0] if c else None))

    print("\nF2. 日报**失败**通知：必须出声（静默失败 = FOMO 源头）")
    reset()
    cfg(READER_BASE="http://127.0.0.1:8099")
    notify.daily_failed("2026-10-16", "RuntimeError: 采集全部超时",
                        attempts=1, max_attempts=5)
    c = calls()
    check("失败也发通知（不是只有成功才出声）", len(c) == 1, str(c))
    check("标题写明「生成失败」，日期同成功那条",
          c and c[0][c[0].index("-title") + 1] == "谛听日报 · 2026-10-16 生成失败",
          str(c[0] if c else None))
    check("正文带上原始错误（否则只有『失败了』，还得自己去翻日志）",
          c and "采集全部超时" in c[0][c[0].index("-message") + 1],
          str(c[0] if c else None))
    check("副标题说明「还有几次重试」（决定要不要现在管）",
          c and c[0][c[0].index("-subtitle") + 1] == "第 1/5 次 · 稍后自动重试",
          str(c[0] if c else None))
    check("★ group 与成功那条**分开**（diting-daily-fail-<日期>）",
          c and c[0][c[0].index("-group") + 1] == "diting-daily-fail-2026-10-16",
          str(c[0] if c else None))
    check("★ 失败与成功**不会互相顶掉**（group 不同）",
          "diting-daily-fail-" not in "diting-daily-2026-10-16")
    check("★ 点击落点是**总结页** /summary（那里有状态、错误原因和「重跑」按钮）",
          c and c[0][c[0].index("-open") + 1] == "http://127.0.0.1:8099/summary?src=daily-fail-notify",
          str(c[0] if c else None))
    check("失败用单独的提示音（默认 Basso，与成功的 default 区分得开）",
          c and c[0][c[0].index("-sound") + 1] == "Basso", str(c[0] if c else None))

    reset()
    notify.daily_failed("2026-10-16", "boom", attempts=5, max_attempts=5, terminal=True)
    c = calls()
    check("重试已用尽 → 副标题明说要人介入",
          c and c[0][c[0].index("-subtitle") + 1] == "重试已用尽 · 需人工处理",
          str(c[0] if c else None))

    reset()
    notify.daily_failed("2026-10-16", "boom", attempts=0, max_attempts=5)
    c = calls()
    check("抢占用之前就失败（还没进重试计数）→ 说「稍后自动重试」，不是干巴巴一句失败",
          c and c[0][c[0].index("-subtitle") + 1] == "稍后自动重试",
          str(c[0] if c else None))

    reset()
    notify.daily_failed("2026-10-16", "x" * 400)
    c = calls()
    check("超长错误被截断（通知里有上限，别把一整段 traceback 塞进去）",
          c and len(c[0][c[0].index("-message") + 1]) <= 160,
          str(len(c[0][c[0].index("-message") + 1]) if c else 0))

    reset()
    notify.daily_failed("2026-10-16", "第一行\n第二行\t带空白")
    c = calls()
    check("换行/制表符压成空格（多行消息在通知里会被截成一截）",
          c and "\n" not in c[0][c[0].index("-message") + 1]
          and c[0][c[0].index("-message") + 1] == "第一行 第二行 带空白",
          str(c[0] if c else None))

    reset()
    cfg(FAIL_SOUND="")
    notify.daily_failed("2026-10-16", "boom")
    check("NOTIFY_FAIL_SOUND 留空 → 失败静音（但通知照发）",
          calls() and "-sound" not in calls()[0], str(calls()))
    cfg(FAIL_SOUND="Basso")

    reset()
    cfg(READER_BASE="")
    notify.daily_failed("2026-10-16", "boom")
    check("没有能拼出的本地地址 → 不带 -open（宁可点不动，也别给死链）",
          calls() and "-open" not in calls()[0], str(calls()))
    cfg(READER_BASE="http://127.0.0.1:8099")

    print("\nG. 通知图标：默认用白犬 PNG")
    reset()
    cfg(APP_ICON="")
    notify.send("T", "M")
    c = calls()
    icon = c[0][c[0].index("-appIcon") + 1] if c and "-appIcon" in c[0] else ""
    png = os.path.join(BASE, "static", "favicon-256.png")
    check("默认 -appIcon = static/favicon-256.png（file:// URL）",
          icon == "file://" + png, icon)
    check("该 PNG 确实存在（否则图标静默失效）", os.path.exists(png))
    reset()
    cfg(APP_ICON="/tmp/x.png")
    notify.send("T", "M")
    c = calls()
    check("NOTIFY_APP_ICON 给裸路径 → 自动补 file://",
          c and c[0][c[0].index("-appIcon") + 1] == "file:///tmp/x.png",
          str(c[0] if c else None))

    print("\nG2. 勿扰模式：默认尊重系统设置，可开关穿透")
    reset()
    cfg(IGNORE_DND=False)
    notify.send("T", "M")
    check("NOTIFY_IGNORE_DND=0（默认）→ 不带 -ignoreDnD", calls() and "-ignoreDnD" not in calls()[0])
    reset()
    cfg(IGNORE_DND=True)
    notify.send("T", "M")
    check("NOTIFY_IGNORE_DND=1 → 带 -ignoreDnD（穿透专注模式）",
          calls() and "-ignoreDnD" in calls()[0], str(calls()))
    cfg(IGNORE_DND=False)

    print("\nH. 降级：通知出任何问题都不许影响调用方")
    reset()
    cfg(BIN=os.path.join(TMP, "不存在的程序"))
    try:
        r = notify.send("T", "M")
        check("binary 不存在 → 返回 False 且不抛异常", r is False)
    except Exception as exc:  # noqa: BLE001
        check("binary 不存在 → 返回 False 且不抛异常", False, f"{type(exc).__name__}: {exc}")
    cfg(BIN=FAKE_BIN)

    reset()
    os.environ["FAKE_NOTIFY_CODE"] = "1"
    try:
        r = notify.send("T", "M")
        check("binary 返回非 0 → 返回 False 且不抛异常（只记 warning）", r is False)
    except Exception as exc:  # noqa: BLE001
        check("binary 返回非 0 → 返回 False 且不抛异常", False,
              f"{type(exc).__name__}: {exc}")
    finally:
        os.environ["FAKE_NOTIFY_CODE"] = "0"

    print("\nI. 源码契约：接入点没被改掉（防回归）")
    TRIAGE = read("core/triage.py")
    SUMM = read("core/summary.py")
    WEEKLY = read("core/weekly.py")
    NOTIFY = read("core/notify.py")
    APPJS = read("static/app.js")
    check("triage 的 L3 分支调用了 _notify_l3",
          "l3_notified" in TRIAGE and "_notify_l3(e, reason)" in TRIAGE)
    check("_notify_l3 只对 reason=='llm' 发（零成本/手工/fail_open 都不发）",
          'if reason != "llm":' in TRIAGE)
    # ★★ 通知的触发点必须是"生成完成"，不能绑在任何下游动作上。
    #   这是最容易漏的一处：删掉下游模块时如果忘了接生成侧，日报通知会**整体消失**
    #   而单测全绿（没人断言"日报完成后一定发通知"）。
    check("★ 日报生成成功路径发通知（落点本地日报页）",
          "_nfy.daily(day, degraded=not body)" in SUMM)
    check("★ 日报失败路径发 daily_failed（静默失败 = FOMO）",
          "_nfy.daily_failed(day, row.error" in SUMM)
    check("★ 失败通知去噪：首次 或 重试用尽 才出声",
          "terminal or row.attempt <= 1" in SUMM
          and "terminal = row.attempt >= max_attempt" in SUMM)
    check("★ 周报生成成功路径发通知（落点本地周报页）",
          "notify_weekly(week_end, stars=len(stars))" in WEEKLY)
    check("周报成功通知的落点是本地 /summary/weekly/<日期>",
          "/summary/weekly/{week_end.isoformat()}" in WEEKLY)
    check("notify 用 list 传参（源码里没有 shell=True）",
          "shell=True" not in NOTIFY and "subprocess.run(argv" in NOTIFY)
    check("★ notify 同步执行（不做「丢线程不管」：短命进程会把通知丢掉）",
          "threading" not in NOTIFY, "源码里还有 threading")
    check("子进程独立会话（防御调用方异常先退，通知仍能发完）",
          "start_new_session=True" in NOTIFY)
    check("notify 里没有任何 DB 访问（通知不该碰库）",
          "models" not in NOTIFY and "objects." not in NOTIFY)
    check("★ L3 深链带 `&open=`（点通知进去自动展开正文，不用再点一次）",
          "&open={eid}" in NOTIFY and "#row-{eid}" in NOTIFY)
    check("★ app.js 有 openFromUrl（读 URL 的 open 参数 → 点开那条）",
          "function openFromUrl()" in APPJS and 'get("open")' in APPJS)
    check("★ app.js 自动展开走的是 `.entry-head` 的**点击**（复用现成手风琴/标已读）",
          'row.querySelector(".entry-head")' in APPJS and "head.click();" in APPJS)
    check("★ app.js 展开后把该行滚到容器顶部（长正文不会半截在视口外）",
          'scrollIntoView({ block: "start" })' in APPJS)
    # ★ 不写死成一个具体的回调名：?ai=1 深链接入后，这里改成了包一层匿名函数
    #   （先 openFromUrl 再 aiFromUrl），只断言"回调名"会误报成功能坏了
    #   （2026-11-01 实测：这条断言自 3a5da0d 起一直红着，而功能是好的）。
    #   要守的是「**挂在** DOMContentLoaded 上」，且回调体里确实调了它。
    check("★★ app.js 的自动展开挂在 DOMContentLoaded 上（htmx 在它的处理器里才绑 hx-get）",
          'document.addEventListener("DOMContentLoaded", function () {' in APPJS
          and "openFromUrl();" in APPJS)
    check("settings 有 NOTIFY_* 配置块",
          all(f"NOTIFY_{k}" in read("config/settings.py")
              for k in ("ENABLE", "BIN", "SOUND", "TIMEOUT", "L3", "DAILY",
                        "DAILY_FAIL", "FAIL_SOUND", "IGNORE_DND", "APP_ICON",
                        "READER_BASE")))
    check("启动横幅会报告通知可用性（开关开着但没装 binary 是最易静默失效的一种）",
          "系统通知" in read("core/apps.py"))

    print("\nJ. 零真实通知 & 测试不留痕")
    check("全程只用假 binary（真实 terminal-notifier 没被碰过）",
          settings.NOTIFY_BIN == FAKE_BIN, settings.NOTIFY_BIN)
    check("fake log 是可解析 JSONL", all(isinstance(x, list) for x in calls()))
    print("   （真实 binary 路径仅供记录：%s）"
          % (subprocess.run(["bash", "-lc", "command -v terminal-notifier || true"],
                            capture_output=True, text=True).stdout.strip() or "未安装"))


if __name__ == "__main__":
    try:
        main()
    finally:
        for k, v in _SAVED.items():
            setattr(settings, k, v)
        import shutil

        shutil.rmtree(TMP, ignore_errors=True)

    print("\n" + "=" * 68)
    print(f"结果：{ok} 通过 / {bad} 失败 / {skip} 跳过")
    if fails:
        print("失败项：", fails)
    print("=" * 68)
    sys.exit(1 if bad else 0)
