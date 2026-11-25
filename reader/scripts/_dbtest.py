"""把验证脚本整体切到 Django 测试库（sqlite 内存），真实库一行不碰。

★ 为什么必须有这个文件（2026-10-31 事故的教训）：
   验证脚本要「真落库」才测得准（create / delete / 改状态），但直接在真实
   `data/reader.sqlite3` 上做这些动作，等于**每跑一次验证就改一次用户的数据**。
   那天 verify_wiki 的 `Highlight.objects.all().delete()` 把 96 条划线和 28 条批注
   清空，且 `.recover` 从 freelist 一行都捞不回来 —— 代价是真实的、不可逆的。

   而这件事最难被发现的地方在于：脚本「全绿」，用户却觉得"我的划线又没了"。
   静默的无损失败比报错贵，所以隔离要写在脚本入口、而不是靠人记得别跑。

★ 用法（必须**在 `django.setup()` 之后、`from core.models import ...` 之前**）：

       import _dbtest          # scripts/ 已在 sys.path
       _dbtest.isolate()

   之后脚本里所有 ORM 操作都落在内存测试库上，进程退出时自动拆掉。
   判据不是"我改成了 test db"，而是：跑之前在真实库插一条哨兵，跑完它还在。

★ 为什么不是每个脚本各写一遍这五行：
   同一段纪律复制 5 份，迟早有一份被改漏或改错 —— 那就又变成
   "有的脚本隔离了、有的没有"，而失败仍然是静默的。单一来源。

★ 顺带静默系统通知（2026-11-05）：`verify_weekly.py` 的「缺历史日报」分支
   会真调 `weekly._fail()` → terminal-notifier，每跑一次回归就在桌面弹一条
   「谛听 · 周报未生成」。那是**验证脚本造出来的失败**，不是真的周报没生成。
   隔离不该只管数据库：外部副作用（弹窗）同样属于"验证不许碰真实世界"。
"""

import atexit
import os
import shutil
import tempfile

_OLD_CONFIG = None

#: 文件写入点的临时根目录（`_isolate_files` 建，退出时删）。
_FILE_TMP = None

#: 必须切走的目录 —— **按写入点枚举，不按变量名猜**（2026-11-05 事故）：
#:   · REPORT_DIR：日报/周报正文（reportstore / weekly 写）
#:   · WIKI_DIR：wiki 页 / 草稿 / index / log（core/wiki.py 写）
#:   · SUMMARY_CACHE_DIR：S3/S2/S1 摘要缓存（core/summarycache.py 写）
#: ★ 这三个都被写，但分散在三个不同模块；只切 DB 时它们全部指向真实 data/。
#:   代价：2026-11-05 一次隔离 server 的补跑把 4 份真实日报换成了规则版。
#:   「不碰数据库」≠「不碰真实世界」—— 文件、通知、进程都算真实世界。
_FILE_DIRS = ("REPORT_DIR", "WIKI_DIR", "SUMMARY_CACHE_DIR")

#: 被拦下的系统通知。只记不发，供验证脚本做「桩真被用上」的反证。
MUTED_NOTIFICATIONS = []


def mute_notifications():
    """验证进程里拦掉真实系统通知：只记录、不弹窗。幂等。

    ★ 只作用于**测试/验证进程**。生产（runserver / cron / CLI）的失败通知是
      FOMO 防线（见 `core/notify.py` 的 `daily_failed`），一行都不动 ——
      真没生成周报/日报时，人必须被主动告知，静默失败最贵。
    ★ 拦的是「起 terminal-notifier 进程」这一步，不是「判失败」：
      `weekly._fail()` 照常写 SummaryRun、照常置 `notified`，验证脚本对业务
      行为的断言原样保留，只是不再往桌面上弹东西。
    ★ 打桩打在 `core.notify.send`（所有出口的唯一收口）：daily / l3 / weekly
      的业务入口都调它，patch 一处即覆盖全集，不用逐条追调用点。
    ★ 「桩真被用上」的反证：调用记进 `MUTED_NOTIFICATIONS`，`verify_weekly.py`
      断言里面确实多了「周报未生成」。否则"没弹窗"也可能是路径压根没走到，
      那种绿是假的。
    """
    from core import notify

    if getattr(notify.send, "_diting_muted", False):
        return
    real_send = notify.send

    def _muted(title, message, **kw):
        MUTED_NOTIFICATIONS.append((title, message))
        return True          # 语义同「已发出」，别让调用方因 False 走岔路

    _muted._diting_muted = True
    _muted._diting_real = real_send
    notify.send = _muted
    atexit.register(_restore_notifications)


def _restore_notifications():
    """把真 send 还回去（退出时清理，也供守卫测试显式复原）。"""
    from core import notify

    if getattr(notify.send, "_diting_muted", False):
        notify.send = notify.send._diting_real


def isolate_files():
    """把 `_FILE_DIRS` 全部切到同一个临时根下（幂等）。

    每个目录先建好（`reportstore` / `wiki.ensure_dirs` 都假定父目录存在也无妨，
    但提前建能更早暴露"写到了临时目录之外"这类错）。

    ★ 为什么单独一个函数：守卫测试要能直接验它（不必建测试库），
      而 `isolate()` 里调它只占一行 —— 少一处"人可以忘"的地方。
    """
    global _FILE_TMP
    if _FILE_TMP is not None:
        return _FILE_TMP

    from django.conf import settings

    _FILE_TMP = tempfile.mkdtemp(prefix="diting-verify-")
    for name in _FILE_DIRS:
        path = os.path.join(_FILE_TMP, name.lower())
        os.makedirs(path, exist_ok=True)
        setattr(settings, name, path)
    atexit.register(_cleanup_files)
    return _FILE_TMP


def _cleanup_files():
    global _FILE_TMP
    if _FILE_TMP and os.path.isdir(_FILE_TMP):
        shutil.rmtree(_FILE_TMP, ignore_errors=True)
    _FILE_TMP = None


def isolate(verbosity=0):
    """建测试库并接管本次进程的所有 ORM 操作。重复调用是安全的（幂等）。

    ★ 2026-11-05 起**同时切走文件写入点**（见 `_FILE_DIRS`）：验证脚本里
      一旦有人调 `summary.run()` / `wk.run()`，报告只会写进临时目录。
      只切库不切文件，是这个坑第二次出现时才补上的 —— 第一次是 2026-10-31
      `IngestCsrfTest` 漏了 `WIKI_DIR`（真实 `ingest.json` 被写入）。
    """
    global _OLD_CONFIG
    mute_notifications()     # ★ 先静默再建库：任何验证脚本都不会弹真通知
    isolate_files()          # ★ 再切文件：报告/wiki/缓存都不许落真实 data/
    if _OLD_CONFIG is not None:
        return _OLD_CONFIG

    from django.test.utils import (setup_databases, setup_test_environment,
                                   teardown_databases)

    setup_test_environment()
    _OLD_CONFIG = setup_databases(verbosity=verbosity, interactive=False)
    atexit.register(_teardown)
    return _OLD_CONFIG


def _teardown():
    from django.test.utils import teardown_databases

    try:
        teardown_databases(_OLD_CONFIG, verbosity=0)
    except Exception:      # noqa: BLE001  退出阶段的清理失败不该污染脚本的结论
        pass
