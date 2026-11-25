"""GReader 数据源的纯逻辑单测（不联网）。

覆盖最容易写错、又不需要真实 FreshRSS 的几处：
  · 条目 id：完整 tag（16 位 hex）↔ int64 ↔ `i=` 的完整 tag；
  · item → Entry：正文/已读/星标/origin/发布时间；
  · label ↔ 本地 int id 的持久映射（重启后 id 不变）；
  · P1：错误人话、401 重登、写 token 重试、continuation 防死循环、
    条目流缓存失效、icon 负缓存。
"""
from datetime import datetime, timezone as dt_timezone
from unittest import mock

import requests
from django.core.cache import cache
from django.test import RequestFactory, TestCase

from core.datasource.base import Entry
from core.datasource.greader import (
    GReaderDataSource, GReaderError, READ_STATE, STAR_STATE,
    _api_entry_id, _parse_entry_id,
)
from core.middleware import DataSourceErrorMiddleware
from core.models import CategoryLabel


class EntryIdTests(TestCase):
    def test_hex_tag_roundtrip(self):
        raw = "tag:google.com,2005:reader/item/00000000abcdef01"
        got = _parse_entry_id(raw)
        self.assertEqual(got, 0xABCDEF01)
        self.assertEqual(_api_entry_id(got), raw)

    def test_decimal_item_ref(self):
        # FreshRSS 的 stream/items/ids 返回 64 位十进制
        self.assertEqual(_parse_entry_id("1844674407370955161"),
                         1844674407370955161)

    def test_bare_hex(self):
        self.assertEqual(_parse_entry_id("abcdef01"), 0xABCDEF01)

    def test_bad_id_raises(self):
        from core.datasource.greader import GReaderError
        with self.assertRaises(GReaderError):
            _parse_entry_id("not-an-id")


class EntryConvertTests(TestCase):
    def test_item_to_entry(self):
        raw = {
            "id": "tag:google.com,2005:reader/item/00000000abcdef02",
            "published": 1700000000,
            "title": "标题 &amp; 实体",
            "categories": [READ_STATE, STAR_STATE, "user/-/label/科技"],
            "origin": {"streamId": "feed/52", "title": "来源"},
            "content": {"content": "<p>正文</p>"},
            "alternate": [{"href": "https://example.com/a"}],
            "author": "作者",
        }
        e = GReaderDataSource._to_entry(raw)
        self.assertEqual(e.id, 0xABCDEF02)
        self.assertEqual(e.feed_id, 52)
        self.assertEqual(e.feed_title, "来源")
        self.assertEqual(e.title, "标题 & 实体")
        self.assertEqual(e.status, "read")
        self.assertTrue(e.starred)
        self.assertEqual(e.content, "<p>正文</p>")
        self.assertEqual(e.url, "https://example.com/a")
        self.assertIsNotNone(e.published_at)


class MappingTests(TestCase):
    def setUp(self):
        CategoryLabel.objects.all().delete()
        self.ds = GReaderDataSource(base_url="http://example.test/api/greader.php",
                                    username="u", password="p")

    def test_label_id_is_persistent_and_rename_keeps_id(self):
        first = self.ds._touch_mapping("科技")
        again = GReaderDataSource(
            base_url="http://example.test/api/greader.php", username="u",
            password="p")._touch_mapping("科技")
        self.assertEqual(first.pk, again.pk)
        # 改名保留 id（rename_category 在 local_only 时不打服务端）
        obj = CategoryLabel.objects.create(label="本地新建", local_only=True)
        data = self.ds.rename_category(obj.pk, "本地改名")
        self.assertEqual(data["id"], obj.pk)
        self.assertEqual(CategoryLabel.objects.get(pk=obj.pk).label, "本地改名")

    def test_label_stream_is_url_encoded(self):
        obj = self.ds._touch_mapping("科技")
        stream = self.ds._stream_for(category_id=obj.pk)
        assert stream.startswith("user/-/label/")
        self.assertIn("%E7%A7%91%E6%8A%80", stream)


class _Resp:
    """requests.Response 的最小替身。"""

    def __init__(self, status=200, text="", *, json=None, content=None,
                 headers=None, json_error=False):
        self.status_code = status
        self.ok = status < 400
        self.text = text
        self._json = json
        self._json_error = json_error
        if content is not None:
            self.content = content
        elif json is not None:
            self.content = b"{}"
        else:
            self.content = (text or "").encode()
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._json if self._json is not None else {}

    def raise_for_status(self):
        if not self.ok:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def close(self):
        self.closed = True


class ErrorMessageTests(TestCase):
    def setUp(self):
        self.ds = GReaderDataSource(base_url="http://freshrss.test/api/greader.php",
                                    username="u", password="p")

    def test_wrong_password_is_explicit(self):
        self.ds.session.post = mock.Mock(return_value=_Resp(401, text="Unauthorized"))
        with self.assertRaises(GReaderError) as cm:
            self.ds._login()
        msg = str(cm.exception)
        self.assertIn("API 密码", msg)
        self.assertIn("HTTP 401", msg)

    def test_unreachable_is_explicit(self):
        self.ds._auth_token = "cached"
        self.ds.session.request = mock.Mock(side_effect=requests.ConnectionError("boom"))
        with self.assertRaises(GReaderError) as cm:
            self.ds._json("GET", "/reader/api/0/subscription/list")
        msg = str(cm.exception)
        self.assertIn("无法连接 FreshRSS", msg)
        self.assertIn("GREADER_BASE_URL", msg)

    def test_html_instead_of_json_is_explicit(self):
        self.ds._auth_token = "cached"
        self.ds.session.request = mock.Mock(
            return_value=_Resp(200, text="<html>login</html>", json_error=True))
        with self.assertRaises(GReaderError) as cm:
            self.ds._json("GET", "/reader/api/0/subscription/list")
        msg = str(cm.exception)
        self.assertIn("不是 JSON", msg)
        self.assertNotIn("<html>", msg)  # 标签被剥掉，不把整页 HTML 塞给用户

    def test_401_relogin_only_once(self):
        self.ds._auth_token = "expired"
        self.ds._login = mock.Mock()
        self.ds.session.request = mock.Mock(side_effect=[
            _Resp(401, text="expired"),
            _Resp(200, json={"subscriptions": []}),
        ])
        data = self.ds._json("GET", "/reader/api/0/subscription/list")
        assert data == {"subscriptions": []}
        # 第一次调 _login() 是常规检查；401 后只强登一次（递归里再调一次但已缓存）
        forces = [c for c in self.ds._login.call_args_list if c.kwargs.get("force")]
        assert len(forces) == 1
        assert self.ds.session.request.call_count == 2

    def test_401_after_relogin_says_permission(self):
        self.ds._auth_token = "expired"
        self.ds._login = mock.Mock()
        self.ds.session.request = mock.Mock(return_value=_Resp(401, text="nope"))
        with self.assertRaises(GReaderError) as cm:
            self.ds._json("GET", "/reader/api/0/subscription/list")
        assert "重新登录后仍被拒绝" in str(cm.exception)
        assert self.ds.session.request.call_count == 2

    def test_post_token_retry_once(self):
        self.ds._post_token_value = mock.Mock(side_effect=["T1", "T2"])
        self.ds._request = mock.Mock(side_effect=[
            GReaderError("POST", "/reader/api/0/edit-tag", 403, "stale token"),
            _Resp(200, json={"ok": True}),
        ])
        self.ds._post_action("/reader/api/0/edit-tag", {"i": "x"})
        assert self.ds._request.call_count == 2
        assert self.ds._request.call_args_list[0].kwargs["data"]["T"] == "T1"
        assert self.ds._request.call_args_list[1].kwargs["data"]["T"] == "T2"


class ContinuationTests(TestCase):
    def setUp(self):
        self.ds = GReaderDataSource(base_url="http://freshrss.test/api/greader.php",
                                    username="u", password="p")

    def test_repeated_continuation_stops(self):
        # 坏服务端一直回同一个 continuation：必须停，不能无限翻同一页
        self.ds._json = mock.Mock(return_value={
            "items": [{"id": "tag:google.com,2005:reader/item/0000000000000001"}],
            "continuation": "same-token",
        })
        items = list(self.ds._iter_items("feed/1", max_pages=50))
        assert len(items) == 2
        assert self.ds._json.call_count == 2

    def test_max_pages_and_max_items_caps(self):
        self.ds._json = mock.Mock(side_effect=[
            {"items": [{"id": f"tag:google.com,2005:reader/item/{i:016x}"} for i in range(5)],
             "continuation": f"c{i}"} for i in range(10)
        ])
        items = list(self.ds._iter_items("feed/1", max_pages=3, max_items=8))
        assert len(items) == 8
        assert self.ds._json.call_count == 2


class EntryCacheTests(TestCase):
    def setUp(self):
        self.ds = GReaderDataSource(base_url="http://freshrss.test/api/greader.php",
                                    username="u", password="p")
        now = datetime(2026, 11, 25, tzinfo=dt_timezone.utc)
        self.entry = Entry(id=1, feed_id=1, feed_title="f", title="t", url="",
                           published_at=now, created_at=now)

    def test_same_query_is_cached_until_invalidate(self):
        self.ds._fetch_entries = mock.Mock(return_value=[self.entry])
        first = self.ds.list_entries(feed_id=1, limit=10)
        second = self.ds.list_entries(feed_id=1, limit=10)
        assert first[0].id == second[0].id == 1
        assert self.ds._fetch_entries.call_count == 1
        self.ds._invalidate()   # 标已读/星标等写操作会走这里
        self.ds.list_entries(feed_id=1, limit=10)
        assert self.ds._fetch_entries.call_count == 2

    def test_large_windows_are_not_cached(self):
        self.ds._fetch_entries = mock.Mock(return_value=[self.entry])
        self.ds.list_entries(feed_id=1, limit=1000)
        self.ds.list_entries(feed_id=1, limit=1000)
        assert self.ds._fetch_entries.call_count == 2


class IconTests(TestCase):
    def setUp(self):
        cache.clear()
        self.ds = GReaderDataSource(base_url="http://freshrss.test/api/greader.php",
                                    username="u", password="p")
        self.ds._raw_subs = mock.Mock(return_value=[{
            "id": "feed/5", "iconUrl": "https://icons.example/x.png"}])

    def tearDown(self):
        cache.clear()

    def test_failure_is_negatively_cached(self):
        with mock.patch("requests.Session.get",
                        side_effect=requests.Timeout("slow")) as get:
            assert self.ds.icon(5) is None
            assert self.ds.icon(5) is None
        assert get.call_count == 1

    def test_success_is_cached_and_capped(self):
        resp = _Resp(200, content=b"\x89PNGok", headers={"Content-Type": "image/png"})
        resp.iter_content = lambda size: iter([resp.content])
        with mock.patch("requests.Session.get", return_value=resp) as get:
            assert self.ds.icon(5) == ("image/png", b"\x89PNGok")
            assert self.ds.icon(5) == ("image/png", b"\x89PNGok")
        assert get.call_count == 1
        assert resp.closed

    def test_non_image_is_negatively_cached(self):
        resp = _Resp(200, content=b"<html>", headers={"Content-Type": "text/html"})
        resp.iter_content = lambda size: iter([resp.content])
        with mock.patch("requests.Session.get", return_value=resp) as get:
            assert self.ds.icon(5) is None
            assert self.ds.icon(5) is None
        assert get.call_count == 1

    def test_unreachable_does_not_raise(self):
        self.ds._raw_subs = mock.Mock(side_effect=GReaderError(
            "GET", "/x", None, "无法连接 FreshRSS"))
        assert self.ds.icon(5) is None


class MiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def _mw(self, exc):
        def boom(request):
            raise exc
        return DataSourceErrorMiddleware(boom)

    def test_page_gets_friendly_502(self):
        req = self.factory.get("/")
        resp = self._mw(None).process_exception(
            req, GReaderError("GET", "/reader/api/0/unread-count", None,
                              "无法连接 FreshRSS"))
        assert resp.status_code == 502
        body = resp.content.decode()
        assert "数据源不可用" in body
        assert "无法连接 FreshRSS" in body
        assert "Traceback" not in body

    def test_json_endpoint_gets_json(self):
        import json as _json
        req = self.factory.post("/tree/feed/create")
        resp = self._mw(None).process_exception(
            req, GReaderError("POST", "/x", 401, "密码错误"))
        assert resp.status_code == 502
        payload = _json.loads(resp.content)
        assert payload["ok"] is False
        assert "密码错误" in payload["error"]

    def test_htmx_gets_200_banner(self):
        req = self.factory.get("/hx/tree", HTTP_HX_REQUEST="true")
        resp = self._mw(None).process_exception(
            req, GReaderError("GET", "/x", None, "无法连接 FreshRSS"))
        # htmx 默认不交换非 2xx：200 才能让人真的看到错误
        assert resp.status_code == 200
        assert "数据源不可用" in resp.content.decode()

    def test_other_exceptions_are_not_swallowed(self):
        req = self.factory.get("/")
        resp = self._mw(None).process_exception(req, ValueError("boom"))
        assert resp is None
