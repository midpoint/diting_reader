"""wiki 实现第一步（数据模型 + 来源取数 + schema）的守卫反证，2026-11-05。

★ 这一组只证明「守卫自己会响」，不证明「改完能跑」：
  每组都先构造一个**故意踩线**的输入，断言被拒 / 被清空 / 被留空。
  只写期望值不构造反例，守卫写坏了照样全绿（项目里付过这个学费）。

盯的是本步最容易静默错掉的两件事：
  1. `published_at` 是「内容发布时间」，不是划线时间、不是提炼时间、
     不是抓取入库时间；拿不到要留 None，绝不降级、绝不编造。
  2. domain 是受控清单，清单外不许落库；清单本身读不出来时要**报错**，
     不能把所有 domain 静默清空当成一次成功的提炼。

花钱端点一律打桩（`core.citeguard._call`），且每条都配「桩真被用上」的反证。
"""
import json
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from core import ingest
from core import sourceblock
from core import wiki as wstore
from core.datasource.base import Entry
from core.models import Annotation, IngestCandidate

SID = "5730"
QUOTE = "我认为发布时间必须取原文发布的时间"
BLOCK = {"title": "测试文章", "text": "这是作者的正文，不是我的判断。",
         "html": "<p>这是作者的正文，不是我的判断。</p>",
         "feed_title": "", "published_at": None}


def _reply(domain="ai"):
    return json.dumps({"candidates": [
        {"kind": "decision", "claim": "发布时间要取原文", "quote": QUOTE,
         "domain": domain, "confidence": "medium"}]}, ensure_ascii=False)


class Step1Base(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.mkdtemp(prefix="diting-wiki-step1-")
        cls.ov = override_settings(WIKI_DIR=cls.tmp)
        cls.ov.enable()

    @classmethod
    def tearDownClass(cls):
        cls.ov.disable()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        wstore.ensure_dirs()
        ingest.title_of.cache_clear()
        Annotation.objects.create(source_type="entry", source_id=SID, body=QUOTE)

    def write_draft(self, text):
        p = wstore.root() / ".drafts" / "x.md"
        p.write_text(text, encoding="utf-8")
        return p

    def fake_entry(self, published_at, created_at=None):
        return Entry(id=int(SID), feed_id=1, feed_title="测试源", title="测试文章",
                     url="", published_at=published_at,
                     created_at=created_at or timezone.now(),
                     content="<p>这是作者的正文，不是我的判断。</p>")

    def extract_from_real_sourceblock(self, entry_obj, reply=None):
        """★ 不桩 sourceblock.load，让「取正文 + 取发布时间」这条链真跑一遍。

        只桩最外层的数据源单例与花钱端点；这样测到的 published_at 才是
        sourceblock 从 Entry 里取出来的，而不是测试自己塞进去的期望值。
        """
        ds = type("FakeDS", (), {"get_entry": lambda _self, _id: entry_obj})()
        with patch("core.citeguard._call", return_value=(reply or _reply(), "")) as call, \
                patch("core.datasource.get_datasource", return_value=ds):
            res = ingest.extract("entry", SID)
        call.assert_called_once()          # ★ 桩真被用上，否则整条用例是空跑
        return res


class MissingFmGuardTest(Step1Base):
    """反证 1：REQUIRED_FM 的新字段真的会拦下草稿。"""

    def _draft(self, extra):
        return self.write_draft(
            "---\ntitle: t\ndomain: ai\nsources: [entry:1]\n"
            "updated: 2026-11-05\nconfidence: medium\n%s---\n\n正文\n" % extra)

    def test_missing_candidates_is_rejected(self):
        self._draft("tags: []\n")
        ok, why = wstore.approve("x")
        self.assertFalse(ok)
        self.assertIn("candidates", why)
        self.assertTrue((wstore.root() / ".drafts" / "x.md").exists(),
                        "被拒时草稿要留着，不能挪走或删掉")

    def test_missing_tags_is_rejected(self):
        self._draft("candidates: [1]\n")
        ok, why = wstore.approve("x")
        self.assertFalse(ok)
        self.assertIn("tags", why)

    def test_empty_candidates_is_rejected(self):
        """空分组 = 空页，也算缺。页面即分组，没有候选的页没有存在意义。"""
        self._draft("tags: []\ncandidates: []\n")
        ok, why = wstore.approve("x")
        self.assertFalse(ok)
        self.assertIn("candidates", why)

    def test_empty_tags_is_legal(self):
        """★ 反证的反证：tags: [] 是合法的「无标签」，守卫不能过严。

        直接把 7 个字段塞进 REQUIRED_FM 而沿用 `not fm.get(k)` 判空，
        这一条就会红 —— 它专门挡住「守卫比漏一条更贵」的那种改法。
        """
        self._draft("tags: []\ncandidates: [1]\n")
        ok, why = wstore.approve("x")
        self.assertTrue(ok, why)

    def test_list_drafts_and_approve_share_the_same_gate(self):
        """两处判据收在 missing_fm() 一份：面板说完整，审批就不能反悔。"""
        p = self._draft("tags: []\ncandidates: [1]\n")
        row = [r for r in wstore.list_drafts() if r["slug"] == "x"][0]
        self.assertTrue(row["complete"], row["missing"])
        self.assertEqual(wstore.missing_fm(wstore.parse_front(p.read_text(encoding="utf-8"))[0]), [])


class OntologyGuardTest(Step1Base):
    """反证 2/3：受控清单解析 + 清单坏掉时调用方必须报错。"""

    #: 本体里**允许存在**的 H2 段落（按 `## 领域（…）` 里「（」前的名字比对）。
    #: ★ 只收「程序真的读」的一节 + 说明边界的一节；别往里加"参考性"段落。
    ONTOLOGY_SECTIONS = {"领域", "边界"}

    @staticmethod
    def _h2_names(p):
        """抽 H2 段落名（去掉括号里的解释）。空文件/无 H2 ⇒ 空集合。"""
        names = set()
        for line in Path(p).read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s.startswith("## "):
                continue
            names.add(s[3:].split("（")[0].split("(")[0].strip())
        return names

    def test_ontology_has_only_sections_the_program_reads(self):
        """★ 本体只准有「领域」「边界」两节（2026-11-23 用户拍板删概念/关系）。

        起因：原「概念（供编译归类参考）」「关系（AI 建链接时参考）」两段程序
        一条都不读 —— `ontology_text()` 只喂 `ontology_hash()`，而它没有产品
        调用方。分类实际由 domain + tags 承担，链接由正文 `[[slug]]` 自算。
        留着 = 给维护者一个假承诺：改了以为生效，其实什么都不发生。
        """
        got = self._h2_names(wstore.ONTOLOGY_PATH)
        self.assertEqual(got, self.ONTOLOGY_SECTIONS,
                         "本体多出（或缺少）了程序不读的段落：%s" % (got,))
        self.assertNotIn("概念", got)
        self.assertNotIn("关系", got)
        body = Path(wstore.ONTOLOGY_PATH).read_text(encoding="utf-8")
        self.assertNotIn("供编译归类参考", body)

    def test_example_matches_real_ontology_shape(self):
        """★ 使用者 cp 的是 example：两侧段落结构必须一致。

        否则真实文件删了「概念/关系」、模板还留着，新人复制一份就把不生效的
        段落又装回去了 —— 守卫只守真实文件等于没守。
        """
        example = Path(wstore.ONTOLOGY_PATH).with_name("ontology.example.md")
        if not example.exists():
            self.skipTest("示例模板不存在（精简/打包场景）")
        self.assertEqual(self._h2_names(example), self._h2_names(wstore.ONTOLOGY_PATH),
                         "模板与真实本体的段落结构不一致（cp 后会把死段落带回来）")

    def test_parses_the_five_domains_from_ontology_md(self):
        self.assertEqual(wstore.ontology_domains(),
                         ["invest", "ai", "career", "family", "life"])

    def test_example_is_anonymous_not_a_copy_of_real_ontology(self):
        """★ example 进版本库：不能是真实画像的翻版（公开即泄露）。

        真实域 / 虚构域必须零重合；`life` 是真实库里真有的域（宗教、网络安全
        两页都是 life），example 保留同名标签用于演示，不算泄露。
        虚构标签还得真能被解析出来（新人 cp 即用）。
        """
        example = Path(wstore.ONTOLOGY_PATH).with_name("ontology.example.md")
        if not example.exists():
            self.skipTest("示例模板不存在（精简/打包场景）")
        fake = wstore.ontology_domains(path=str(example))
        real = wstore.ontology_domains()
        self.assertEqual(len(fake), len(real), "虚构域数量应与真实域一致（模板要能演示多域）")
        self.assertEqual(set(fake) & set(real), {"life"},
                         "example 与真实本体只能共有 life，其余须虚构")
        self.assertIn("life", fake, "example 须保留 life（真实库里确有 life 页）")

    def test_domains_for_prompt_includes_life(self):
        """★ `life` 与别的域一样可选：wiki 里确有 life 页（宗教、网络安全…）。

        历史坑：曾把 life 单独从 prompt 可选域里排除，但落库校验又放行它 ——
        「不推荐却接受」的半吊子，行为不可预测。2026-11-23 去掉，这里钉住。
        """
        self.assertEqual(list(ingest.domains_for_prompt()),
                         wstore.ontology_domains())
        self.assertIn("life", ingest.domains_for_prompt())

    def test_broken_ontology_returns_empty(self):
        bad = Path(self.tmp) / "broken.md"
        bad.write_text("# 没有领域表\n\n随便写个 `#ghost` 标签\n", encoding="utf-8")
        self.assertEqual(wstore.ontology_domains(path=bad), [])

    def test_missing_ontology_returns_empty(self):
        self.assertEqual(wstore.ontology_domains(path=str(Path(self.tmp) / "nope.md")), [])

    def test_extract_refuses_when_ontology_unreadable(self):
        """★ 清单坏了要**显式报错**，不是静默把所有 domain 清空当成功。

        而且必须在调模型之前拦下：钱不该花在一次注定产不出合法 domain 的提炼上。
        """
        with patch.object(wstore, "ontology_domains", return_value=[]), \
                patch("core.sourceblock.load", return_value=BLOCK), \
                patch("core.citeguard._call") as call:
            res = ingest.extract("entry", SID)
        self.assertFalse(res["ok"])
        self.assertIn("受控清单", res["err"])
        call.assert_not_called()
        self.assertEqual(IngestCandidate.objects.count(), 0)

    def test_unknown_domain_is_blanked(self):
        reply = _reply(domain="区块链")
        with patch("core.citeguard._call", return_value=(reply, "")) as call, \
                patch("core.sourceblock.load", return_value=BLOCK):
            res = ingest.extract("entry", SID)
        self.assertTrue(res["ok"], res)
        call.assert_called_once()
        self.assertEqual(IngestCandidate.objects.get().domain, "",
                         "清单外的域名不许落库")

    def test_domain_gate_really_reads_the_ontology_list(self):
        """★ 反证：把受控清单**临时缩到只有 ai**，仍带 invest 的候选必须被清空。

        这条专门证明落库处那道闸不是摆设 —— 只测「区块链」的话，
        clean_candidates 那一层就先挡掉了，证明不了落库闸自己会响。
        """
        with patch("core.citeguard._call", return_value=(_reply(domain="invest"), "")) as call, \
                patch("core.sourceblock.load", return_value=BLOCK), \
                patch.object(wstore, "ontology_domains", return_value=["ai"]):
            res = ingest.extract("entry", SID)
        self.assertTrue(res["ok"], res)
        call.assert_called_once()
        self.assertEqual(IngestCandidate.objects.get().domain, "")


class PublishedAtGuardTest(Step1Base):
    """反证 4/5：不混用 created_at，也不编造。这是本步最关键的两条。"""

    def test_entry_uses_published_at_not_created_at(self):
        pub = timezone.make_aware(datetime(2026, 1, 2, 3, 4))
        created = pub + timedelta(days=30)      # 抓取/入库晚 30 天，最容易混的那个
        res = self.extract_from_real_sourceblock(self.fake_entry(pub, created))
        self.assertTrue(res["ok"], res)
        c = IngestCandidate.objects.get()
        self.assertEqual(c.published_at, pub)
        self.assertNotEqual(c.published_at, created, "降级到抓取时间就污染了 decay 排序")
        self.assertNotEqual(c.published_at.date(), c.created_at.date(),
                            "不是提炼时间，也不是我写批注的今天")
        self.assertEqual(c.tags, [], "本步 tags 一律落空，提炼 prompt 未改动")

    def test_missing_published_at_is_none_not_fabricated(self):
        """★ published_at 为 None 时要老实留空，不许拿 created_at / 今天补。"""
        res = self.extract_from_real_sourceblock(self.fake_entry(None))
        self.assertTrue(res["ok"], res)
        self.assertIsNone(IngestCandidate.objects.get().published_at)

    def test_naive_entry_datetime_becomes_aware(self):
        """USE_TZ=True：naive datetime 落库会炸/告警，取数处要自己补时区。"""
        naive = datetime(2026, 3, 4, 5, 6)
        res = self.extract_from_real_sourceblock(self.fake_entry(naive))
        self.assertTrue(res["ok"], res)
        self.assertTrue(timezone.is_aware(IngestCandidate.objects.get().published_at))

    def test_load_entry_keeps_all_old_keys(self):
        pub = timezone.make_aware(datetime(2026, 1, 2, 3, 4))
        ds = type("FakeDS", (), {"get_entry": lambda _s, _i: self.fake_entry(pub)})()
        with patch("core.datasource.get_datasource", return_value=ds):
            blk = sourceblock.load("entry", SID)
        for key in ("title", "text", "html", "feed_title"):
            self.assertIn(key, blk, "加 published_at 不许碰掉现有 key")
        self.assertEqual(blk["published_at"], pub)

    def test_load_report_returns_its_date_and_keeps_all_old_keys(self):
        with patch("core.summary.read_report", return_value={"text": "# 日报\n正文"}):
            blk = sourceblock.load("report", "daily-2026-11-05")
        for key in ("title", "text", "html", "feed_title"):
            self.assertIn(key, blk)
        self.assertEqual(blk["published_at"].date().isoformat(), "2026-11-05")
        self.assertTrue(timezone.is_aware(blk["published_at"]))

    def test_unparsable_report_date_is_none_not_guessed(self):
        with patch("core.summary.read_report", return_value={"text": "# 日报\n正文"}):
            blk = sourceblock.load("report", "daily-2026-11-05")
        # 日期解析失败时 `_source_published_at` 直接返回 None（不猜、不补今天）
        self.assertIsNone(sourceblock._source_published_at("report", "daily-not-a-date"))
        self.assertIsNone(sourceblock._source_published_at("web", "x"))
        self.assertEqual(blk["published_at"].year, 2026)
