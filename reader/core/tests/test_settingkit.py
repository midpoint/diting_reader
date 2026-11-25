"""settingkit：三层配置优先级 / 类型校验 / 写入即生效（含缓存失效）。"""
from django.test import TestCase

from core import settingkit as sk
from core.models import AppSetting


class RegistryTest(TestCase):
    def test_every_key_maps_to_a_settings_name(self):
        # 注册表 key 必须与 settings.py 的同名项一一对应（防拼错静默回落到零值）
        from django.conf import settings
        missing = [f.key for g in sk.GROUPS for f in g.fields
                   if not hasattr(settings, f.key.upper())]
        self.assertEqual(missing, [], f"注册表里有 settings.py 不存在的项：{missing}")

    def test_unknown_key_raises(self):
        with self.assertRaises(KeyError):
            sk.effective("no_such_key")


class EffectiveTest(TestCase):
    def setUp(self):
        # effective 有 15s 本地缓存，跨用例必须清（否则读到上一个用例的值）
        from django.core.cache import cache
        cache.clear()
        AppSetting.objects.all().delete()

    def test_env_fallback_when_no_row(self):
        # 无 AppSetting 行 → 用 .env / settings 默认
        self.assertEqual(sk.effective("triage_repeat"), 3)
        self.assertTrue(sk.effective("triage_adaptive"))

    def test_appsetting_overrides_env(self):
        ok, _ = sk.write("triage_repeat", "1")
        self.assertTrue(ok)
        self.assertEqual(sk.effective("triage_repeat"), 1)

    def test_write_invalid_value_rejected(self):
        ok, err = sk.write("triage_repeat", "9")      # 超过 maximum=5
        self.assertFalse(ok)
        self.assertIn("投票次数", err)
        self.assertFalse(AppSetting.objects.filter(key="triage_repeat").exists())

    def test_write_then_effective_updates_immediately(self):
        ok, err = sk.write("summary_hour", "8")
        self.assertTrue(ok, err)
        self.assertEqual(sk.effective("summary_hour"), 8)
        ok, _ = sk.write("notify_enable", "0")
        self.assertTrue(ok)
        self.assertFalse(sk.effective("notify_enable"))

    def test_bool_and_string_roundtrip(self):
        ok, _ = sk.write("notify_sound", "Glass")
        self.assertTrue(ok)
        self.assertEqual(sk.effective("notify_sound"), "Glass")
        ok, _ = sk.write("notify_ignore_dnd", "true")
        self.assertTrue(ok)
        self.assertTrue(sk.effective("notify_ignore_dnd"))

    def test_reset_falls_back_to_env(self):
        sk.write("summary_hour", "9")
        sk.reset("summary_hour")
        self.assertEqual(sk.effective("summary_hour"), 7)   # .env 默认
