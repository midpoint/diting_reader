"""macOS 系统通知 —— 经 terminal-notifier 发出（L3 点名 / 日报完成 / 日报失败）。

为什么不是 osascript
--------------------
`osascript -e 'display notification …'` **不支持点击动作**，通知点不了、只能看见。
terminal-notifier 的 `-open URL` 能在**点击通知时打开一个链接** —— 这正是我们要的：

    L3   → 阅读器 /?sel=feed:<源>&open=<条目>#row-<条目>
           （树定位 + 滚到那一行 + **自动展开正文** —— 点名点进来就是要读）
    日报 → 本地 /summary/<日期>（日报已生成）
    周报 → 本地 /summary/weekly/<日期>
    失败 → 本地 /summary（总结页那一行有状态、错误原因和「重跑」按钮）

三条硬规则（每一条都是"不做就会咬人"）
--------------------------------------
1. **同步发，不丢**。见下面 `send()` 的说明 —— 实测"丢线程不管"会让短命进程
   （CLI 脚本 / `manage.py` 一次性命令）静默丢通知。
2. **绝不抛异常**。通知是"锦上添花"，失败绝不能影响分级与推送 —— 全部兜住只记日志。
3. **参数用 list 传**（`shell=False`）。标题里出现 `"`、`$`、反引号、换行也不会被解释。

去重
----
`-group` 用 `diting-l3-<entry_id>` / `diting-daily-<date>` / `diting-daily-fail-<date>`：
同一条重复通知会**替换**系统里那条旧的，不会在通知中心堆成一摞。
★ 失败用**单独的 group**（不跟成功那条共用）—— 否则"推成功"会把"推失败"顶掉，
  反过来也一样，两条本来不同的消息不该互相覆盖。
"""
import logging
import os
import shutil
import subprocess
import sys

from django.conf import settings

from core import settingkit as sk
from core.logkit import fields

log = logging.getLogger(__name__)

#: 找不到可执行文件时只警告一次（每 5 分钟一轮分级，不能变成刷屏机）
_warned_missing = False


def _backend() -> str:
    """通知后端：``mac``（terminal-notifier，默认）或 ``win``（Python shim → winotify）。

    ``NOTIFY_BACKEND=auto`` 时按平台选（win32 → win，其余 → mac）。显式指定可覆盖。
    """
    val = (getattr(settings, "NOTIFY_BACKEND", "auto") or "auto").strip().lower()
    if val == "auto":
        return "win" if sys.platform == "win32" else "mac"
    return val if val in ("win", "mac") else "mac"


def _shim() -> str:
    """Windows shim 脚本路径（默认随包内的 core/notify_win.py）。"""
    return (getattr(settings, "NOTIFY_SHIM", "") or "").strip() or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "notify_win.py")


def _bin() -> str:
    return getattr(settings, "NOTIFY_BIN", "terminal-notifier") or "terminal-notifier"


def _launcher() -> list:
    """返回子进程 argv 的前缀。mac 是 [terminal-notifier]；win 是 [python, shim]。

    ★ win 必须用 **当前 venv 的 sys.executable** 起 shim —— 而不是 .cmd 包装：
      CreateProcess 不能直接启动 .bat/.cmd，且系统隐式走 shell 会重新解释参数，
      破坏"list 传参、标题里的引号/$/换行不被解释"这条底线。用 python + .py 则
      全程 list 传参，且 import 得到同一个 venv 里 pip 装的 winotify。

    ★ 同步不变：subprocess.run 等的是 shim 进程（几百毫秒），shim 内部再同步
      调 winotify。winotify 内部 Popen 起 PowerShell 的异步限制见 notify_win.py。
    """
    if _backend() == "win":
        return [sys.executable, _shim()]
    return [_bin()]


def _icon() -> str:
    """通知图标：默认用 static 里的白犬（与 favicon 同一份图形的 256px 位图）。"""
    val = (getattr(settings, "NOTIFY_APP_ICON", "") or "").strip()
    if val:
        return val if "://" in val else "file://" + os.path.abspath(os.path.expanduser(val))
    png = os.path.join(settings.BASE_DIR, "static", "favicon-256.png")
    return "file://" + png if os.path.exists(png) else ""


def _ready() -> bool:
    """后端所需的程序/文件是否就位（不含总开关）。"""
    if _backend() == "win":
        # shim 随包分发，几乎一定在；但 winotify 是否 pip 装了这里看不出来
        #（它在子进程里 import），失败由 shim 的退出码 2 → _run 记 warning 告知。
        return os.path.exists(_shim())
    return bool(shutil.which(_bin()) or os.path.exists(_bin()))


def enabled() -> bool:
    """总开关 + 后端程序都在，才认为通知可用。"""
    if not sk.effective("notify_enable"):
        return False
    return _ready()


def send(title: str, message: str, *, subtitle: str = "", url: str = "",
         group: str = "", sound=None, icon=None) -> bool:
    """发一条通知。**同步返回**（实测一次 0.24–0.38s）。

    ★ 为什么不是"丢线程立刻返回"：实测踩到 —— daemon 线程 + 调用方进程稍后退出
      （`scripts/run_weekly.py` 这类短命命令）时，线程**还没执行到起进程**就被
      杀掉了，通知静默丢失（`start_new_session` 也救不了，因为进程压根没 fork 出来）。
      通知本来就低频（L3 一天几条 + 日报一条），同步几百毫秒换"一条都不会丢"，
      这笔账划算。嫌慢就调小 `NOTIFY_TIMEOUT`（它只是病态情况的上限）。

    返回 True 表示"已发出命令"（退出码 0），不表示系统一定显示了 ——
    通知权限/勿扰设置不在我们手里（win 后端的 winotify 还多一层异步 Popen，
    连"已发出"都无法从退出码自证）。排查看 `/logs`。
    """
    global _warned_missing
    if not sk.effective("notify_enable"):
        return False
    if not _ready():
        if not _warned_missing:
            _warned_missing = True
            target = _shim() if _backend() == "win" else _bin()
            log.warning("找不到通知程序 %r，本次及后续通知都跳过"
                        "（mac 改 NOTIFY_BIN；win 检查 NOTIFY_SHIM）",
                        target, extra=fields(src="notify"))
        return False

    argv = _launcher() + ["-title", title, "-message", message]
    if subtitle:
        argv += ["-subtitle", subtitle]
    if url:
        argv += ["-open", url]
    if group:
        argv += ["-group", group]
    snd = sk.effective("notify_sound") if sound is None else sound
    if snd:
        argv += ["-sound", snd]
    ic = _icon() if icon is None else icon
    if ic:
        argv += ["-appIcon", ic]
    # ★ 勿扰/专注模式会**吞掉**通知。默认尊重系统设置（不想半夜被日报吵醒）；
    #   但 L3 是"点名"，有人要它穿透专注模式 —— 给个开关。
    if sk.effective("notify_ignore_dnd"):
        argv += ["-ignoreDnD"]

    return _run(argv)


def _run(argv) -> bool:
    """真正起进程。任何异常都只记日志、返回 False（绝不让调用方炸）。"""
    try:
        p = subprocess.run(argv, capture_output=True, start_new_session=True,
                           timeout=getattr(settings, "NOTIFY_TIMEOUT", 10))
    except subprocess.TimeoutExpired:
        log.warning("通知超时（%ss）未返回，放弃：%s",
                    getattr(settings, "NOTIFY_TIMEOUT", 10), argv[1:3],
                    extra=fields(src="notify"))
        return False
    except Exception as exc:  # noqa: BLE001  通知失败绝不影响主流程
        log.warning("通知发送失败：%s", exc, extra=fields(src="notify"))
        return False

    if p.returncode != 0:
        log.warning("通知返回码 %s：%s", p.returncode,
                    (p.stderr or b"").decode("utf-8", "replace")[:200],
                    extra=fields(src="notify"))
        return False
    log.debug("已发通知：%s", argv[1:3], extra=fields(src="notify"))
    return True


# ------------------------------------------------------------------ 三个业务入口
def reader_entry_url(feed_id: int, entry_id: int, src: str = "") -> str:
    """阅读器里**定位到某一条**的深链 —— 定位 + 滚动 + **自动展开正文**。

    三段各管一件事：
      - `?sel=feed:<源>` —— 树定位到该源（列表只渲染这个源的条目）
      - `&open=<条目>`   —— `static/app.js` 的 `openFromUrl()` 会把这一条的正文
                            拉出来（等价于点一下它的标题）。L3 是"点名"，
                            点进来几乎一定要读，所以省掉再点一次。
      - `#row-<条目>`    —— 原生锚点滚到那一行。列表是**服务端渲染**的（不是 HTMX
                            懒加载），每行本来就有 `id="row-<id>"`，不需要 JS 就生效。
                            这也让"`open` 那套 JS 万一没跑起来"时链接仍然可用。
      - `&src=<来源>`    —— utm_source：点击由 index 视图记入 `core/clicktrack`，
                            区分 L3 通知 / 日报引用等来源（打开率监控用）。

    ★ 展开复用 htmx 现成的 `/hx/entry/<id>` 通道（手风琴、展开即标已读都在那条链上），
      不另起一条"只加载不标已读"的路 —— 否则同一个动作会有两种行为。
    ★ 日报内引用的同一格式住在 `reporttext.deep_link`（那边走 settings 直读），
      两处格式必须保持一致 —— 同一个动作不许有两种 URL 方言。
    """
    base = (getattr(settings, "NOTIFY_READER_BASE", "") or "").rstrip("/")
    if not base:
        return ""
    eid = int(entry_id)
    src_q = f"&src={src}" if src else ""
    return f"{base}/?sel=feed:{int(feed_id)}&open={eid}{src_q}#row-{eid}"


def l3(entry, *, feed_title: str = "") -> bool:
    """L3 点名通知。`entry` 只要有 id / feed_id / title 三个属性即可。"""
    if not sk.effective("notify_l3"):
        return False
    feed = feed_title or getattr(entry, "feed_title", "") or f"feed:{entry.feed_id}"
    title = (getattr(entry, "title", "") or "（无标题）").strip()
    url = reader_entry_url(entry.feed_id, entry.id, src="l3")
    return send("谛听 · L3 点名", title, subtitle=feed, url=url,
                group=f"diting-l3-{entry.id}")


def daily(day, *, degraded: bool = False) -> bool:
    """日报**生成完成**通知。点击打开本地日报页。

    ★ 它说的是"今天的日报好了"，跟任何第三方页面无关。
    ★ `degraded`：LLM 没产出、落成规则版时要在通知里说出来，否则读者点开才发现
      内容只是一张条目清单。
    """
    if not sk.effective("notify_daily"):
        return False
    base = (getattr(settings, "NOTIFY_READER_BASE", "") or "").rstrip("/")
    # 本地落点带 src=daily-notify（区别于日报正文里的引用 src=daily）
    url = f"{base}/summary/{day}?src=daily-notify" if base else ""
    # 文案原则：**陈述事实，不做教学**。日期在标题里；"点击打开"是废话——通知本身就可点。
    return send(f"谛听日报 · {day}",
                "已生成（规则版，LLM 未产出）" if degraded else "已生成",
                url=url, group=f"diting-daily-{day}")


def daily_failed(day, error: str = "", *, attempts: int = 0, max_attempts: int = 0,
                 terminal: bool = False) -> bool:
    """日报**生成失败**通知 —— 静默失败就是 FOMO 的源头。

    为什么必须有这条
    ----------------
    成功有通知、失败没有 ⇒ **唯一会主动找你的消息是好消息**。日报没生成是
    "静默的"（当天什么都没发生，没人提醒你），等你自己发现时可能已经过去几天。
    所以失败也要出声 —— 而且**比成功更该出声**。

    去噪（由调用方 `summary.run` 决定发不发，这里只管"怎么发"）
    ----------------------------------------------------------
    同一个失败原因在自动重试里会反复出现，那属于"已知问题、正在处理"，没有信息增量；
    调用方只在 ① 首次失败 ② 重试已用尽 时才调到这里。

    点击 → 本地**总结页** `/summary`（那一行有状态、错误原因和「重跑」按钮）。
    失败时日报页还没有正文，给个必然打得开的落点更重要。
    """
    if not sk.effective("notify_daily_fail"):
        return False

    reason = " ".join((error or "").split()) or "未知错误"
    if len(reason) > 160:
        reason = reason[:157] + "…"

    if terminal:
        sub = "重试已用尽 · 需人工处理"
    elif attempts and max_attempts:
        sub = f"第 {attempts}/{max_attempts} 次 · 稍后自动重试"
    else:
        sub = "稍后自动重试"

    base = (getattr(settings, "NOTIFY_READER_BASE", "") or "").rstrip("/")
    url = f"{base}/summary?src=daily-fail-notify" if base else ""
    # ★ 失败用**另一个提示音**（默认 Basso）：「响一声但不用看」和「响一声得爬起来看」
    #   听感上就该不一样。三态语义（与 send() 的约定）：
    #     "Basso" 等  → 用指定音；""（显式留空）→ 失败静音；未定义 → None = 跟随通用音。
    #   ★ 不能写 `sk.effective(...) or None` —— or 会把显式空串也吞成 None，
    #     "留空 = 静音"就静悄悄变成"留空 = 响通用音"（回归脚本抓到过）。
    snd = sk.effective("notify_fail_sound")
    return send(f"谛听日报 · {day} 生成失败", reason, subtitle=sub, url=url,
                group=f"diting-daily-fail-{day}",
                sound=(snd if isinstance(snd, str) else None))
