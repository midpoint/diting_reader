"""应用内 .env 设置：合并写、密钥不回显、整组保存、高级原文保存。

★ 所有用例都把 `envfile.env_path` 指到 tmp 目录 —— 绝不能碰仓库根目录的真 `.env`。
"""
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings

from core import envfile


class EnvFileTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Path(self.tmp.name) / ".env"
        p = mock.patch.object(envfile, "env_path", return_value=self.env)
        p.start()
        self.addCleanup(p.stop)

    def test_merge_preserves_comments_and_order(self):
        self.env.write_text("# 说明\nA=1\n\nB=2\nB=old\nC=3\n", encoding="utf-8")
        out = envfile.merge(self.env.read_text(), {"B": "9", "D": "4"})
        self.assertEqual(out, "# 说明\nA=1\n\nB=9\nC=3\nD=4\n")

    def test_write_is_0600(self):
        envfile.write_updates({"GREADER_PASSWORD": "s3cret"})
        self.assertEqual(envfile.read_values()["GREADER_PASSWORD"], "s3cret")
        self.assertEqual(os.stat(self.env).st_mode & 0o777, 0o600)

    def test_newline_rejected(self):
        with self.assertRaises(ValueError):
            envfile.write_updates({"A": "x\ny"})


@override_settings(ALLOWED_HOSTS=["*"])
class PrefsEnvViewTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = Path(self.tmp.name) / ".env"
        self.env.write_text(
            "# 用户注释\nDATASOURCE_ENGINE=miniflux\n"
            "GREADER_PASSWORD=old-secret\nDEEPSEEK_API_KEY=sk-test-secret\n",
            encoding="utf-8")
        p = mock.patch.object(envfile, "env_path", return_value=self.env)
        p.start()
        self.addCleanup(p.stop)

        snapshot = {k: getattr(settings, k) for k in (
            "DATASOURCE_ENGINE", "GREADER_BASE_URL", "GREADER_USERNAME",
            "GREADER_PASSWORD", "MINIFLUX_BASE_URL", "MINIFLUX_USERNAME",
            "MINIFLUX_PASSWORD", "LLM_PROVIDER", "DEEPSEEK_API_KEY")}
        def restore():
            for k, v in snapshot.items():
                setattr(settings, k, v)
        self.addCleanup(restore)

    def post(self, url, data):
        return self.client.post(url, data)

    def test_page_renders_without_secret(self):
        r = self.client.get("/prefs")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode()
        self.assertIn("数据源", body)
        self.assertIn("高级：整份 .env", body)
        self.assertNotIn("old-secret", body)
        self.assertNotIn("sk-test-secret", body)
        self.assertIn("已设置", body)

    def test_save_datasource_hot_applies(self):
        with mock.patch.object(envfile, "reset_datasource") as reset:
            r = self.post("/prefs/env", {
                "DATASOURCE_ENGINE": "greader",
                "GREADER_BASE_URL": "https://rss.example/api/greader.php",
                "GREADER_USERNAME": "alice",
                "GREADER_PASSWORD": "new-secret",
            })
        self.assertEqual(r.status_code, 200)
        self.assertTrue(json.loads(r.content)["ok"])
        vals = envfile.read_values()
        self.assertEqual(vals["DATASOURCE_ENGINE"], "greader")
        self.assertEqual(vals["GREADER_PASSWORD"], "new-secret")
        self.assertIn("# 用户注释", self.env.read_text())
        self.assertEqual(settings.DATASOURCE_ENGINE, "greader")
        self.assertEqual(settings.GREADER_USERNAME, "alice")
        reset.assert_called_once()

    def test_blank_password_keeps_old(self):
        r = self.post("/prefs/env", {
            "DATASOURCE_ENGINE": "greader",
            "GREADER_BASE_URL": "https://rss.example/api/greader.php",
            "GREADER_USERNAME": "bob",
            "GREADER_PASSWORD": "",
        })
        self.assertTrue(json.loads(r.content)["ok"])
        self.assertEqual(envfile.read_values()["GREADER_PASSWORD"], "old-secret")

    def test_clear_password(self):
        r = self.post("/prefs/env", {
            "DATASOURCE_ENGINE": "miniflux",
            "MINIFLUX_URL": "http://localhost:8080",
            "GREADER_PASSWORD__clear": "1",
        })
        self.assertTrue(json.loads(r.content)["ok"])
        self.assertEqual(envfile.read_values()["GREADER_PASSWORD"], "")

    def test_engine_requires_credentials(self):
        r = self.post("/prefs/env", {
            "DATASOURCE_ENGINE": "greader",
            "GREADER_BASE_URL": "",
            "GREADER_USERNAME": "",
        })
        body = json.loads(r.content)
        self.assertFalse(body["ok"])
        self.assertIn("FreshRSS", body["err"])
        self.assertEqual(envfile.read_values()["DATASOURCE_ENGINE"], "miniflux")

    def test_bad_url_rejected(self):
        r = self.post("/prefs/env", {
            "DATASOURCE_ENGINE": "greader",
            "GREADER_BASE_URL": "rss.example/api/greader.php",
            "GREADER_USERNAME": "a",
            "GREADER_PASSWORD": "b",
        })
        self.assertIn("http", json.loads(r.content)["err"])

    def test_url_trailing_slash_normalized(self):
        r = self.post("/prefs/env", {
            "DATASOURCE_ENGINE": "GREADER",
            "GREADER_BASE_URL": "https://rss.example/api/greader.php/",
            "GREADER_USERNAME": "a",
            "GREADER_PASSWORD": "b",
        })
        self.assertTrue(json.loads(r.content)["ok"])
        vals = envfile.read_values()
        self.assertEqual(vals["DATASOURCE_ENGINE"], "greader")
        self.assertEqual(vals["GREADER_BASE_URL"],
                         "https://rss.example/api/greader.php")
        self.assertEqual(settings.GREADER_BASE_URL,
                         "https://rss.example/api/greader.php")

    def test_test_endpoint_uses_datasource(self):
        class FakeDS:
            def list_tree(self):
                class C:
                    feeds = [1, 2]
                return [C()]
        with mock.patch("core.datasource.new_datasource", return_value=FakeDS()):
            r = self.post("/prefs/env/test", {})
        body = json.loads(r.content)
        self.assertTrue(body["ok"])
        self.assertIn("2 个源", body["message"])

    def test_raw_roundtrip(self):
        text = "# c\nDATASOURCE_ENGINE=greader\nGREADER_BASE_URL=https://h/api/greader.php\n"
        r = self.post("/prefs/env/raw/save", {"text": text})
        self.assertTrue(json.loads(r.content)["ok"])
        self.assertEqual(self.env.read_text(), text)
        self.assertEqual(settings.DATASOURCE_ENGINE, "greader")

        r = self.post("/prefs/env/raw/save", {"text": "这不是配置\n"})
        self.assertFalse(json.loads(r.content)["ok"])
        self.assertEqual(self.env.read_text(), text)
