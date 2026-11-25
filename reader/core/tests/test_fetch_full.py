"""文章右键「抓取全文」端点（fetch original content，2026-11-20）。

判据（都走真实 view + 真 ORM，只 mock 掉 Miniflux 网络层）：
  ★ 覆盖前必须二次确认（need_confirm + confirm=1，与日报重跑同契约）：
    ① 抓过全文 → 拦；② 有划线/批注 → marks.rerun_warning 拦；两者合并一条 msg。
    后端权威：confirm=1 才放行，改包请求绕不过。
  ★ 不存正文副本（仓库硬约束）：只记状态与长度变化。
  ★ 抓取失败给出可读原因（上游 error_message），不静默。

★ 为什么不用 Client()：真实 settings 的 ALLOWED_HOSTS 会挡 testserver
  （见 MEMORY.md 坑），这里用 RequestFactory 直接打 view，专注端点契约。
"""
import json
from unittest import mock

from django.test import RequestFactory, TestCase
from django.utils import timezone

from apps.reader import views as rv
from core import marks
from core.models import EntryFullFetch

EID = 91001
PATH = "/hx/fetch-full/%s" % EID


class _Entry:
    def __init__(self, content="一句话摘要" * 3):
        self.id = EID
        self.content = content


class _DS:
    """只实现 fetch-full 端点会碰的两件事：get_entry（取长度）+ fetch_content。"""

    def __init__(self, content="一句话摘要" * 3, boom=None):
        self.content = content
        self.boom = boom
        self.fetch_calls = []

    def get_entry(self, _id):
        return _Entry(self.content)

    def fetch_content(self, entry_id, *, timeout=120):
        self.fetch_calls.append((entry_id, timeout))
        if self.boom:
            raise self.boom


def _body(resp):
    """RequestFactory 直调返回 JsonResponse（无 .json()），从 content 解。"""
    return json.loads(resp.content.decode("utf-8"))


def _post(ds, **fields):
    req = RequestFactory().post(PATH, fields)
    with mock.patch.object(rv, "get_datasource", return_value=ds):
        return _body(rv.hx_fetch_full(req, EID))


class FetchFullTest(TestCase):
    def tearDown(self):
        EntryFullFetch.objects.all().delete()
        marks.Highlight.objects.all().delete()

    def test_first_fetch_goes_through_and_records_state(self):
        """首次抓取：无确认、真调用 fetch_content、只记状态不存正文。"""
        ds = _DS()
        body = _post(ds)
        self.assertTrue(body["ok"])
        self.assertEqual(len(ds.fetch_calls), 1)
        self.assertEqual(ds.fetch_calls[0][0], EID)
        self.assertGreaterEqual(ds.fetch_calls[0][1], 120)  # 慢站要长超时
        row = EntryFullFetch.objects.get(entry_id=EID)
        self.assertGreater(row.content_len_after, 0)

    def test_refetch_without_confirm_is_blocked(self):
        """抓过全文：不带 confirm 再抓 → need_confirm，且不调上游。"""
        EntryFullFetch.objects.create(entry_id=EID, fetched_at=timezone.now())
        ds = _DS()
        body = _post(ds)
        self.assertFalse(body["ok"])
        self.assertTrue(body["need_confirm"])
        self.assertIn("抓过全文", body["msg"])
        self.assertEqual(len(ds.fetch_calls), 0)  # ★ 拦下就没打源站

    def test_refetch_with_confirm_goes_through(self):
        """带 confirm=1 是契约的一部分：放行，且真的重抓。"""
        EntryFullFetch.objects.create(entry_id=EID, fetched_at=timezone.now())
        ds = _DS()
        body = _post(ds, confirm="1")
        self.assertTrue(body["ok"])
        self.assertEqual(len(ds.fetch_calls), 1)

    def test_highlight_blocks_then_confirm_passes(self):
        """有划线 → 同一份 msg 里带锚点警告；确认后放行（警告不是禁令）。"""
        marks.add_highlight("entry", str(EID), "被划的原句", note="")
        ds = _DS()
        blocked = _post(ds)
        self.assertTrue(blocked["need_confirm"])
        self.assertIn("划线", blocked["msg"])
        self.assertEqual(len(ds.fetch_calls), 0)
        passed = _post(ds, confirm="1")
        self.assertTrue(passed["ok"])
        self.assertEqual(len(ds.fetch_calls), 1)

    def test_upstream_error_is_readable(self):
        """上游报错 → ok=false + 人话，不是「未知错误」。"""
        class _Boom(Exception):
            response = None

        exc = _Boom("miniflux: upstream 502 bad gateway")
        ds = _DS(boom=exc)
        body = _post(ds)
        self.assertFalse(body["ok"])
        self.assertNotIn("未知错误", body.get("error", ""))
        self.assertIn("抓取全文失败", body["error"])

    def test_no_body_copy_written_locally(self):
        """★ 仓库硬约束：本地只记长度，绝不把全文正文写进任何本地表。"""
        long_text = "这是源站抓回来的长正文。" * 500
        ds = _DS(content=long_text)
        body = _post(ds)
        self.assertTrue(body["ok"])
        row = EntryFullFetch.objects.get(entry_id=EID)
        # 模型字段里没有任何存正文的列
        cols = {f.name for f in EntryFullFetch._meta.get_fields()}
        self.assertNotIn("content", cols)
        self.assertNotIn("body", cols)
        self.assertEqual(row.content_len_after, len(long_text))


class MinifluxFetchContentTest(TestCase):
    """★★ 真实网络语义：钉死官方 API 的两个坑（GET + update_content=true）。

    曾在实现时凭直觉写成 `PUT /fetch-content`，查官方文档才发现是 GET，
    且默认 update_content=false 只返回全文不写库 —— 那种实现跑起来
    "没报错但正文没变"，是最贵的静默失败。这里用假 session 捕获真实调用，
    让"方法/路径/参数"三者都有行为级守卫（静态守卫只守源码形状）。
    """

    def test_uses_get_with_update_content_true(self):
        from core.datasource.miniflux import MinifluxDataSource

        ds = MinifluxDataSource.__new__(MinifluxDataSource)
        ds.base_url = "http://miniflux.example"
        calls = []

        class _Resp:
            status_code = 200
            content = b'{"content":"<p>full</p>"}'

            def raise_for_status(self):
                pass

            def json(self):
                return {"content": "<p>full</p>"}

        class _Session:
            def get(self, url, params=None, timeout=None):
                calls.append(("GET", url, dict(params or {}), timeout))
                return _Resp()

        ds.session = _Session()
        ds._invalidate = mock.Mock()
        self.assertTrue(ds.fetch_content(91001))

        method, url, params, timeout = calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "http://miniflux.example/v1/entries/91001/fetch-content")
        self.assertEqual(params.get("update_content"), "true")  # ★ 否则不写库
        self.assertGreaterEqual(timeout, 120)                    # ★ 慢站要长超时
        ds._invalidate.assert_called_once_with()
