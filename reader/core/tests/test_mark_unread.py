"""文章右键「标记为未读」（2026-11-24）。

修的是一条**不可逆的小事故**：谛听点开即标已读，误点开之后没有反向入口，
只能去 Miniflux 网页自己改。判据（都走真 view / 真 datasource 代码，只桩网络层）：
  1. 写回真相层：`PUT /v1/entries {"entry_ids":[…], "status":"unread"}`
     —— 与标已读共用**一个**写方法，只差 status，不许复制出第二份口径。
  2. 写回失败要给上游人话，不许静默成功；不存在的条目也不能当成功。
  3. 端点只收 POST（GET 有副作用 = 预取/爬虫误触发）。

★ 为什么不用 Client()：真实 settings 的 ALLOWED_HOSTS 会挡 testserver，
  这里用 RequestFactory 直接打 view（同 test_fetch_full.py）。
"""
import json
from unittest import mock

from django.test import RequestFactory, TestCase

from apps.reader import views as rv
from core.datasource.miniflux import MinifluxDataSource

EID = 92001
PATH = "/hx/mark-unread/%s" % EID


def _ds():
    return MinifluxDataSource(base_url="http://miniflux.local",
                              username="u", password="p")


class DatasourceTest(TestCase):
    def test_mark_unread_writes_the_same_endpoint_as_read(self):
        """★ 唯一写口：status=unread 打到 /v1/entries，并让树缓存失效。"""
        ds = _ds()
        with mock.patch.object(ds, "_put") as put, \
             mock.patch("core.datasource.miniflux.MinifluxDataSource._invalidate") as inv:
            n = ds.mark_unread([EID, "92002"])
        self.assertEqual(n, 2)
        put.assert_called_once_with("/v1/entries", {
            "entry_ids": [EID, 92002], "status": "unread"})
        inv.assert_called_once_with()

    def test_read_and_unread_share_one_writer(self):
        """★ 两份实现各抄一遍 = 以后改缓存失效只改一处，静默分叉。"""
        ds = _ds()
        with mock.patch.object(ds, "_put") as put:
            ds.mark_read([1])
            ds.mark_unread([2])
        self.assertEqual([c.args for c in put.call_args_list], [
            ("/v1/entries", {"entry_ids": [1], "status": "read"}),
            ("/v1/entries", {"entry_ids": [2], "status": "unread"}),
        ])

    def test_empty_ids_do_not_touch_upstream(self):
        ds = _ds()
        with mock.patch.object(ds, "_put") as put:
            self.assertEqual(ds.mark_unread([]), 0)
        put.assert_not_called()


class _Entry:
    id = EID


class _DS:
    def __init__(self, missing=False, boom=None):
        self.missing = missing
        self.boom = boom
        self.unread_calls = []

    def get_entry(self, entry_id):
        if self.missing:
            return None
        return _Entry()

    def mark_unread(self, ids):
        self.unread_calls.extend(ids)
        if self.boom:
            raise self.boom


def _body(resp):
    return json.loads(resp.content.decode("utf-8"))


def _post(ds, method="post"):
    req = getattr(RequestFactory(), method)(PATH, {})
    with mock.patch.object(rv, "get_datasource", return_value=ds):
        return rv.hx_mark_unread(req, EID)


class MarkUnreadViewTest(TestCase):
    def test_success_writes_once_and_returns_json(self):
        ds = _DS()
        body = _body(_post(ds))
        self.assertTrue(body["ok"])
        self.assertEqual(ds.unread_calls, [EID])
        self.assertEqual(body["entry_id"], EID)

    def test_missing_entry_is_not_reported_as_success(self):
        ds = _DS(missing=True)
        body = _body(_post(ds))
        self.assertFalse(body["ok"])
        self.assertIn("条目不存在", body["error"])
        self.assertEqual(ds.unread_calls, [])

    def test_upstream_error_is_readable(self):
        ds = _DS(boom=RuntimeError("miniflux: upstream 502"))
        body = _body(_post(ds))
        self.assertFalse(body["ok"])
        self.assertIn("标记为未读失败", body["error"])
        self.assertIn("upstream 502", body["error"])

    def test_get_is_rejected(self):
        """GET 不能改已读状态（预取 / 爬虫 / 链接重放都不许触发写）。"""
        ds = _DS()
        resp = _post(ds, method="get")
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(ds.unread_calls, [])
