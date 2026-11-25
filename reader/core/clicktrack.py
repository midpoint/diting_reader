"""点击来源追踪（utm_source 机制的「记录端」）。

为什么是 JSONL 而不是建表
--------------------------
  · 量极小：只有人手点深链才会产生一条（日报 / L3 通知），一天几十条封顶；
  · 结构还会变：打开率怎么用（周报口径、多少算"过低"）还没定，先不承诺 schema；
  · 零迁移：建表要走 makemigrations，为一个还没定口径的指标不值当。
  现在周报已经用 group_counts() 做「按来源分类汇总」，仍然只是读一遍 JSONL，
  没到需要建表的量级；等真要按天/按条目下钻了再迁 sqlite 也不迟（load()
  已经把「按 src / 按天过滤」封装好了，到时候只换存储不动调用方）。

链接端是谁
----------
  深链统一长这样（复用 L3 通知的 reader_entry_url，见 core/notify.py）：
      {NOTIFY_READER_BASE}/?sel=feed:<fid>&open=<eid>&src=<来源>#row-<eid>
  记录发生在 apps/reader/views.py 的 index（日报内引用、L3 通知点击都落到这）
  与 apps/summary/views.py 的 summary 页（日报通知打开的是 /summary/<day>）。

★ 隐私边界：只记 src / entry_id / feed_id / 时间，不记 UA、不记 IP——
  这是单机自用工具，没有"用户"可言，但开源后别人拿去用也不该顺手记敏感的。
"""
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta, timezone

from django.conf import settings

log = logging.getLogger(__name__)

_PATH = None          # 延迟定位（测试可覆盖）
_LOCK = threading.Lock()

# src 白名单字符：来源名是我们自己写进链接的，防的是有人手改 URL 往文件里塞垃圾
_SAFE_SRC = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

# 已知来源（不是硬限制，是文档：新来源在这里登记一行）
KNOWN_SOURCES = {
    "daily": "日报正文里的引用（无法再区分是页面还是通知点进来的）",
    "daily-notify": "macOS 通知「日报已生成」点击打开日报页",
    "daily-fail-notify": "macOS 通知「日报生成失败」点击打开总结页",
    "l3": "L3 点名通知点击打开某一条",
    "weekly": "周报正文里的引用（从日报搬来后统一改写为此来源）",
    "weekly-notify": "macOS 通知「周报已生成 / 生成失败」点击打开",
    "wiki": "wiki 页正文里的引用（编译成页后指向原始条目，见 core/ingest.py）",
}

# 周报「按分类汇总」的口径：把细粒度 src 并成读者看得懂的大类。
# ★ 与 KNOWN_SOURCES 分开维护：那个是「有哪些来源」的登记表，这个是
#   「报告里怎么并组」的口径；链接端新增 src 不会自动改报告里的分组。
# ★ 实测 clicks.jsonl 里出现过当时未登记的 src=wiki —— 登记表不是全集，
#   group_counts() 必须兜住未知来源，绝不能让分项之和小于总数。
SOURCE_GROUP = {
    "daily": "日报引用",
    "daily-notify": "通知",
    "daily-fail-notify": "通知",
    "weekly-notify": "通知",
    "l3": "L3 通知",
    "weekly": "周报引用",
    "wiki": "wiki 页引用",
}


def path() -> str:
    global _PATH
    if _PATH is None:
        _PATH = os.path.join(str(settings.DATA_DIR), "clicks.jsonl")
    return _PATH


def set_path(p: str) -> None:
    """测试注入口。"""
    global _PATH
    _PATH = p


def record(src: str, entry_id: int = 0, feed_id: int = 0) -> bool:
    """记一次点击。写失败绝不影响页面打开（这是它的底线）。"""
    src = (src or "").strip()
    if not _SAFE_SRC.match(src):
        return False
    line = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "src": src,
        "entry_id": int(entry_id or 0),
        "feed_id": int(feed_id or 0),
    }, ensure_ascii=False)
    try:
        with _LOCK:
            with open(path(), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("点击记录写入失败（不影响页面）：%s", exc)
        return False


def load(days: int = 0, src: str = "") -> list:
    """读点击记录（新→旧）。days=N 只看最近 N 天；src 过滤来源。"""
    try:
        with open(path(), encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001
        log.warning("点击记录读取失败：%s", exc)
        return []
    rows = []
    for x in lines:
        if not x.strip():
            continue
        try:
            rows.append(json.loads(x))
        except Exception:  # noqa: BLE001
            # ★ 逐行容错：append-only 文件可能被截断出半行 —— 一行坏不能把
            #   整份记录作废（否则一次崩溃就"清空"全部历史点击）。
            continue
    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        rows = [r for r in rows if _ts(r) >= cutoff]
    if src:
        rows = [r for r in rows if r.get("src") == src]
    rows.sort(key=_ts, reverse=True)
    return rows


def group_counts(rows: list) -> list:
    """按大类汇总点击，返回 [(分类名, 次数), ...]（次数降序、同数按名字稳定）。

    给周报「本周概览」用：原来只给一个总数 +「含日报/通知/L3」的含糊括注，
    现在把每个来源真正点了几下摊开。rows 是**已经过滤好窗口**的记录，
    所以分项与总数永远同源、同一口径。

    ★ 不按 KNOWN_SOURCES 白名单过滤：链接端随时可能带新 src（实测就有
      src=wiki）。未知来源归到「其他（<src>）」并原样显示，分项之和恒等于
      len(rows) —— 汇总数字对不上比不汇总更伤信任。
    """
    counts = {}
    for r in rows:
        src = (r.get("src") or "").strip() or "(空)"
        label = SOURCE_GROUP.get(src, f"其他（{src}）")
        counts[label] = counts.get(label, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _ts(r: dict) -> datetime:
    try:
        return datetime.fromisoformat(r.get("ts", ""))
    except Exception:  # noqa: BLE001
        return datetime.min.replace(tzinfo=timezone.utc)
