"""按 (source_type, source_id) 取内容块正文 —— **唯一的分发实现**。

谁在消费：
  - `apps/wiki/views._render_body`：给划线/批注做 inline 高亮，需要 HTML；
  - `core/conversation`：对话要喂模型，需要纯文本 + 标题。

★ 为什么必须抽出来（2026-10-31）：这段「按类型取正文」原先长在 views 里，
  只服务划线。对话再写一份，两边一定会漂 —— 典型症状是「页面显示的正文
  和模型看到的正文不是同一篇」（改了 pdf/微博的取数口径只改了一边）。
  这与 `marks.validate_source` 是同一条纪律：**判据/取数只有一份**。

★ 只做取数与渲染，不碰 DB、不碰请求；取不到返回 None，降级交给调用方。
  （页面侧降级成「只刷新标记列表」；对话侧降级成「明确说读不到正文」。）
"""
import logging
from datetime import date as _date, datetime as _datetime, time as _time
from functools import lru_cache

from django.utils import timezone

from core import mdrender
from core.textutil import to_plain

log = logging.getLogger(__name__)

def _aware(dt):
    """datetime → aware datetime（USE_TZ=True 下 DateTimeField 落库必须 aware）。"""
    if dt is None:
        return None
    return dt if timezone.is_aware(dt) else timezone.make_aware(dt)


def _source_published_at(source_type: str, source_id: str, entry=None):
    """内容的**发布时间** —— 取数只此一份，别在调用方各写一套。

    ★ 这不是「我划线/说话的时间」（`my_words` items 的 `at`），也不是系统提炼
      时间（`IngestCandidate.created_at`），更不是抓取入库时间（`Entry.created_at`）。
      那三个都跟「这条内容什么时候发的」无关，混进来会让 decay 排序整体错位 ——
      `datasource/base.py` 已有明文口径：统计与总结一律用 `published_at`。
    ★ 拿不到就返回 None，**绝不降级**、**绝不编造**（不拿今天补）。

    - `entry` → `entry.published_at`（可为 None）；
    - `report` → `daily-YYYY-MM-DD` / `weekly-YYYY-MM-DD` 里的那天（零点 aware）；
    - 其他 / 解析失败 → None。
    """
    if source_type == "entry":
        return _aware(getattr(entry, "published_at", None))
    if source_type == "report":
        low = (source_id or "").lower()
        for prefix in ("daily-", "weekly-"):
            if low.startswith(prefix):
                try:
                    d = _date.fromisoformat(low[len(prefix):])
                except ValueError:
                    return None
                return _aware(_datetime.combine(d, _time.min))
    return None


def load(source_type: str, source_id: str):
    """返回 {"title", "text", "html", "feed_title", "published_at"}，取不到返回 None。

    - `text`：纯文本 / markdown 原文（喂模型用，保留段落）
    - `html`：可直接展示与做高亮的 HTML（entry 是 Miniflux 原始 HTML，
      report 是 markdown 渲染结果）；**未加** marks 高亮，那是调用方后处理。
    - `feed_title`：这条内容**是谁发出来的**（订阅源名）。entry 取 Miniflux 的
      feed 标题，report 为空串。★ 加这个字段的原因：提炼出来的 claim 会带主体名
      （「某作者表示…」），可材料里根本没有作者信息 —— 模型是靠先验知识把网名
      认成真人写上去的。猜对了是运气，换一个转载号/聚合号就是张冠李戴，而引用
      守卫只管 quote 逐字对不上，管不到主语。有了这一行，主体名至少**有据可依**。
    - `published_at`：**内容的发布时间**（不是我划线/说话的时间，也不是抓取入库
      时间），取数只在 `_source_published_at()` 一份。候选带它落库，是 decay
      （衰减）的根基；拿不到为 None，不降级、不编造。见该函数 docstring。
    """
    try:
        if source_type == "entry":
            from core.datasource import get_datasource
            entry = get_datasource().get_entry(int(source_id))
            if entry is None:
                return None
            return {"title": entry.title or "",
                    "text": to_plain(entry.content or ""),
                    "html": entry.content or "",
                    "feed_title": (getattr(entry, "feed_title", "") or "").strip(),
                    "published_at": _source_published_at("entry", source_id, entry=entry)}

        if source_type == "report":
            sid = (source_id or "").lower()
            if sid.startswith("daily-"):
                from core import summary
                got = summary.read_report(_date.fromisoformat(sid[len("daily-"):]))
                if got is None:
                    return None
                return {"title": "日报 %s" % sid[len("daily-"):],
                        "text": got["text"], "html": mdrender.render(got["text"]),
                        "feed_title": "",
                        "published_at": _source_published_at("report", sid)}
            if sid.startswith("weekly-"):
                from core import weekly as wk
                text = wk.read_weekly(_date.fromisoformat(sid[len("weekly-"):]))
                if text is None:
                    return None
                return {"title": "周报 %s" % sid[len("weekly-"):],
                        "text": text, "html": mdrender.render(text),
                        "feed_title": "",
                        "published_at": _source_published_at("report", sid)}
    except Exception as exc:  # noqa: BLE001  取不到正文不该拖垮整个页面/对话
        log.warning("取正文失败 %s/%s: %s", source_type, source_id, exc)
    return None


def source_note(blk) -> str:
    """材料抬头那行「来源：…」；没有订阅源时返回空串（不输出空标题）。

    ★ 只此一份（2026-11-03）：wiki 提炼（`ingest._material`）和阅读时对话
      （`conversation._material`）都要拼材料，两处各写一遍必然漂 —— 这个项目
      已经为「同一个判据抄两份」付过两次学费（引用边界、作者归属）。
    ★ 括号里那句是**给模型看的定位说明**，不是给人看的装饰：不写清「正文由
      订阅源发布」，模型容易把 feed 名当成「频道/栏目」，转头继续凭记忆补人名。
    """
    src = ((blk or {}).get("feed_title") or "").strip()
    return f"来源：{src}（订阅源，正文由它发布）" if src else ""


# ------------------------------------------------------------------ 来源跳转链接
@lru_cache(maxsize=256)
def _entry_feed_id(entry_id: int):
    """entry → 它所属的 feed_id（深链定位树要用）。取不到返回 None。

    ★ 缓存：`source_url` 会在收件箱/候选列表里**每行**都调一次，每行一次 Miniflux
      查询等于「开一次 wiki 页打 N 次外部接口」。feed 归属极少变，缓存错了也只是
      跳错源（仍能通过 open= 打开那条），刷新即恢复 —— 便宜且错得轻。
    """
    try:
        from core.datasource import get_datasource
        entry = get_datasource().get_entry(entry_id)
        return getattr(entry, "feed_id", None) if entry is not None else None
    except Exception:  # noqa: BLE001
        return None


def source_url(source_type: str, source_id: str, ai: bool = False,
               mark_id=None, msg_id=None, src=None):
    """某来源在阅读器里的**可点击跳转链接**。返回 URL 或 None（跳不过去）。

    ★ 与 `load` 同一条纪律：来源跳转的推导**只有这一份**。收件箱、候选卡、
      已入库页反查都要跳回原文，各写一份 URL 拼法必然漂 —— 典型症状是
      日报能点开、文章点了没反应（两处对 source_type 的分发不一致）。

    `ai=True`：跳过去落地就自动弹对话浮窗（前端读 `?ai=1`）。
    `mark_id`：落地后滚到并高亮那一条划线/批注（前端读 `?mk=`）。
    `msg_id`：对话浮窗里滚到那条消息（前端读 `?msg=`）。
    `src`：utm_source，点击落在 `core/clicktrack`（报告里的痕迹链接要带
      `weekly`，否则「周报点出去多少次」统计不到）。

    ★★ `ai` / `msg` / `mk` / `src` 都**必须插在锚点 `#row-*` 之前** ——
      拼在 `#` 后面就成了锚点的一部分，前端 `URLSearchParams` 读不到
      （这是最容易踩的 URL 拼接坑，2026-11-01 加 `mk`/`msg` 时又碰了一次）。
      所以统一走 `_final()` 一次性拼，别在各分支里各自 `+ "&mk="`。

    映射：
      entry            → /?sel=feed:<feed_id>&open=<id>#row-<id>（深链自动展开）
      report daily     → /summary/<date>
      report weekly    → /summary/weekly/<week_end>
      chat             → 反查这条消息所属的 conversation，跳到那个来源
                         （并带上 `msg=<消息 id>` 精确到那条消息）
      web / 其他       → None（现在还没有网页采集）
    """
    st = (source_type or "").strip()
    sid = (source_id or "").strip()
    if not st or not sid:
        return None

    def _final(url: str) -> str:
        # ★ 顺序固定 ai → msg → mk → src：只影响可读性，但**都在 fragment 之前**。
        extra = []
        if ai:
            extra.append("ai=1")
        if msg_id:
            extra.append("msg=%s" % msg_id)
        if mark_id:
            extra.append("mk=%s" % mark_id)
        if src:
            extra.append("src=%s" % src)
        if not extra:
            return url
        frag = ""
        if "#" in url:
            url, frag = url.split("#", 1)
            frag = "#" + frag
        return url + ("&" if "?" in url else "?") + "&".join(extra) + frag

    try:
        if st == "entry":
            if not sid.isdigit():
                return None
            fid = _entry_feed_id(int(sid))
            if fid is None:
                return None
            return _final("/?sel=feed:%s&open=%s#row-%s" % (fid, sid, sid))

        if st == "report":
            low = sid.lower()
            if low.startswith("daily-"):
                return _final("/summary/%s" % low[len("daily-"):])
            if low.startswith("weekly-"):
                return _final("/summary/weekly/%s" % low[len("weekly-"):])
            return None

        if st == "chat":
            # ★ 反查消息所属会话：chat 的 source_id 是消息 id，不是内容 id。
            #   跳回「这条消息所在的那篇内容」，对话浮窗才挂得上。
            # ★ 再带 `msg=<消息 id>`：光 `ai=1` 只能弹出浮窗，落不到**哪一条**消息上。
            #   周报里的对话划线圈要「可追溯到那条话」，靠的就是这个参数。
            #   只在**要定位**时才带（给了 mark_id 或 msg_id）—— 收件箱那种
            #   「跳回原文」的普通链接不该把用户拽到某条消息上。
            if not sid.isdigit():
                return None
            from core.models import ChatMessage
            msg = ChatMessage.objects.filter(pk=int(sid)).select_related(
                "conversation").first()
            if msg is None or msg.conversation is None:
                return None
            # 会话只可能挂在 entry/report 上；真挂了 chat（坏数据）就别再递归，
            # 否则 source_url → chat → source_url 无限循环。
            if (msg.conversation.source_type or "").strip() == "chat":
                return None
            want_msg = msg_id or (sid if mark_id else None)
            return source_url(msg.conversation.source_type,
                              msg.conversation.source_id, ai=ai,
                              mark_id=mark_id, msg_id=want_msg, src=src)
    except Exception as exc:  # noqa: BLE001  导航层降级，别让一条读不到炸掉整页
        log.debug("取来源链接失败 %s/%s: %s", st, sid, exc)
    return None
