"""受控 domain 清单缓存的反证（2026-11-05）。

本体改动极低频，所以 `ontology_domains()` 进程内缓存 + stat 指纹快路径；
但缓存最贵的不是慢，是**静默**：本体换了却还用旧清单，模型照样花钱、
照样按旧 domain 落库，日志一行不红。这里的每条测试都是冲着这个去的。

★ 一条判据写三遍必漏一遍：本文件不复制解析规则，只验「缓存的边界」。
★ `test_rewinding_mtime_cannot_hide_a_content_change` 是这套实现的定海神针：
  把 `_ontology_fingerprint()` 里的 `st_ctime_ns` 删掉，它立刻变红。
"""
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from core import wiki as wstore

#: 两份本体**长度必须严格相同**：测试要构造「同 inode、同 size、同 mtime，
#: 只有内容不同」这种最刁钻的漏检场景。
GOOD_A = "# 本体\n\n## 领域\n\n| 域 | 标签 |\n| --- | --- |\n| A | `#aaaaa` |\n"
GOOD_B = "# 本体\n\n## 领域\n\n| 域 | 标签 |\n| --- | --- |\n| A | `#bbbbb` |\n"
BROKEN = "# 没有领域表\n\n随手一个 `#ghost` 标签\n"


class OntologyCacheTest(SimpleTestCase):
    """全部走临时文件 + `path=`，不碰真实的 `Meta/ontology.md`。"""

    def setUp(self):
        # 缓存是模块级全局，测试间不串味；真实本体的条目被清掉也只是下次重读一次。
        wstore._ONTOLOGY_CACHE.clear()
        self.tmp = tempfile.mkdtemp(prefix="diting-ontology-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(wstore._ONTOLOGY_CACHE.clear)
        self.p = Path(self.tmp) / "ontology.md"
        self.p.write_text(GOOD_A, encoding="utf-8")

    def test_two_texts_have_equal_length(self):
        """前提守卫：不等长的话，「回拨 mtime」那条测试就白写了。"""
        self.assertEqual(len(GOOD_A.encode("utf-8")), len(GOOD_B.encode("utf-8")))

    def test_cache_hit_skips_both_read_and_parse(self):
        """★ 桩真被用上的反证：连读 3 次，文件只读 1 次、只解析 1 次。

        若快路径只是「不算 sha 但仍读文件」，这条不会全绿 —— 而那样等于没优化
        （实测 3.42ms 里 3.26ms 是读文件，解析才 0.015ms）。
        """
        reads, parses = [], []
        real_read, real_parse = Path.read_bytes, wstore._parse_domain_lines

        def counted_read(self, *a, **kw):
            reads.append(str(self))
            return real_read(self, *a, **kw)

        def counted_parse(text):
            parses.append(text)
            return real_parse(text)

        with patch.object(Path, "read_bytes", counted_read), \
                patch.object(wstore, "_parse_domain_lines", counted_parse):
            for _ in range(3):
                self.assertEqual(wstore.ontology_domains(path=self.p), ["aaaaa"])
        self.assertEqual([r for r in reads if r == str(self.p)], [str(self.p)])
        self.assertEqual(len(parses), 1)

    def test_content_change_takes_effect_without_restart(self):
        """有变动 ⇒ 立即现取：改完文件下一次调用就是新清单，没有 TTL、不用重启。"""
        self.assertEqual(wstore.ontology_domains(path=self.p), ["aaaaa"])
        self.p.write_text(GOOD_B, encoding="utf-8")
        self.assertEqual(wstore.ontology_domains(path=self.p), ["bbbbb"])

    def test_touch_only_does_not_reparse(self):
        """只 touch（内容没变）⇒ 指纹变、sha 不变 ⇒ 复用旧结果，不重复解析。"""
        with patch.object(wstore, "_parse_domain_lines",
                          wraps=wstore._parse_domain_lines) as spy:
            first = wstore.ontology_domains(path=self.p)
            st = self.p.stat()
            os.utime(self.p, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
            second = wstore.ontology_domains(path=self.p)
        self.assertEqual(first, second)
        self.assertEqual(spy.call_count, 1)

    def test_sha_is_only_computed_when_fingerprint_changes(self):
        """sha 是二次确认，不是每次必算：命中不算、指纹变了才算。"""
        real = hashlib.sha256
        calls = []

        def counted(data):
            calls.append(len(data))
            return real(data)

        with patch.object(wstore.hashlib, "sha256", counted):
            wstore.ontology_domains(path=self.p)              # 首次：算
            wstore.ontology_domains(path=self.p)              # 命中：不算
            st = self.p.stat()
            os.utime(self.p, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
            wstore.ontology_domains(path=self.p)              # 指纹变：算，sha 同复用
        self.assertEqual(len(calls), 2, calls)

    def test_rewinding_mtime_cannot_hide_a_content_change(self):
        """★★ 守卫自己会响：删掉指纹里的 `st_ctime_ns`，这条立刻变红。

        构造：同 inode、同 size、内容换掉，再把 mtime 精确回拨到纳秒原值 ——
        mtime/size/ino 三项全同，光看它们会静默命中旧缓存。`os.utime()` 能
        回拨 mtime，但回拨不了 ctime（内核会把 ctime 推到当前时刻）。
        """
        self.assertEqual(wstore.ontology_domains(path=self.p), ["aaaaa"])
        before = self.p.stat()
        self.p.write_text(GOOD_B, encoding="utf-8")
        os.utime(self.p, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = self.p.stat()
        self.assertEqual((after.st_ino, after.st_size, after.st_mtime_ns),
                         (before.st_ino, before.st_size, before.st_mtime_ns),
                         "前提没成立：必须构造出三项全同才有意义")
        self.assertNotEqual(after.st_ctime_ns, before.st_ctime_ns)
        self.assertEqual(wstore.ontology_domains(path=self.p), ["bbbbb"])

    def test_broken_ontology_is_not_cached(self):
        """坏清单绝不进缓存：否则本体改好了，调用方还一直报「受控清单读不到」。"""
        self.assertEqual(wstore.ontology_domains(path=self.p), ["aaaaa"])
        self.p.write_text(BROKEN, encoding="utf-8")
        self.assertEqual(wstore.ontology_domains(path=self.p), [])
        self.assertNotIn(str(self.p), wstore._ONTOLOGY_CACHE)
        self.p.write_text(GOOD_B, encoding="utf-8")
        self.assertEqual(wstore.ontology_domains(path=self.p), ["bbbbb"])

    def test_missing_file_is_not_cached(self):
        """文件临时消失也是失败，同样不缓存；重建后下一次调用立即恢复。"""
        self.assertEqual(wstore.ontology_domains(path=self.p), ["aaaaa"])
        self.p.unlink()
        self.assertEqual(wstore.ontology_domains(path=self.p), [])
        self.assertNotIn(str(self.p), wstore._ONTOLOGY_CACHE)
        self.p.write_text(GOOD_B, encoding="utf-8")
        self.assertEqual(wstore.ontology_domains(path=self.p), ["bbbbb"])

    def test_invalid_utf8_returns_empty_instead_of_raising(self):
        """契约不变：读不到 / 读不懂都返回 [] 由调用方报错，缓存不得把异常吞成旧值。"""
        self.p.write_bytes(b"\xff\xfe\x00bad")
        self.assertEqual(wstore.ontology_domains(path=self.p), [])
        self.assertNotIn(str(self.p), wstore._ONTOLOGY_CACHE)

    def test_returned_list_is_a_copy(self):
        """返回副本：调用方往清单里 append 一个域，不许污染后续所有调用。"""
        got = wstore.ontology_domains(path=self.p)
        got.append("ghost")
        self.assertEqual(wstore.ontology_domains(path=self.p), ["aaaaa"])

    def test_two_paths_do_not_share_a_cache_entry(self):
        """缓存按路径分：`path=` 指向两个不同本体时不能串味。"""
        q = Path(self.tmp) / "other.md"
        q.write_text(GOOD_B, encoding="utf-8")
        self.assertEqual(wstore.ontology_domains(path=self.p), ["aaaaa"])
        self.assertEqual(wstore.ontology_domains(path=q), ["bbbbb"])

    def test_default_path_uses_the_same_cache(self):
        """★ 默认入口也要走这套缓存，不能只优化了测试的 `path=` 分支。"""
        with patch.object(wstore, "ONTOLOGY_PATH", self.p):
            self.assertEqual(wstore.ontology_domains(), ["aaaaa"])
            self.p.write_text(GOOD_B, encoding="utf-8")
            self.assertEqual(wstore.ontology_domains(), ["bbbbb"])
