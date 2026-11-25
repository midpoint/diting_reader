"""FreshRSS（Google Reader 兼容 API）数据源实现。

只暴露阅读器语义（见 base.py），FreshRSS / GReader 特有概念在这里消化掉：

  · 分类（FreshRSS category / label）在协议里只有字符串 `user/-/label/<名>`，
    接口却要求 int id → 用 `core.CategoryLabel` 持久化映射（首次见到分配 id，
    改名保留 id，删除软删）。**不用字符串哈希凑 id**。
  · 条目 id：`stream/contents` 返回 `tag:google.com,2005:reader/item/<16 位十六进制>`，
    取十六进制部分转 int64；`stream/items/ids` 返回 64 位十进制，两种都收。
  · feed id：FreshRSS 的流 id 是 `feed/<数字>`，直接取数字。

三个协议做不到的点（按任务书降级，不伪造）：
  · refresh_feed  → 协议无单源立即刷新；记 info 日志返回 True，等 FreshRSS 自身周期。
  · fetch_content → 协议无抓原文全文；抛 NotImplementedError（源侧「抓取全文」解决）。
  · health        → 协议不给抓取错误；恒返回 ok=True（UI 上该源永远显示正常）。

★ 线程安全：与 MinifluxDataSource 一样，实例内部持 `requests.Session`，
  不是线程安全的。并发场景用 `new_datasource()` 建独立实例（沿用现有约定）。
"""
import html
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import quote, urljoin, urlsplit

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError

from core.logkit import fields
from core.models import CategoryLabel

from .base import CategoryNode, DataSource, Entry, FeedNode

log = logging.getLogger(__name__)

# 树 / 未读数 / 订阅表的进程内短缓存（秒）。写操作后立即 _invalidate()。
# ★ 这只是避免一次页面加载重复打 subscription/list+unread-count；
#   条目流另有独立的、有界的短缓存（见下）。
CACHE_TTL = int(getattr(settings, "TREE_CACHE_TTL", 20) or 20)

# 条目流短缓存：首屏 + HTMX 局部刷新常在同一秒内重复请求同一筛选。
# ★ 只缓存 UI 规模（limit <= ENTRY_CACHE_MAX_LIMIT）的结果；日报/分级一次要
#   1000 条，不驻留内存。写操作（标已读/星标/全部已读）会整体清空。
ENTRY_CACHE_TTL = int(getattr(settings, "GREADER_ENTRY_CACHE_TTL", 15) or 0)
ENTRY_CACHE_MAX = max(0, int(getattr(settings, "GREADER_ENTRY_CACHE_MAX", 24) or 0))
ENTRY_CACHE_MAX_LIMIT = 300

# 图标负缓存 / 大小上限：404、超时、非图片在 TTL 内不再重试。
ICON_MISS_TTL = int(getattr(settings, "GREADER_ICON_MISS_TTL", 300) or 0)
ICON_MAX_BYTES = max(0, int(getattr(settings, "GREADER_ICON_MAX_BYTES",
                                     256 * 1024) or 0))

MULTI_PER_SOURCE = int(getattr(settings, "MULTI_FEED_PER_SOURCE", 60) or 60)

READ_STATE = "user/-/state/com.google/read"
STAR_STATE = "user/-/state/com.google/starred"
READING_LIST = "user/-/state/com.google/reading-list"
STARRED_STREAM = STAR_STATE
LABEL_PREFIX = "user/-/label/"
ITEM_PREFIX = "tag:google.com,2005:reader/item/"

# 单页最多取多少条（Google Reader 上限 1000）
MAX_PAGE = 1000
# iter_unread 默认最多翻多少页（与 Miniflux 实现的 max_pages 语义保持一致）
MAX_UNREAD_PAGES = 10
# 写操作批量上限（i= 可重复；太大可能撞 PHP max_input_vars / 请求体限制）
WRITE_CHUNK = 500

_EPOCH = datetime(1970, 1, 1, tzinfo=dt_timezone.utc)
_ITEM_RE = re.compile(r"reader/item/([0-9a-fA-F]+)$")
_HEX_RE = re.compile(r"^[0-9a-fA-F]{1,16}$")


class GReaderError(RuntimeError):
    """带端点信息的 GReader 失败（调用方据此给用户人话，不静默吞）。"""

    def __init__(self, method: str, path: str, status=None, message: str = ""):
        self.method = method
        self.path = path
        self.status = status
        self.message = message or ""
        parts = [f"{method} {path}".strip() + " 失败"]
        if status is not None:
            parts.append(f"（HTTP {status}）")
        if self.message:
            parts.append(f"：{self.message}")
        super().__init__("".join(parts))


# ------------------------------------------------------------------ 小工具
def _dt_from_seconds(value):
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(float(value)), tz=dt_timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _dt_from_usec(value):
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(float(value)) / 1_000_000, tz=dt_timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _dt_from_msec(value):
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(float(value)) / 1_000, tz=dt_timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def _unix(value):
    """时间 → Unix 秒（ot/nt 参数口径）。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if hasattr(value, "timestamp"):
        return int(value.timestamp())
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _aware(value):
    """naive datetime → UTC aware（本地比较不因 tz 混用抛 TypeError）。"""
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=dt_timezone.utc)
    return value


def _parse_entry_id(value) -> int:
    """GReader 条目 ID → int64。

    · 完整 tag：`tag:google.com,2005:reader/item/<hex>` → hex 转 int；
    · `stream/items/ids` 的 `itemRefs[].id` 是 **64 位十进制**；
    · 少数实现给裸十六进制（带 a-f），也接受。
    """
    if isinstance(value, int):
        return value
    s = str(value or "").strip()
    m = _ITEM_RE.search(s)
    if m:
        return int(m.group(1), 16)
    if s.isdigit():
        return int(s, 10)
    if _HEX_RE.match(s):
        return int(s, 16)
    raise GReaderError("PARSE", "", None, f"无法解析条目 ID：{s[:80]}")


def _api_entry_id(entry_id) -> str:
    """int64 → `i=` 接受的完整 tag ID（16 位十六进制，左侧补零）。"""
    return ITEM_PREFIX + format(int(entry_id), "016x")


def _feed_id_from_stream(stream) -> int:
    """`feed/123` → 123。非数字流（URL 形式）返回 0 并告警。"""
    if not isinstance(stream, str) or not stream.startswith("feed/"):
        return 0
    tail = stream[len("feed/"):]
    if tail.isdigit():
        return int(tail)
    log.warning("greader：不支持非数字 feed 流 ID（%r），该源将被跳过", tail[:100])
    return 0


def _label_of(category) -> str:
    """`{'id': 'user/-/label/X', 'label': 'X'}` → 'X'。"""
    if not isinstance(category, dict):
        return ""
    label = category.get("label") or ""
    if not label:
        cid = category.get("id") or ""
        if isinstance(cid, str) and cid.startswith(LABEL_PREFIX):
            label = cid[len(LABEL_PREFIX):]
    return html.unescape(str(label)).strip()


def _html_of(node) -> str:
    """`{"content": "..."}`（FreshRSS）或裸字符串（部分实现）→ HTML 文本。"""
    if isinstance(node, dict):
        return node.get("content") or ""
    return node if isinstance(node, str) else ""


def _item_time(entry: Entry):
    return entry.published_at or entry.created_at


def _within(entry: Entry, after, before) -> bool:
    """published_at 口径的本地兜底过滤（base.py 的硬要求）。"""
    ts = _item_time(entry)
    if ts is None:
        return after is None and before is None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt_timezone.utc)
    if after is not None and ts < after:
        return False
    if before is not None and ts > before:
        return False
    return True


def _reading_time(content: str) -> int:
    """粗略阅读时长（分钟）：中文按 500 字/分钟。0 = 不显示。"""
    text = re.sub(r"<[^>]+>", " ", content or "")
    text = html.unescape(text).strip()
    if not text:
        return 0
    return max(1, round(len(text) / 500))


def _chunks(seq, size):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _snippet(text, limit=160) -> str:
    """错误响应摘要：去掉 HTML 标签/多余空白，避免把整页登录 HTML 塞进报错。"""
    if not text:
        return ""
    plain = re.sub(r"<[^>]+>", " ", str(text))
    plain = html.unescape(plain)
    return re.sub(r"\s+", " ", plain).strip()[:limit]


class GReaderDataSource(DataSource):
    def __init__(self, base_url=None, username=None, password=None):
        self.base_url = (base_url or settings.GREADER_BASE_URL or "").rstrip("/")
        self.username = username or settings.GREADER_USERNAME
        self.password = password or settings.GREADER_PASSWORD
        missing = [name for name, val in (
            ("GREADER_BASE_URL", self.base_url),
            ("GREADER_USERNAME", self.username),
            ("GREADER_PASSWORD", self.password),
        ) if not val]
        if missing:
            raise GReaderError("CONFIG", "", None,
                               "缺少 FreshRSS 配置：" + ", ".join(missing) + "（写在 .env）")

        self.session = requests.Session()
        # 与 Miniflux 一致：macOS 系统代理会把本机/内网请求也送上游
        self.session.trust_env = False
        self.session.headers.update({"Accept": "application/json"})

        self._auth_token = None
        self._post_token = None
        self._search_unsupported = False
        self._cache = {}
        self._entry_cache = {}
        self._entry_lock = threading.Lock()

    # ------------------------------------------------------------ 底层 HTTP
    def _login(self, *, force=False):
        """ClientLogin。成功后把 Authorization 头挂到 session 上。

        ★ 日志只记"成功/失败"，不打印密码，也不打印完整 Auth token。
        """
        if self._auth_token and not force:
            return
        path = "/accounts/ClientLogin"
        try:
            r = self.session.post(
                f"{self.base_url}{path}",
                data={"Email": self.username, "Passwd": self.password,
                      "service": "reader"},
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("gr POST %s 失败：%s", path, exc,
                        extra=fields(src="gr", status=None))
            raise GReaderError(
                "POST", path, None,
                f"无法连接 FreshRSS（{self.base_url}）：{exc}。"
                "请检查 GREADER_BASE_URL、网络与 FreshRSS 是否在运行",
            ) from exc
        if r.status_code == 401:
            raise GReaderError("POST", path, 401,
                               "用户名或 API 密码错误（请用 FreshRSS 个人资料页的"
                               "「专用 API 密码」，不是网页登录密码）")
        if r.status_code == 403:
            raise GReaderError("POST", path, 403,
                               "FreshRSS 拒绝登录（请确认后台已开启 API 访问）")
        if r.status_code >= 500:
            raise GReaderError("POST", path, r.status_code,
                               f"FreshRSS 服务端错误（HTTP {r.status_code}），请稍后重试"
                               + (f"：{_snippet(r.text)}" if _snippet(r.text) else ""))
        if not r.ok:
            raise GReaderError("POST", path, r.status_code, _snippet(r.text))

        auth = ""
        for line in (r.text or "").splitlines():
            if line.startswith("Auth="):
                auth = line[len("Auth="):].strip()
                break
        if not auth:
            raise GReaderError("POST", path, r.status_code, "响应里没有 Auth=…")
        self._auth_token = auth
        self.session.headers["Authorization"] = f"GoogleLogin auth={auth}"
        # 重新登录后旧 POST token 未必还有效，强制下次重取
        self._post_token = None
        log.info("greader 登录成功 user=%s", self.username)

    def _request(self, method, path, *, params=None, data=None, timeout=20, _retry=True):
        self._login()
        t0 = time.monotonic()
        try:
            r = self.session.request(method, f"{self.base_url}{path}",
                                     params=params, data=data, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            ms = int((time.monotonic() - t0) * 1000)
            log.warning("gr %s %s 失败（%sms）：%s", method, path, ms, exc,
                        extra=fields(src="gr", ms=ms, status=None))
            if isinstance(exc, requests.Timeout):
                hint = f"请求超时（{timeout}s）"
            else:
                hint = (f"无法连接 FreshRSS（{self.base_url}）：{exc}。"
                        "请检查 GREADER_BASE_URL、网络与 FreshRSS 是否在运行")
            raise GReaderError(method, path, None, hint) from exc

        ms = int((time.monotonic() - t0) * 1000)
        if r.status_code == 401 and _retry:
            # 会话过期：重登一次再试（任务书要求；只重试一次）
            log.info("greader 401，重新登录后重试一次：%s %s", method, path)
            self._login(force=True)
            return self._request(method, path, params=params, data=data,
                                 timeout=timeout, _retry=False)
        if r.status_code == 401:
            raise GReaderError(method, path, 401,
                               "重新登录后仍被拒绝：账号权限或 API 密码已失效")
        if r.status_code == 403:
            raise GReaderError(method, path, 403,
                               "没有权限（请确认 FreshRSS 后台已开启 API 访问）")
        if r.status_code >= 500:
            snippet = _snippet(r.text)
            log.warning("gr %s %s → HTTP %s（%sms）：%s",
                        method, path, r.status_code, ms, snippet,
                        extra=fields(src="gr", ms=ms, status=r.status_code))
            raise GReaderError(method, path, r.status_code,
                               f"FreshRSS 服务端错误（HTTP {r.status_code}），请稍后重试"
                               + (f"：{snippet}" if snippet else ""))
        if r.status_code >= 400:
            snippet = _snippet(r.text)
            log.warning("gr %s %s → HTTP %s（%sms）：%s",
                        method, path, r.status_code, ms, snippet,
                        extra=fields(src="gr", ms=ms, status=r.status_code))
            raise GReaderError(method, path, r.status_code, snippet)

        # 每次调用一行 DEBUG（与 miniflux 的数据源日志口径一致）
        log.debug("gr %s %s → %s（%sms）", method, path, r.status_code, ms,
                  extra=fields(src="gr", ms=ms, status=r.status_code))
        return r

    def _json(self, method, path, *, params=None, data=None, timeout=20):
        r = self._request(method, path, params=params, data=data, timeout=timeout)
        if not r.content:
            return {}
        try:
            return r.json()
        except ValueError:
            snippet = _snippet(r.text, 120)
            log.warning("gr %s %s 返回的不是 JSON（HTTP %s，前 120 字：%r）",
                        method, path, r.status_code, snippet,
                        extra=fields(src="gr", status=r.status_code))
            raise GReaderError(method, path, r.status_code,
                               "返回了 HTML/文本而不是 JSON（GREADER_BASE_URL 是否正确？"
                               "FreshRSS 是否已开启 API？）"
                               + (f"：{snippet}" if snippet else ""))

    def _post_token_value(self):
        """取 POST token（写操作表单里的 T=）。FreshRSS 的 token 不会过期，缓存即可。"""
        if self._post_token:
            return self._post_token
        r = self._request("GET", "/reader/api/0/token")
        token = (r.text or "").strip()
        if not token:
            raise GReaderError("GET", "/reader/api/0/token", r.status_code, "token 为空")
        self._post_token = token
        return token

    def _post_action(self, path, data, *, timeout=30, retries=1):
        """带 T= 的写操作。

        ★ FreshRSS 对失效 token 回 401（checkToken → unauthorized）。这里取新 token
          重试一次；401 的重新登录由 _request 自己处理，两个机制各管一层。
        """
        payload = dict(data)
        payload["T"] = self._post_token_value()
        try:
            return self._request("POST", path, data=payload, timeout=timeout)
        except GReaderError as exc:
            if retries and exc.status in (400, 401, 403):
                log.info("greader 写操作 token 可能失效（HTTP %s），重取 token 重试一次：%s",
                         exc.status, path)
                self._post_token = None
                payload = dict(payload)   # 不就地改第一次调用用的字典（日志/测试可复核）
                payload["T"] = self._post_token_value()
                return self._request("POST", path, data=payload, timeout=timeout)
            raise

    # ------------------------------------------------------------ 缓存
    def _cached(self, key, fn):
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        val = fn()
        self._cache[key] = (now + CACHE_TTL, val)
        return val

    def _invalidate(self):
        """写操作后清空树/计数与条目流缓存（同一进程内立即一致）。"""
        self._cache.clear()
        with self._entry_lock:
            self._entry_cache.clear()

    # ------------------------------------------------------------ 条目流缓存
    @staticmethod
    def _entry_cache_key(*, feed_id, category_id, status, search, starred,
                         limit, offset, after, before):
        """缓存键必须覆盖所有会影响结果的参数。limit 过大不缓存（见下）。"""
        if not ENTRY_CACHE_TTL or ENTRY_CACHE_MAX <= 0:
            return None
        if int(limit) > ENTRY_CACHE_MAX_LIMIT:
            return None
        return (
            int(feed_id) if feed_id else None,
            int(category_id) if category_id is not None else None,
            status or "all",
            (search or "").strip().lower(),
            None if starred is None else bool(starred),
            int(limit), int(offset), _unix(after), _unix(before),
        )

    def _entry_cache_get(self, key):
        if key is None:
            return None
        now = time.monotonic()
        with self._entry_lock:
            hit = self._entry_cache.get(key)
            if hit is None:
                return None
            expires, value = hit
            if expires <= now:
                self._entry_cache.pop(key, None)
                return None
            return list(value)

    def _entry_cache_put(self, key, value):
        if key is None:
            return
        now = time.monotonic()
        with self._entry_lock:
            # 顺手清过期键，再按"最多 N 个筛选"淘汰最早插入的（dict 保序）
            for k, (expires, _v) in list(self._entry_cache.items()):
                if expires <= now:
                    self._entry_cache.pop(k, None)
            self._entry_cache[key] = (now + ENTRY_CACHE_TTL, list(value))
            while len(self._entry_cache) > ENTRY_CACHE_MAX:
                self._entry_cache.pop(next(iter(self._entry_cache)))

    def _raw_subs(self):
        def fetch():
            data = self._json("GET", "/reader/api/0/subscription/list",
                              params={"output": "json"})
            return data.get("subscriptions") or []
        return self._cached("subscriptions", fetch)

    def _raw_unread(self):
        def fetch():
            data = self._json("GET", "/reader/api/0/unread-count",
                              params={"output": "json"})
            return data.get("unreadcounts") or []
        return self._cached("unread", fetch)

    def _raw_tags(self):
        def fetch():
            data = self._json("GET", "/reader/api/0/tag/list",
                              params={"output": "json"})
            return data.get("tags") or []
        return self._cached("tags", fetch)

    # ------------------------------------------------------------ 本地 label 映射
    def _touch_mapping(self, label: str, *, local_only: bool = False,
                       force_local_only: bool = False) -> CategoryLabel:
        """取/建 label 映射行；顺便把状态修正到当前事实。

        · 订阅里出现 → active=True、local_only=False（FreshRSS 侧真实存在）
        · create_category → 只建本地行，local_only=True（FreshRSS 侧尚未创建）
        """
        label = (label or "").strip()
        if not label:
            raise GReaderError("LOCAL", "", None, "label 不能为空")
        obj = CategoryLabel.objects.filter(label=label).first()
        if obj is None:
            try:
                return CategoryLabel.objects.create(label=label, local_only=local_only)
            except IntegrityError:
                # 并发下另一个线程先插入了：回头取它
                obj = CategoryLabel.objects.get(label=label)
        changed = []
        if not obj.active:
            obj.active = True
            changed.append("active")
        if force_local_only and not obj.local_only:
            # create_category 的语义：先当成本地空分类；下次在订阅里看到它会自动转正
            obj.local_only = True
            changed.append("local_only")
        elif not local_only and obj.local_only:
            obj.local_only = False
            changed.append("local_only")
        if changed:
            obj.save(update_fields=changed + ["updated_at"])
        return obj

    def _label_for_id(self, category_id) -> str:
        cid = int(category_id)
        obj = CategoryLabel.objects.filter(pk=cid).first()
        if obj is None or not obj.active:
            raise GReaderError("LOCAL", "", None, f"未知分类 id={cid}（本地映射不存在）")
        return obj.label

    def _feed_category_map(self):
        """{feed_id: category_id}（搜索按分类过滤用，不递归取整棵树）。"""
        by_label = {m.label: m.pk for m in CategoryLabel.objects.filter(active=True)}
        out = {}
        for sub in self._raw_subs():
            fid = _feed_id_from_stream(sub.get("id"))
            if not fid:
                continue
            cats = sub.get("categories") or []
            label = _label_of(cats[0]) if cats else ""
            if label:
                if label not in by_label:
                    by_label[label] = self._touch_mapping(label).pk
                out[fid] = by_label[label]
            else:
                out[fid] = 0
        return out

    def _uncategorized_feed_ids(self):
        out = []
        for sub in self._raw_subs():
            cats = sub.get("categories") or []
            if not cats or not _label_of(cats[0]):
                fid = _feed_id_from_stream(sub.get("id"))
                if fid:
                    out.append(fid)
        return out

    # ------------------------------------------------------------ 树 / 计数
    def counters(self) -> dict:
        """{feed_id: {'reads': n, 'unreads': n}}。

        ★ GReader 的 unread-count 只有未读数，没有已读数；已读要逐源扫条目才能凑，
          代价与收益不成比例（UI 与现有业务也只用 unreads）。这里 reads 恒为 0，
          与"读不到"的语义一致，不伪造数字。
        """
        out = {}
        for row in self._raw_unread():
            fid = _feed_id_from_stream(row.get("id"))
            if fid:
                out[fid] = {"unreads": int(row.get("count") or 0), "reads": 0}
        return out

    def list_tree(self) -> list:
        counters = self.counters()
        by_cat = {}

        for sub in self._raw_subs():
            fid = _feed_id_from_stream(sub.get("id"))
            if not fid:
                continue
            cats = sub.get("categories") or []
            label = _label_of(cats[0]) if cats else ""
            # ★ FreshRSS 一个 feed 只属于一个分类；多 label 时取第一个并在报告里注明。
            if label:
                mapping = self._touch_mapping(label)
                cat_id, cat_title = mapping.pk, mapping.label
            else:
                cat_id, cat_title = 0, "未分类"
            bucket = by_cat.setdefault(cat_id, {"title": cat_title, "feeds": []})
            bucket["feeds"].append(FeedNode(
                id=fid,
                title=html.unescape(sub.get("title") or sub.get("url") or f"#{fid}"),
                site_url=sub.get("htmlUrl") or "",
                category_id=cat_id,
                category_title=cat_title,
                unread=int((counters.get(fid) or {}).get("unreads", 0)),
                ok=True,
                error_msg="",
                last_success=None,
                icon_url=f"/icon/{fid}",
            ))

        # FreshRSS 侧存在但当前没有 feed 的分类（tag/list 的 folder 类型）。
        # 老版本 FreshRSS 可能不带 type；带不上就等它第一次有 feed 再出现。
        for label in self._folder_labels():
            mapping = self._touch_mapping(label)
            by_cat.setdefault(mapping.pk, {"title": mapping.label, "feeds": []})

        # 由 reader 新建、FreshRSS 侧还没落地的空分类。
        for mapping in CategoryLabel.objects.filter(active=True, local_only=True):
            by_cat.setdefault(mapping.pk, {"title": mapping.label, "feeds": []})

        tree = []
        for cat_id, payload in sorted(by_cat.items(), key=lambda kv: kv[1]["title"]):
            node = CategoryNode(id=cat_id, title=payload["title"])
            node.feeds = sorted(payload["feeds"], key=lambda x: (-x.unread, x.title.lower()))
            tree.append(node)
        return tree

    def _folder_labels(self):
        """tag/list 里 type=folder 的 label（FreshRSS 的空分类也能看到）。

        ★ 非核心路径：tag/list 失败不该拖垮整棵树（订阅与未读数才是主数据）。
        """
        try:
            tags = self._raw_tags()
        except GReaderError as exc:
            log.warning("greader tag/list 不可用（%s），空分类暂不显示：%s",
                        getattr(exc, "status", None), exc)
            return []
        out = []
        for tag in tags:
            if not isinstance(tag, dict) or tag.get("type") != "folder":
                continue
            tid = tag.get("id") or ""
            if isinstance(tid, str) and tid.startswith(LABEL_PREFIX):
                label = html.unescape(tid[len(LABEL_PREFIX):]).strip()
                if label:
                    out.append(label)
        return out

    def health(self, feed_id) -> dict:
        """GReader 协议不给抓取错误信息 → 恒正常（已知取舍，见模块头）。"""
        return {"ok": True, "last_success": None, "error_msg": ""}

    # ------------------------------------------------------------ 条目
    @staticmethod
    def _to_entry(raw) -> Entry:
        item = raw or {}
        origin = item.get("origin") or {}
        categories = item.get("categories") or []
        content = (_html_of(item.get("content"))
                   or _html_of(item.get("summary")) or "")

        url = ""
        for key in ("alternate", "canonical"):
            for link in item.get(key) or []:
                if isinstance(link, dict) and link.get("href"):
                    url = link["href"]
                    break
            if url:
                break

        published = (_dt_from_seconds(item.get("published"))
                     or _dt_from_usec(item.get("timestampUsec"))
                     or _dt_from_msec(item.get("crawlTimeMsec")))
        created = (_dt_from_msec(item.get("crawlTimeMsec"))
                   or _dt_from_usec(item.get("timestampUsec")))

        return Entry(
            id=_parse_entry_id(item.get("id")),
            feed_id=_feed_id_from_stream(origin.get("streamId")),
            feed_title=html.unescape(origin.get("title") or ""),
            title=html.unescape(item.get("title") or "") or "(无标题)",
            url=url,
            published_at=published,
            created_at=created,
            status="read" if READ_STATE in categories else "unread",
            starred=STAR_STATE in categories,
            author=html.unescape(item.get("author") or ""),
            reading_time=_reading_time(content),
            content=content,
        )

    def _safe_entry(self, raw):
        try:
            return self._to_entry(raw)
        except Exception as exc:  # noqa: BLE001  单条坏数据不该拖垮整个列表
            log.debug("greader 跳过无法解析的条目：%s", exc)
            return None

    @staticmethod
    def _sort_time(entry: Entry):
        ts = _item_time(entry)
        if ts is None:
            return _EPOCH
        return ts if ts.tzinfo else ts.replace(tzinfo=dt_timezone.utc)

    def _stream_for(self, feed_id=None, category_id=None, *, starred=False):
        """接口参数 → GReader stream id。category_id=0 表示"未分类"，返回 None。"""
        if feed_id:
            return f"feed/{int(feed_id)}"
        if category_id is not None:
            cid = int(category_id)
            if cid == 0:
                return None
            return LABEL_PREFIX + quote(self._label_for_id(cid), safe="")
        if starred:
            return STARRED_STREAM
        return READING_LIST

    def _stream_path(self, stream: str) -> str:
        # 流 id 里的 label 已单独 quote；这里的 / 都是结构分隔符，不能再编码
        return "/reader/api/0/stream/contents/" + stream

    def _iter_items(self, stream, *, status="all", after=None, before=None,
                    page_size=200, max_pages=MAX_UNREAD_PAGES, max_items=10_000):
        """按 continuation 翻页产出原始 item（未转 Entry）。

        ot = 时间窗起点（oldest），nt = 终点（newest）—— FreshRSS 源码
        `$start_time = $_GET['ot']; $stop_time = $_GET['nt']`，别写反。
        """
        params = {"n": min(int(page_size), MAX_PAGE), "r": "n"}
        if status == "unread":
            params["xt"] = READ_STATE
        elif status == "read":
            # FreshRSS 的 it= 是"只保留该状态"（源码 filter_target）
            params["it"] = READ_STATE
        ot, nt = _unix(after), _unix(before)
        if ot:
            params["ot"] = ot
        if nt:
            params["nt"] = nt

        continuation = None
        seen_tokens = set()
        got = 0
        for _ in range(max(1, int(max_pages))):
            page_params = dict(params)
            if continuation:
                page_params["c"] = continuation
            data = self._json("GET", self._stream_path(stream), params=page_params)
            items = data.get("items") or []
            if not items:
                return
            for item in items:
                yield item
                got += 1
                if got >= max_items:
                    return
            continuation = data.get("continuation")
            if not continuation:
                return
            # ★ 防御坏服务端/代理：continuation 重复会无限翻同一页并吃满内存。
            #   正常 FreshRSS 的 token 是每次前进的；重复即停止并留一条 warning。
            token = str(continuation)
            if token in seen_tokens:
                log.warning("greader continuation 重复（%s），停止翻页以免死循环",
                            token[:80])
                return
            seen_tokens.add(token)

    def _collect_entries(self, stream, *, status="all", after=None, before=None,
                         want=200, starred_local=False, title_match=None):
        """取到 want 条（含本地 star / 标题过滤）为止。"""
        want = max(0, int(want))
        if want <= 0:
            return []
        local_filter = bool(starred_local or title_match)
        # 本地过滤会丢条目：多扫一点；纯拉流则按需即可。
        max_items = want * (5 if local_filter else 1)
        page_size = min(MAX_PAGE, max(200, want))
        # 翻页上限按"要多少条"推，不写死 10 页：大 offset（总结分页）也能走到
        max_pages = max(1, min(100, max_items // page_size + 2))
        out, seen = [], set()
        for raw in self._iter_items(stream, status=status, after=after, before=before,
                                    page_size=page_size, max_pages=max_pages,
                                    max_items=max_items):
            entry = self._safe_entry(raw)
            if entry is None or entry.id in seen:
                continue
            seen.add(entry.id)
            if title_match and title_match not in (entry.title or "").lower():
                continue
            if starred_local and not entry.starred:
                continue
            if not _within(entry, after, before):
                continue
            out.append(entry)
            if len(out) >= want:
                break
        return out

    def _fetch_entries(self, *, feed_id=None, category_id=None, starred=None,
                       status="all", after=None, before=None, want=200,
                       title_match=None):
        stream = self._stream_for(feed_id, category_id, starred=bool(starred))
        starred_local = bool(starred and (feed_id or category_id is not None))
        if stream is not None:
            return self._collect_entries(stream, status=status, after=after,
                                         before=before, want=want,
                                         starred_local=starred_local,
                                         title_match=title_match)
        # category_id=0：GReader 没有"未分类"流 → 逐源取后在内存归并
        out = []
        for fid in self._uncategorized_feed_ids():
            out.extend(self._collect_entries(f"feed/{fid}", status=status, after=after,
                                             before=before, want=want,
                                             starred_local=starred_local,
                                             title_match=title_match))
            if len(out) >= want:
                break
        return out

    def list_entries(self, *, feed_id=None, category_id=None, status="all",
                     search="", limit=200, offset=0, starred=None,
                     published_after=None, published_before=None) -> list:
        limit = max(0, int(limit))
        offset = max(0, int(offset))
        if limit == 0:
            return []
        want = offset + limit
        published_after = _aware(published_after)
        published_before = _aware(published_before)

        cache_key = self._entry_cache_key(
            feed_id=feed_id, category_id=category_id, status=status,
            search=search, starred=starred, limit=limit, offset=offset,
            after=published_after, before=published_before)
        hit = self._entry_cache_get(cache_key)
        if hit is not None:
            return hit

        if search:
            entries = self._search_entries(
                search, feed_id=feed_id, category_id=category_id, status=status,
                starred=starred, after=published_after, before=published_before,
                want=want)
        else:
            entries = self._fetch_entries(
                feed_id=feed_id, category_id=category_id, starred=starred,
                status=status, after=published_after, before=published_before,
                want=want)

        entries.sort(key=self._sort_time, reverse=True)
        result = entries[offset:offset + limit]
        self._entry_cache_put(cache_key, result)
        return result

    def list_entries_multi(self, feed_ids, *, status="all", search="",
                           limit=200) -> list:
        """多 feed 归并（虚拟分组视图）。口径与 miniflux.py 一致：
        每源先取 min(limit, MULTI_PER_SOURCE) 条 → 全局按发布时间归并 → 取前 limit。
        """
        ids = [int(i) for i in feed_ids]
        if not ids:
            return []
        limit = min(int(limit), MAX_PAGE)
        per = max(1, min(limit, MULTI_PER_SOURCE))
        merged = []
        for fid in ids:
            merged.extend(self.list_entries(feed_id=fid, status=status,
                                            search=search, limit=per))
        merged.sort(key=self._sort_time, reverse=True)
        return merged[:limit]

    def get_entry(self, entry_id) -> Entry:
        try:
            api_id = _api_entry_id(entry_id)
        except Exception:
            return None
        # ★ FreshRSS 只处理 POST items/contents（源码 `isset($_POST['i'])`），
        #   不是状态变更，不需要 T= token。
        data = self._json("POST", "/reader/api/0/stream/items/contents",
                          data={"i": api_id, "output": "json"})
        items = data.get("items") or []
        return self._safe_entry(items[0]) if items else None

    def _items_by_ids(self, entry_ids):
        out = []
        for chunk in _chunks(entry_ids, 200):
            data = self._json(
                "POST", "/reader/api/0/stream/items/contents",
                data={"i": [_api_entry_id(i) for i in chunk], "output": "json"})
            out.extend(data.get("items") or [])
        return out

    def _search_ids(self, query, limit):
        data = self._json("GET", "/reader/api/0/search/items/ids",
                          params={"q": query, "n": min(int(limit), MAX_PAGE),
                                  "output": "json"})
        raw = (data.get("itemRefs") or data.get("results")
               or data.get("items") or data.get("ids") or [])
        ids = []
        for row in raw:
            value = row.get("id") if isinstance(row, dict) else row
            if value is None:
                continue
            try:
                ids.append(_parse_entry_id(value))
            except GReaderError:
                continue
        return list(dict.fromkeys(ids))

    def _search_entries(self, query, *, feed_id=None, category_id=None, status="all",
                        starred=None, after=None, before=None, want=200):
        """search/items/ids + items/contents；端点不可用则退化为本地标题过滤。"""
        if not self._search_unsupported:
            try:
                ids = self._search_ids(query, max(int(want) * 2, 200))
            except GReaderError as exc:
                if exc.status in (400, 404, 405, 501):
                    self._search_unsupported = True
                    log.info("greader 搜索端点不可用（HTTP %s），改用本地标题过滤", exc.status)
                else:
                    raise
            else:
                entries = [e for e in (self._safe_entry(it)
                                       for it in self._items_by_ids(ids)) if e]
                cat_map = None
                filtered = []
                for entry in entries:
                    if feed_id and entry.feed_id != int(feed_id):
                        continue
                    if category_id is not None:
                        if cat_map is None:
                            cat_map = self._feed_category_map()
                        if cat_map.get(entry.feed_id, None) != int(category_id):
                            continue
                    if status == "unread" and entry.status == "read":
                        continue
                    if status == "read" and entry.status != "read":
                        continue
                    if starred and not entry.starred:
                        continue
                    if not _within(entry, after, before):
                        continue
                    filtered.append(entry)
                return filtered

        # 退化路径：在对应流里按标题本地过滤（只扫有界窗口，不翻整个归档）
        return self._fetch_entries(feed_id=feed_id, category_id=category_id,
                                   starred=starred, status=status, after=after,
                                   before=before, want=want,
                                   title_match=query.lower())

    # ------------------------------------------------------------ 未读遍历
    def iter_unread(self, *, feed_id=None, category_id=None, starred=None,
                    max_pages=MAX_UNREAD_PAGES):
        stream = self._stream_for(feed_id, category_id, starred=bool(starred))
        starred_local = bool(starred and (feed_id or category_id is not None))
        if stream is None:
            for fid in self._uncategorized_feed_ids():
                yield from self._collect_entries(
                    f"feed/{fid}", status="unread", want=MAX_PAGE * max_pages,
                    starred_local=starred_local)
            return
        for raw in self._iter_items(stream, status="unread", page_size=MAX_PAGE,
                                    max_pages=max_pages,
                                    max_items=MAX_PAGE * max_pages):
            entry = self._safe_entry(raw)
            if entry is None:
                continue
            if starred_local and not entry.starred:
                continue
            yield entry

    # ------------------------------------------------------------ 写操作
    def _edit_tag(self, entry_ids, *, add=(), remove=()) -> int:
        ids = [int(i) for i in entry_ids]
        if not ids:
            return 0
        for chunk in _chunks(ids, WRITE_CHUNK):
            data = {"i": [_api_entry_id(i) for i in chunk]}
            if add:
                data["a"] = list(add)
            if remove:
                data["r"] = list(remove)
            self._post_action("/reader/api/0/edit-tag", data)
        self._invalidate()
        return len(ids)

    def mark_read(self, entry_ids) -> int:
        return self._edit_tag(entry_ids, add=[READ_STATE])

    def mark_unread(self, entry_ids) -> int:
        return self._edit_tag(entry_ids, remove=[READ_STATE])

    def toggle_star(self, entry_id, starred: bool) -> bool:
        # ★ 与 Miniflux 的 toggle 语义不同：GReader 的 edit-tag 是 set（a/r 加减），
        #   调用方仍按"先读当前状态再决定"用，这里只负责落到目标状态。
        if starred:
            self._edit_tag([entry_id], add=[STAR_STATE])
        else:
            self._edit_tag([entry_id], remove=[STAR_STATE])
        return True

    def _mark_all_as_read(self, stream, *, ts_usec):
        self._post_action("/reader/api/0/mark-all-as-read",
                          {"s": stream, "ts": str(int(ts_usec))})
        self._invalidate()

    def mark_scope_read(self, *, feed_id=None, category_id=None) -> int:
        if feed_id:
            stream = f"feed/{int(feed_id)}"
        elif category_id is not None:
            if int(category_id) == 0:
                for fid in self._uncategorized_feed_ids():
                    self.mark_scope_read(feed_id=fid)
                return -1
            stream = LABEL_PREFIX + self._label_for_id(category_id)
        else:
            raise ValueError("mark_scope_read 需要 feed_id 或 category_id")
        self._mark_all_as_read(stream, ts_usec=int(time.time() * 1_000_000))
        return -1  # 具体条数协议不返回

    def mark_read_before(self, *, days, feed_id=None, category_id=None,
                         starred=None) -> int:
        """把「该时刻之前」的条目标为已读。

        ★ ts 是 FreshRSS 的**条目 id**（微秒级摄取时间，见 lib_rss.php uTimeString），
          不是 published_at。因此严格说是"摄取时间早于 N 天前"，与 Miniflux 的
          published_at 口径有细微差别；按任务书的协议映射实现，报告里注明。
        """
        cutoff = datetime.now(dt_timezone.utc) - timedelta(days=int(days))
        cutoff_usec = int(cutoff.timestamp() * 1_000_000)

        # 星标 + 具体源/分类：协议没有交集流，只能本地筛未读后批量标
        if starred and (feed_id or category_id is not None):
            ids = [e.id for e in self.iter_unread(feed_id=feed_id,
                                                  category_id=category_id,
                                                  starred=True)
                   if _item_time(e) is None or _item_time(e) < cutoff]
            return self.mark_read(ids)

        stream = self._stream_for(feed_id, category_id, starred=bool(starred))
        if stream is None:  # category_id=0
            for fid in self._uncategorized_feed_ids():
                self.mark_scope_read(feed_id=fid)
            return -1
        if not feed_id and category_id is None and not starred:
            stream = READING_LIST
        self._mark_all_as_read(stream, ts_usec=cutoff_usec)
        return -1

    # ------------------------------------------------------------ 降级点
    def refresh_feed(self, feed_id) -> bool:
        # GReader 没有"单源立即刷新"端点；FreshRSS 按自己的抓取周期更新。
        # 记录 info 便于排障，返回 True 让调用方继续（不要伪造一次刷新）。
        log.info("greader 无单源刷新端点，跳过 feed=%s（等 FreshRSS 自身抓取周期）", feed_id)
        return True

    def fetch_content(self, entry_id, *, timeout=120) -> bool:
        # GReader 协议没有"抓原文全文"；由 FreshRSS 源配置的「抓取全文」解决。
        raise NotImplementedError(
            "GReader 数据源不支持抓取原文全文；请在 FreshRSS 里为该源开启「抓取全文」")

    # ------------------------------------------------------------ 分类 / feed 写入
    def create_category(self, title: str) -> dict:
        """只登记本地映射，**不**在 FreshRSS 侧创建空分类。

        GReader 没有"建空分类"端点；FreshRSS 的分类是随 feed 挂 label 才出现的。
        这里先给一个本地 id，等第一次有 feed 挂上来（create_feed）再落地。
        """
        title = (title or "").strip()
        if not title:
            raise ValueError("分类名不能为空")
        obj = self._touch_mapping(title, local_only=True, force_local_only=True)
        return {"id": obj.pk, "title": obj.label}

    def rename_category(self, category_id, title: str) -> dict:
        title = (title or "").strip()
        if not title:
            raise ValueError("分类名不能为空")
        cid = int(category_id)
        obj = CategoryLabel.objects.filter(pk=cid).first()
        if obj is None or not obj.active:
            raise GReaderError("LOCAL", "", None, f"未知分类 id={cid}")
        if title == obj.label:
            return {"id": obj.pk, "title": obj.label}
        if CategoryLabel.objects.filter(label=title).exclude(pk=obj.pk).exists():
            raise GReaderError("LOCAL", "", None, f"分类名已存在：{title}")

        if not obj.local_only:
            # ★ 任务书写 t=，但 FreshRSS 源码 rename-tag 只读 dest=
            #   （GReaderController: $_POST['dest']）。两个都发，兼容两种实现。
            self._post_action("/reader/api/0/rename-tag", {
                "s": LABEL_PREFIX + obj.label,
                "dest": LABEL_PREFIX + title,
                "t": LABEL_PREFIX + title,
            })
        obj.label = title
        obj.active = True
        obj.save(update_fields=["label", "active", "updated_at"])
        self._invalidate()
        return {"id": obj.pk, "title": title}

    def create_feed(self, feed_url: str, category_id) -> dict:
        """quickadd 新增订阅，再 subscription/edit 挂到目标分类。

        ★ quickadd 在 FreshRSS 里是**同步抓取**（addFeed），慢源可能 30s+ → 90s。
        ★ FreshRSS 一个 feed 只能属于一个分类：`a=user/-/label/X` 是"移动"语义。
        """
        feed_url = (feed_url or "").strip()
        if not feed_url:
            raise ValueError("Feed URL 不能为空")

        r = self._post_action("/reader/api/0/subscription/quickadd",
                              {"quickadd": feed_url}, timeout=90)
        self._invalidate()
        try:
            data = r.json()
        except ValueError:
            data = {}
        if not data or int(data.get("numResults") or 0) <= 0 or data.get("error"):
            raise GReaderError("POST", "/reader/api/0/subscription/quickadd",
                               r.status_code,
                               (data or {}).get("error") or "quickadd 未返回结果")

        feed_id = _feed_id_from_stream(data.get("streamId"))
        if not feed_id:
            # 兜底：按 URL 在新订阅表里找（部分实现 quickadd 不回 streamId）
            for sub in self._raw_subs():
                if sub.get("url") == feed_url:
                    feed_id = _feed_id_from_stream(sub.get("id"))
                    break
        if not feed_id:
            raise GReaderError("POST", "/reader/api/0/subscription/quickadd",
                               r.status_code, "没有拿到新 feed 的数字 id")

        if category_id:
            cid = int(category_id)
            if cid != 0:
                label = self._label_for_id(cid)
                self._post_action("/reader/api/0/subscription/edit", {
                    "s": f"feed/{feed_id}",
                    "ac": "edit",
                    "a": LABEL_PREFIX + label,
                })
                # 分类已真实存在：把本地 local_only 标记转正
                CategoryLabel.objects.filter(pk=cid).update(local_only=False)
        self._invalidate()
        return {"id": feed_id, "feed_id": feed_id,
                "title": html.unescape(data.get("streamName") or "") or feed_url}

    def rename_feed(self, feed_id, title: str) -> dict:
        """只改标题（UI 的 feed_rename 会调；不在 base.py 的接口里，沿用 Miniflux 的鸭子类型）。"""
        title = (title or "").strip()
        if not title:
            raise ValueError("源名称不能为空")
        fid = int(feed_id)
        # ★ FreshRSS subscription/edit 的标题参数是 t=（源码 multiplePosts('t')），
        #   不是 Google Reader 文档里的 title=。
        self._post_action("/reader/api/0/subscription/edit", {
            "s": f"feed/{fid}", "ac": "edit", "t": title})
        self._invalidate()
        return {"id": fid, "feed_id": fid, "title": title}

    def delete_feed(self, feed_id) -> bool:
        """取消订阅（**不可逆**：FreshRSS 会连该源条目一起删）。"""
        fid = int(feed_id)
        self._post_action("/reader/api/0/subscription/edit", {
            "s": f"feed/{fid}", "ac": "unsubscribe"})
        self._invalidate()
        return True

    def delete_category(self, category_id) -> bool:
        """摘掉 label（disable-tag），本地映射软删除。

        ★ 任务书允许"保留 id 标记失效"或删除；这里选软删除（active=False），
          理由见 CategoryLabel 的 docstring：避免 SQLite 自增 id 复用串号。
        """
        cid = int(category_id)
        obj = CategoryLabel.objects.filter(pk=cid).first()
        if obj is None or not obj.active:
            raise GReaderError("LOCAL", "", None, f"未知分类 id={cid}")
        if not obj.local_only:
            self._post_action("/reader/api/0/disable-tag", {
                "s": LABEL_PREFIX + obj.label})
        obj.active = False
        obj.local_only = False
        obj.save(update_fields=["active", "local_only", "updated_at"])
        self._invalidate()
        return True

    # ------------------------------------------------------------ 图标
    def icon(self, feed_id):
        fid = int(feed_id)
        key = f"gr:icon:{fid}"
        miss_key = f"gr:icon:miss:{fid}"
        hit = cache.get(key)
        if hit is not None:
            return hit
        # ★ 负缓存：图标失败（无 URL / 超时 / 404 / 非图片）不要每页重试；
        #   否则一次首屏会被十几个 15s 超时串起来。
        if ICON_MISS_TTL and cache.get(miss_key):
            return None

        def _miss(reason=""):
            if reason:
                log.debug("greader 取图标失败 feed=%s：%s（%ss 内不再重试）",
                          fid, reason, ICON_MISS_TTL)
            if ICON_MISS_TTL:
                cache.set(miss_key, True, ICON_MISS_TTL)
            return None

        try:
            subs = self._raw_subs()
        except GReaderError as exc:
            # 图标是非核心路径：FreshRSS 挂了也不该让 /icon 抛 502
            return _miss(str(exc))

        sub = next((s for s in subs
                    if _feed_id_from_stream(s.get("id")) == fid), None)
        url = (sub or {}).get("iconUrl") or ""
        if not url:
            return _miss()
        url = urljoin(self.base_url, url)
        if urlsplit(url).scheme not in ("http", "https"):
            return _miss(f"不支持的协议 {urlsplit(url).scheme!r}")

        # ★ 只对 FreshRSS 同主机请求带 Authorization；外部 icon 主机用干净 session，
        #   避免把 Auth token 发给第三方。
        same_host = urlsplit(url).netloc == urlsplit(self.base_url).netloc
        session = self.session if same_host else requests.Session()
        if not same_host:
            session.trust_env = False
        r = None
        try:
            # 同主机图标也走认证：会话过期时重登一次再取，不把 401 当成永久失败
            for attempt in range(2):
                if same_host and not self._auth_token:
                    self._login()
                r = session.get(url, timeout=15, stream=True)
                if same_host and r.status_code == 401 and attempt == 0:
                    r.close()
                    r = None
                    self._login(force=True)
                    continue
                break
            if r is None:
                return _miss("认证失败")
            r.raise_for_status()
            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if not ctype.startswith("image/"):
                return _miss(f"Content-Type={ctype or '空'}")
            declared = 0
            try:
                declared = int(r.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                declared = 0
            if ICON_MAX_BYTES and declared > ICON_MAX_BYTES:
                return _miss(f"图标过大（{declared} bytes）")
            blob = b""
            if hasattr(r, "iter_content"):
                for chunk in r.iter_content(64 * 1024):
                    if not chunk:
                        continue
                    blob += chunk
                    if ICON_MAX_BYTES and len(blob) > ICON_MAX_BYTES:
                        return _miss(f"图标超过 {ICON_MAX_BYTES} bytes")
            else:  # 极少数 stub/实现没有 iter_content
                blob = r.content or b""
                if ICON_MAX_BYTES and len(blob) > ICON_MAX_BYTES:
                    return _miss(f"图标超过 {ICON_MAX_BYTES} bytes")
        except Exception as exc:  # noqa: BLE001  图标失败不该影响页面
            return _miss(str(exc))
        finally:
            if r is not None:
                try:
                    r.close()
                except Exception:  # noqa: BLE001
                    pass
        if not blob:
            return _miss("空响应")
        got = (ctype, blob)
        cache.set(key, got, 86400)
        if ICON_MISS_TTL:
            cache.delete(miss_key)
        return got
