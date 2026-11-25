"""Miniflux REST 数据源实现。

只暴露阅读器语义（见 base.py），Miniflux 特有概念在这里消化掉：
  parsing_error_count / parsing_error_message -> health() -> {ok, last_success, error_msg}
"""
import base64
import logging
import time
from datetime import datetime, timedelta, timezone as dt_timezone

import requests
from django.conf import settings
from django.core.cache import cache

from core.last_success import last_success
from core.logkit import fields
from .base import CategoryNode, DataSource, Entry, FeedNode

log = logging.getLogger(__name__)

CACHE_TTL = 20

# 多 feed 归并时**每个源**最多取多少条（见 list_entries_multi 的口径说明）
MULTI_PER_SOURCE = int(getattr(settings, "MULTI_FEED_PER_SOURCE", 60) or 60)

# 排序兜底用的"最早时间"（带时区，才能与 Miniflux 返回的 aware datetime 比大小）
_EPOCH = datetime(1970, 1, 1, tzinfo=dt_timezone.utc)


def _dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:
        return None


def _ts(value):
    """→ Unix 秒。

    ★ 实测（Miniflux 2.3.3）：`published_after`/`published_before` **只认 Unix 时间戳**。
      传 RFC3339（`2026-10-14T12:00:00Z` 或带时区偏移）会被**静默忽略** ——
      不报错、直接返回全量（实测 total 2643 vs 65），是最容易踩的静默失效。
    """
    if isinstance(value, (int, float)):
        return int(value)
    dt = _dt(value) if not hasattr(value, "timestamp") else value
    if dt is None:
        return int(value)
    return int(dt.timestamp())


class MinifluxDataSource(DataSource):
    def __init__(self, base_url=None, username=None, password=None):
        self.base_url = (base_url or settings.MINIFLUX_BASE_URL).rstrip("/")
        self.session = requests.Session()
        # macOS 系统代理会把 localhost 请求也送上游（风险 #3）
        self.session.trust_env = False
        self.session.auth = (
            username or settings.MINIFLUX_USERNAME,
            password or settings.MINIFLUX_PASSWORD,
        )
        self.session.headers.update({"Accept": "application/json"})

    # ------------------------------------------------------------ 底层
    def _log_http_fail(self, method: str, path: str, ms: int, exc: Exception):
        """HTTP 失败的分级记法。

        ★ 404 是**有效响应**（"没有这个东西"），很多源没有图标、有些条目已被删 ——
          记 WARNING 会把日志冲成噪音（实测一次页面加载十来条 icon 404），
          由**调用方**决定 404 算不算问题；这里只记 DEBUG。
        ★ 真正的故障（5xx / 连不上 / 超时）才是 WARNING。
        """
        # ★★★ 不能写 `getattr(exc, "response", None) and exc.response.status_code`：
        #   requests.Response 定义了 __bool__（状态码 <400 才为真）—— 对 404，
        #   `Response and X` 会**短路返回 Response 本身**（实测踩到：
        #   status 字段渲染成 `<Response [404]>`、404 分支永远进不去）。
        resp = getattr(exc, "response", None)
        code = resp.status_code if resp is not None else None
        if code == 404:
            log.debug("mf %s %s → 404（%sms，调用方自行决定是否是问题）",
                      method, path, ms, extra=fields(src="mf", ms=ms, status=404))
        else:
            log.warning("mf %s %s 失败（%sms）：%s", method, path, ms, exc,
                        extra=fields(src="mf", ms=ms, status=code))

    def _get(self, path, *, timeout=20, **params):
        t0 = time.monotonic()
        try:
            r = self.session.get(f"{self.base_url}{path}", params=params, timeout=timeout)
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001  记录后原样抛给调用方处理
            self._log_http_fail("GET", path, int((time.monotonic() - t0) * 1000), exc)
            raise
        ms = int((time.monotonic() - t0) * 1000)
        # ★ 每次 API 调用一行 DEBUG（文件里留全量、屏幕默认看不见）：
        #   排查"页面为什么慢/数据为什么不对"时，这条能直接对到具体端点。
        log.debug("mf GET %s → %s（%sms）", path, r.status_code, ms,
                  extra=fields(src="mf", ms=ms, status=r.status_code))
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            # ★ 这条不是凑数：本机代理会在上游挂掉时回一段 HTML 502，
            #   表现就是"接口突然全 500 但 HTTP 状态看着正常"。
            #   把响应体前 120 字打出来，一眼就能认出来。
            log.warning("mf GET %s 返回的不是 JSON（HTTP %s，前 120 字：%r）",
                        path, r.status_code, r.text[:120],
                        extra=fields(src="mf", status=r.status_code))
            raise

    def _put(self, path, json_body=None, timeout=30):
        t0 = time.monotonic()
        try:
            r = self.session.put(f"{self.base_url}{path}", json=json_body, timeout=timeout)
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            self._log_http_fail("PUT", path, int((time.monotonic() - t0) * 1000), exc)
            raise
        ms = int((time.monotonic() - t0) * 1000)
        log.debug("mf PUT %s → %s（%sms）", path, r.status_code, ms,
                  extra=fields(src="mf", ms=ms, status=r.status_code))
        # ★ 返回响应体（新建/改名要拿 id）；无 body 的 PUT（标已读等）调用方本来就不用返回值
        if not r.content:
            return True
        try:
            return r.json()
        except ValueError:
            return True

    def _post(self, path, json_body=None, timeout=30):
        t0 = time.monotonic()
        try:
            r = self.session.post(f"{self.base_url}{path}", json=json_body, timeout=timeout)
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            self._log_http_fail("POST", path, int((time.monotonic() - t0) * 1000), exc)
            raise
        ms = int((time.monotonic() - t0) * 1000)
        log.debug("mf POST %s → %s（%sms）", path, r.status_code, ms,
                  extra=fields(src="mf", ms=ms, status=r.status_code))
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            return {}

    def _delete(self, path, timeout=30):
        t0 = time.monotonic()
        try:
            r = self.session.delete(f"{self.base_url}{path}", timeout=timeout)
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            self._log_http_fail("DELETE", path, int((time.monotonic() - t0) * 1000), exc)
            raise
        ms = int((time.monotonic() - t0) * 1000)
        log.debug("mf DELETE %s → %s（%sms）", path, r.status_code, ms,
                  extra=fields(src="mf", ms=ms, status=r.status_code))
        return True

    def _raw_feeds(self):
        feeds = cache.get("mf:feeds")
        if feeds is None:
            feeds = self._get("/v1/feeds") or []
            cache.set("mf:feeds", feeds, CACHE_TTL)
        return feeds

    def _raw_categories(self):
        cats = cache.get("mf:categories")
        if cats is None:
            cats = self._get("/v1/categories") or []
            cache.set("mf:categories", cats, CACHE_TTL)
        return cats

    @staticmethod
    def _invalidate():
        cache.delete("mf:feeds")
        cache.delete("mf:categories")
        cache.delete("mf:tree")

    # ------------------------------------------------------------ 树
    def list_tree(self) -> list:
        tree = cache.get("mf:tree")
        if tree is not None:
            return tree

        counters = self.counters()
        feeds = self._raw_feeds()
        cats = {c["id"]: c["title"] for c in self._raw_categories()}

        by_cat: dict = {}
        # ★ 先把**所有分类**放进来（含还没有 feed 的空分类）：否则新建完分类，
        #   树上看不到它，也就没法右键往里加 feed。
        for cid, title in cats.items():
            by_cat[cid] = {"title": title, "feeds": []}
        for f in feeds:
            cat = f.get("category") or {}
            cat_id = cat.get("id") or 0
            cat_title = cats.get(cat_id, cat.get("title") or "未分类")
            health = self._health_from_feed(f)
            by_cat.setdefault(cat_id, {"title": cat_title, "feeds": []})
            by_cat[cat_id]["feeds"].append(
                FeedNode(
                    id=f["id"],
                    title=f.get("title") or f.get("feed_url") or f"#{f['id']}",
                    site_url=f.get("site_url") or "",
                    category_id=cat_id,
                    category_title=cat_title,
                    unread=int((counters.get(f["id"]) or {}).get("unreads", 0)),
                    ok=health["ok"],
                    error_msg=health["error_msg"],
                    last_success=health["last_success"],
                    icon_url=f"/icon/{f['id']}",
                )
            )

        tree = []
        for cat_id, payload in sorted(by_cat.items(), key=lambda kv: kv[1]["title"]):
            node = CategoryNode(id=cat_id, title=payload["title"])
            node.feeds = sorted(payload["feeds"], key=lambda x: (-x.unread, x.title.lower()))
            tree.append(node)

        cache.set("mf:tree", tree, CACHE_TTL)
        return tree

    def counters(self) -> dict:
        raw = self._get("/v1/feeds/counters") or {}
        unreads = raw.get("unreads") or {}
        reads = raw.get("reads") or {}
        out = {}
        for fid in set(list(unreads) + list(reads)):
            out[int(fid)] = {
                "unreads": int(unreads.get(fid, 0)),
                "reads": int(reads.get(fid, 0)),
            }
        return out

    def _health_from_feed(self, feed) -> dict:
        err = int(feed.get("parsing_error_count") or 0)
        msg = (feed.get("parsing_error_message") or "").strip()
        ls, _exact = last_success(feed)
        return {"ok": err == 0, "last_success": ls, "error_msg": msg}

    def health(self, feed_id) -> dict:
        for f in self._raw_feeds():
            if f["id"] == int(feed_id):
                return self._health_from_feed(f)
        return {"ok": True, "last_success": None, "error_msg": ""}

    # ------------------------------------------------------------ 条目
    @staticmethod
    def _to_entry(raw) -> Entry:
        feed = raw.get("feed") or {}
        return Entry(
            id=int(raw["id"]),
            feed_id=int(raw.get("feed_id") or feed.get("id") or 0),
            feed_title=feed.get("title") or "",
            title=raw.get("title") or "(无标题)",
            url=raw.get("url") or "",
            published_at=_dt(raw.get("published_at")),
            created_at=_dt(raw.get("created_at")),
            status=raw.get("status") or "unread",
            starred=bool(raw.get("starred")),
            author=raw.get("author") or "",
            reading_time=int(raw.get("reading_time") or 0),
            content=raw.get("content") or "",
        )

    def list_entries(self, *, feed_id=None, category_id=None, status="all",
                     search="", limit=200, offset=0, starred=None,
                     published_after=None, published_before=None) -> list:
        params = {
            "order": "published_at",
            "direction": "desc",
            "limit": min(int(limit), 1000),
            "offset": int(offset),
        }
        if status in ("read", "unread"):
            params["status"] = status
        if search:
            params["search"] = search
        if starred:
            # ★ Miniflux 的「星标」是 bookmark；查询参数就是 starred=true
            params["starred"] = "true"
        if feed_id:
            params["feed_id"] = int(feed_id)
        elif category_id:
            params["category_id"] = int(category_id)
        # ★ Miniflux 的 published_* 是**发布时间**口径（不是 created_at）；
        #   且只认 Unix 时间戳（RFC3339 会被静默忽略）
        if published_after:
            params["published_after"] = _ts(published_after)
        if published_before:
            params["published_before"] = _ts(published_before)

        raw = self._get("/v1/entries", **params) or {}
        return [self._to_entry(e) for e in raw.get("entries", [])]

    def get_entry(self, entry_id) -> Entry:
        raw = self._get(f"/v1/entries/{entry_id}")
        if not raw:
            return None
        return self._to_entry(raw)

    @staticmethod
    def _sort_time(e: Entry):
        ts = e.published_at or e.created_at
        if ts is None:
            return _EPOCH
        return ts if ts.tzinfo else ts.replace(tzinfo=dt_timezone.utc)

    def list_entries_multi(self, feed_ids, *, status="all", search="",
                           limit=200) -> list:
        """多 feed 归并（虚拟分组视图用）。

        ★★ 为什么要自己归并：Miniflux 的 `/v1/entries` 只支持**单个** `feed_id`
          —— 2.3.3 `entry_handlers.go` 用 `QueryInt64Param(r, "feed_id", ...)` 取标量，
          `EntryQueryBuilder.WithFeedID` 也只拼一个 `e.feed_id = $n` 条件。
          重复传参不会累加，所以多源视图没有服务端原生支持。
        ★ 口径（与"全部文章"**不完全等价**，UI 上要认）：
          每源先取 `min(limit, MULTI_PER_SOURCE)` 条 → 按 published_at 全局归并 → 取前 limit 条。
          也就是"每源最近 N 条里取全局最近 limit 条"，**不是**"全局最近 limit 条"。
          高频源会挤占窗口，低频源可能只进来很少几条。
        """
        ids = [int(i) for i in feed_ids]
        if not ids:
            return []
        limit = min(int(limit), 1000)
        per = max(1, min(limit, MULTI_PER_SOURCE))
        merged = []
        for fid in ids:
            merged.extend(self.list_entries(feed_id=fid, status=status,
                                            search=search, limit=per))
        merged.sort(key=self._sort_time, reverse=True)
        return merged[:limit]

    def iter_unread(self, *, feed_id=None, category_id=None, starred=None,
                    max_pages=10):
        """分页取未读条目，供批量标记用（单次 limit 上限 1000）。"""
        page = 0
        while page < max_pages:
            batch = self.list_entries(
                feed_id=feed_id,
                category_id=category_id,
                starred=starred,
                status="unread",
                limit=1000,
                offset=page * 1000,
            )
            if not batch:
                return
            yield from batch
            if len(batch) < 1000:
                return
            page += 1

    # ------------------------------------------------------------ 写操作
    def _set_entries_status(self, entry_ids, status: str) -> int:
        """read / unread 走同一个端点、同一份缓存失效口径（别抄成两份）。

        ★ Miniflux API `PUT /v1/entries` 接受 `{"entry_ids": [...], "status": ...}`，
          status 只认 `read` / `unread`（2.3.3 源码 `setEntryStatusAndStarredHandler`
          → `ValidateEntriesStatusAndStarredUpdateRequest`）。没有"标记未读"专用端点，
          别去猜 `/mark-as-unread` 之类的路径。
        """
        ids = [int(i) for i in entry_ids]
        if not ids:
            return 0
        self._put("/v1/entries", {"entry_ids": ids, "status": status})
        self._invalidate()
        return len(ids)

    def mark_read(self, entry_ids) -> int:
        return self._set_entries_status(entry_ids, "read")

    def mark_unread(self, entry_ids) -> int:
        """把已读条目退回未读（点开即标已读的反向入口）。"""
        return self._set_entries_status(entry_ids, "unread")

    def mark_scope_read(self, *, feed_id=None, category_id=None) -> int:
        if feed_id:
            self._put(f"/v1/feeds/{int(feed_id)}/mark-all-as-read")
        elif category_id:
            self._put(f"/v1/categories/{int(category_id)}/mark-all-as-read")
        else:
            raise ValueError("mark_scope_read 需要 feed_id 或 category_id")
        self._invalidate()
        return -1  # 具体条数未知

    def mark_read_before(self, *, days, feed_id=None, category_id=None,
                         starred=None) -> int:
        """把「发布时间早于 N 天前」的未读条目标为已读。"""
        cutoff = datetime.now().astimezone() - timedelta(days=int(days))
        ids = []
        for e in self.iter_unread(feed_id=feed_id, category_id=category_id,
                                  starred=starred):
            ts = e.published_at or e.created_at
            if ts is None or ts < cutoff:
                ids.append(e.id)
        return self.mark_read(ids)

    def toggle_star(self, entry_id, starred: bool) -> bool:
        """⚠ Miniflux 只有 `PUT /v1/entries/{id}/bookmark`（切换语义，不是 set），
        所以调用前必须先确认当前状态再决定调不调。
        ⚠ 端点名是 bookmark 不是 toggle-star（后者 404）。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            return False
        if bool(entry.starred) != bool(starred):
            self._put(f"/v1/entries/{int(entry_id)}/bookmark")
            self._invalidate()
        return True

    def refresh_feed(self, feed_id) -> bool:
        # ★★ timeout 必须比默认 30s 长：Miniflux 的 refresh 是**同步**的，要等源站抓完；
        #    动态渲染 / 反爬较重的源站实测 30–60s，30s 会把「慢但成功」误判成
        #    失败（实测过 `read timeout=30` 而源站其实抓到了内容、
        #    parsing_error_count 仍为 0 的情况）。
        self._put(f"/v1/feeds/{int(feed_id)}/refresh", timeout=120)
        self._invalidate()
        return True

    def fetch_content(self, entry_id, *, timeout=120) -> bool:
        """抓取该条目的原文全文（Miniflux "fetch original content"）。

        ★★★ 官方 API（2026-11-20 查证 miniflux.app/docs/api.html
            #endpoint-fetch-content）两个坑：
            ① 是 **GET** 不是 PUT（文档原样 `GET .../fetch-content?update_content=true`）；
            ② 必须带 `update_content=true`，否则只返回全文、**不写回数据库**
               （默认 false）—— 下次 get_entry 读到的还是短摘要，等于白抓。
        ★ 同步打源站页面，慢站几十秒：默认 20s 太短，这里 120s。
        ★ 不写本地正文副本（仓库硬约束）：Miniflux 侧覆盖后，下次 get_entry
           读到的就是全文。调用方负责二次确认（覆盖会破坏划线锚点）。
        """
        self._get(f"/v1/entries/{int(entry_id)}/fetch-content",
                  timeout=timeout, update_content="true")
        self._invalidate()
        return True

    # ------------------------------------------------------------ 分类 / feed 写入
    # ★ 阅读器从这里开始**写 Miniflux**（在此之前只有本地分组层会写本地库）。
    #   Miniflux 仍是唯一真相层：写完一律 _invalidate()，让树重新拉。
    def create_category(self, title: str) -> dict:
        data = self._post("/v1/categories", {"title": title}) or {}
        self._invalidate()
        return data

    def rename_category(self, category_id, title: str) -> dict:
        data = self._put(f"/v1/categories/{int(category_id)}", {"title": title}) or {}
        self._invalidate()
        return data

    def create_feed(self, feed_url: str, category_id) -> dict:
        # ★★ Miniflux 会在 POST 里**同步抓取并解析**这个 URL：慢源/需要渲染的源可能 30s+。
        #    这里的 timeout 给到 90s；前端也是同步等结果，成功才刷新树。
        data = self._post("/v1/feeds",
                          {"feed_url": feed_url, "category_id": int(category_id)},
                          timeout=90) or {}
        self._invalidate()
        return data

    def rename_feed(self, feed_id, title: str) -> dict:
        """只改标题。

        ★★ 这里**刻意不提供改分类的入口**：PUT /v1/feeds 也能改 `category_id`，
          但跨分类移动会清零 `parsing_error_count`、让历史条目整体改属、
          并立刻切换判据继承——那是独立的一次改动，不做"顺手"。
          所以本函数的 body 只有 `title`，调用方也拿不到传分类的口子。
        """
        data = self._put(f"/v1/feeds/{int(feed_id)}", {"title": title}) or {}
        self._invalidate()
        return data

    def delete_feed(self, feed_id) -> bool:
        """删除订阅。**不可逆**。

        ★★ Miniflux 的 DELETE /v1/feeds/{id} 会连这个源的**全部条目**一起删
           （不是"取消订阅保留历史"）。所以 UI 侧必须二次确认并把这句话说清楚。
        """
        self._delete(f"/v1/feeds/{int(feed_id)}")
        self._invalidate()
        return True

    def delete_category(self, category_id) -> bool:
        # ★ 只允许删空分类（调用方先校验）；Miniflux 对非空分类会拒绝。
        self._delete(f"/v1/categories/{int(category_id)}")
        self._invalidate()
        return True

    def icon(self, feed_id):
        try:
            raw = self._get(f"/v1/feeds/{int(feed_id)}/icon") or {}
        except Exception:
            return None
        data = raw.get("data") or ""
        mime = raw.get("mime_type") or ""
        if not data:
            return None
        # Miniflux 返回的是 "image/x-icon;base64,...."（没有 data: 前缀）
        if "," in data and "base64" in data:
            header, b64 = data.split(",", 1)
            ctype = mime or header.split(";")[0] or "image/png"
            try:
                return ctype, base64.b64decode(b64)
            except Exception:
                return None
        if data.startswith("data:"):
            header, b64 = data.split(",", 1)
            ctype = header[len("data:"):].split(";")[0] or "image/png"
            try:
                return ctype, base64.b64decode(b64)
            except Exception:
                return None
        return None
