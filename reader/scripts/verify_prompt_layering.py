"""零成本验证「分级 prompt 的四层分层」（不调 LLM、不落盘；只建假配置、结束删净）。

用法：python scripts/verify_prompt_layering.py

背景（2026-10-15 事故）：`Meta/interest-profile.md` 曾经同时装着「全局读者画像」和
「某分类的 L0–L3 判据」，而画像被 `{profile}` 拼进每一个源的 prompt
⇒ 该分类判据泄漏进了另一个分类（兼职信息）源的判定 prompt。本脚本把修复后的
分层契约钉住。

★ 数据自足：判据/备注检查全部用**自建的假配置**（分类 910 / 源 9101/9102），
  不依赖运行库里的真实配置 —— 任何环境（包括空库）跑出来结论一致。
"""
import os
import re
import sys

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "apps"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402
django.setup()

import _dbtest  # noqa: E402

# ★ 它用 FAKE_CAT 造假分类，但 config 表还是写在真实库上（写-删之间崩了会留行）。
_dbtest.isolate()

from core import promptbuilder as pb  # noqa: E402
from core import promptspec  # noqa: E402
from core.models import CategoryTriageConfig, FeedTriageConfig  # noqa: E402

ok = bad = 0
fails = []


def check(name, cond, detail=""):
    global ok, bad
    if cond:
        ok += 1
        print("  PASS  %s" % name)
    else:
        bad += 1
        fails.append(name)
        print("  FAIL  %s   %s" % (name, detail))


def read(rel):
    with open(os.path.join(BASE, rel), encoding="utf-8") as f:
        return f.read()


# ★ 画像取「本地画像，没有则用入库模板」——任何环境都有东西可查。
PROFILE = promptspec.profile_text() or read("../Meta/interest-profile.example.md")
check_ok_have_profile = bool(promptspec.profile_text())
print("    画像来源：%s" % ("本地 Meta/interest-profile.md"
                           if check_ok_have_profile else "入库模板 example"))

# ---------------------------------------------------------------- 假配置
FAKE_CAT = 910        # 「行业动态」分类：四级判据齐全
FAKE_JOB_CAT = 911    # 「兼职信息」分类：不配判据（判据全在源上）
FAKE_JOB = 9101       # 兼职信息源：只有 L0/L2 判据 + 默认 L1
FAKE_FULL = 9102      # 行业动态源：本源判据（与分类措辞不同，验证合并不覆盖）+ 三层文本备注

CAT_L = {
    "l0_text": "纯情绪输出、与任何决策无关的口水内容",
    "l1_text": "行业见闻，但与读者关注的方向无直接关联",
    "l2_text": "行业政策 / 数据事实，能改变判断的内容",
    "l3_text": "点名具体对象且有方向性的判断（含风险提示，不做空中楼阁式断言）",
}
JOB_L = {
    "l0_text": "纯广告、刷屏、与兼职无关的推广",
    "l2_text": "招聘 / 发包 / 找队友三类机会",
}
FULL_L = dict(CAT_L, l2_text="本源口径：行业数据里直接影响估值与格局的事实")
NOTE_FULL = "正文是三层复合文本：他本人的回复 // 别人的评论 // 被引用的原帖"


def seed_fake():
    CategoryTriageConfig.objects.update_or_create(
        category_id=FAKE_CAT, defaults=dict(CAT_L))
    FeedTriageConfig.objects.update_or_create(
        feed_id=FAKE_JOB, defaults=dict(JOB_L, default_level=1, note=""))
    FeedTriageConfig.objects.update_or_create(
        feed_id=FAKE_FULL, defaults=dict(FULL_L, note=NOTE_FULL))


def cleanup_fake():
    CategoryTriageConfig.objects.filter(category_id=FAKE_CAT).delete()
    FeedTriageConfig.objects.filter(feed_id__in=(FAKE_JOB, FAKE_FULL)).delete()


def spec_of(fid, cid, title):
    return promptspec.build_spec(fid, cid, feed_title=title)


try:
    seed_fake()
    S_JOB = spec_of(FAKE_JOB, FAKE_JOB_CAT, "兼职信息板")
    S_FULL = spec_of(FAKE_FULL, FAKE_CAT, "行业动态")

    print("\n== 1. 画像只描述读者 ==")
    check("profile_lint 干净（画像里没有级别判据）", promptspec.profile_lint() == [],
          str(promptspec.profile_lint()))
    check("画像长度 ≤ 2000（润色路径 profile_text()[:2000] 会静默砍尾）",
          len(PROFILE) <= 2000, "实际 %d" % len(PROFILE))
    check("画像没有级别定义表（| L0 | ... | 形状）",
          not re.search(r"^\s*\|[^\n]*L[0-3][^\n]*\|", PROFILE, re.M))
    check("画像没有「L2 = 投资」式判据句",
          not re.search(r"\*{0,2}L[0-3]\*{0,2}\s*[:：=]\s*[^\n]{6,}", PROFILE))
    check("画像声明了自己不定义级别判据（防止下次越界）",
          "不重复定义" in PROFILE or "不提供「算哪级」" in PROFILE
          or "只提供读者侧" in PROFILE)
    # ★ 2026-10-27：画像从「分级专属」共用给日报札记
    NB = promptspec.profile_notes_block()
    check("画像可给日报用（profile_notes_block 非空）", bool(NB.strip()), NB[:60])
    check("profile_notes_block 不改写画像正文（与分级同源）",
          NB == PROFILE[:promptspec.PROFILE_LIMIT])
    # ★ 真验证：临时把项目根指向空目录，模拟「没配置画像」
    import tempfile  # noqa: E402
    from pathlib import Path  # noqa: E402
    from django.conf import settings as dj_settings  # noqa: E402
    _old_root, _tmp = dj_settings.PROJECT_ROOT, Path(tempfile.mkdtemp())
    try:
        dj_settings.PROJECT_ROOT = _tmp
        missing = promptspec.profile_notes_block()
    finally:
        dj_settings.PROJECT_ROOT = _old_root
    check("画像缺失时给兜底口径（不是静默空串）",
          "未配置" in missing and bool(missing.strip()), missing[:80])

    print("\n== 2. 旧画像确实会被 lint 抓到（证明 lint 有效）==")
    OLD_SNIPPET = "| **L2** | 投资 | 与投资相关，值得看 |\n**L3**：必须有指向对象，点名行业/个股"
    check("级别定义表会被 lint 抓到",
          bool(re.search(r"^\s*\|[^\n]*L[0-3][^\n]*\|", OLD_SNIPPET, re.M)))
    check("「**L2**：…」会被 lint 抓到",
          bool(re.search(r"\*{0,2}L[0-3]\*{0,2}\s*[:：=]\s*[^\n]{6,}", OLD_SNIPPET)))

    print("\n== 3. 底座不含来源专属规则 ==")
    check("BASE_RULES 没有任何来源专属的「三层复合文本」规则",
          "三层复合文本" not in promptspec.BASE_RULES)
    check("BASE_RULES 仍保留 anti-FOMO / 投票硬约束",
          "全票 L0 才判 L0" in promptspec.BASE_RULES
          and "不要直接判 L0" in promptspec.BASE_RULES)
    check("TEMPLATE_VERSION 已 bump 到 3（改底座必须 bump）",
          promptspec.TEMPLATE_VERSION == "3", promptspec.TEMPLATE_VERSION)

    print("\n== 4. 注入契约 ==")
    check("RULE_TEMPLATE 的画像段带作用域声明",
          "本段不提供" in promptspec.RULE_TEMPLATE or "不得被推导出" in promptspec.RULE_TEMPLATE)
    check("RULE_TEMPLATE 有独立的「本源备注」段", "{note}" in promptspec.RULE_TEMPLATE)
    check("note_block('') 返回空串（没备注就不渲染该段）", promptspec.note_block("") == "")
    check("note_block('x') 明确标注「不是判据」", "不是判据" in promptspec.note_block("x"))

    print("\n== 5. spec 与指纹 ==")
    check("spec 带 note 字段", "note" in S_JOB)
    check("note 参与 source_hash（改备注会触发重建）",
          promptspec.source_hash(dict(S_JOB, note="a"))
          != promptspec.source_hash(dict(S_JOB, note="b")))
    check("levels_defined 只列有判据的级别（兼职源 = L0、L2 + 默认 L1）",
          pb._levels_defined(S_JOB).startswith("L0、L2"), pb._levels_defined(S_JOB))
    check("unused_levels 认出兼职源不使用 L3", pb._unused_levels(S_JOB) == "L3")
    check("unused_levels 对四级齐全的源返回「无」", pb._unused_levels(S_FULL).startswith("无"))
    check("判据合并：分类段在前、本源段在后（拼接不覆盖）",
          S_FULL["l2"]["text"] == CAT_L["l2_text"] + "\n" + FULL_L["l2_text"]
          and [p["from"] for p in S_FULL["l2"]["parts"]] == ["category", "feed"])

    print("\n== 6. 渲染结果 ==")
    R_JOB = promptspec.render_rule(S_JOB)
    R_FULL = promptspec.render_rule(S_FULL)
    check("兼职源渲染：不含另一分类的专属口径",
          "不做空中楼阁式断言" not in R_JOB)
    check("兼职源渲染：无本源备注段", "本源备注" not in R_JOB)
    check("兼职源渲染：有本源说明与默认级别兜底",
          "默认级别" in R_JOB and "兼职信息板" in R_JOB)
    check("行业源渲染：有本源备注段", "本源备注" in R_FULL)
    check("行业源渲染：备注内容进了独立段", "三层复合文本" in R_FULL)

    print("\n== 7. 护栏：判据区定位 ==")
    check("整行标题「===== 各级判据 =====」被识别", bool(pb._HEAD_CRIT.match("===== 各级判据 =====")))
    check("整行标题「四、各级判据」被识别", bool(pb._HEAD_CRIT.match("四、各级判据")))
    check("模板自己的作用域声明**不**被当成标题（踩过的坑）",
          not pb._HEAD_CRIT.match("（本段**只**说明读者是谁；**级别的具体判据一律在下方「各级判据」段**，"))
    check("规则版的判据区不含画像正文（只从「各级判据」起算）",
          len(pb._criteria_region(R_FULL)) < len(R_FULL) / 2
          and "读者" not in pb._criteria_region(R_FULL)[:20])

    print("\n== 8. 护栏：画像越界检测 ==")
    PROF_CHUNK = re.sub(r"\s+", "", PROFILE)[200:280] or re.sub(r"\s+", "", PROFILE)[:60]
    check("成段搬运画像 → 命中",
          len(pb.profile_leak("四、各级判据\nL2：与行业相关\n" + PROF_CHUNK)) > 0)
    check("正常判据文本 → 不误伤",
          pb.profile_leak("四、各级判据\nL2：招聘 / 发包 / 找队友三类机会\n五、仲裁规则\n未命中归 L1") == [])
    check("规则版（画像在段外）→ 不误伤", pb.profile_leak(R_JOB) == [])

    print("\n== 8.5 日报札记契约（2026-10-27 改名 + 画像共用）==")
    from core import summary  # noqa: E402
    S0 = summary.S0_USER
    check("S0 第四节叫「谛听札记」", "## 四、谛听札记" in S0)
    check("不再出现旧标题「我的观察与建议」", "我的观察与建议" not in S0)
    check("札记要求「至多 3 件」", "至多 3 件" in S0)
    # ★ 2026-11-23 用户定：日报札记的三件事已带引用、点名文章，必读/选读是
    #   重复劳动（11-22/11-23 两份真实日报里「必读」就是三件事原样重列）。
    #   沿用「可跳过」那次的口径：**不在 prompt 里写禁令**，不引导即禁止。
    check("日报札记不再出现必读/选读/可跳过分档（不引导即禁止）",
          all(k not in S0 for k in ("必读", "选读", "可跳过")))
    check("三件事必须带引用（删档后唯一可点开的入口）", "每件必须带引用" in S0)
    # ★ 画像全文会经 `{profile}` 注入 S0，prompt 里删词但画像里还写着「必读」照样引导。
    check("注入日报的画像不引导必读/选读/可跳过分档",
          all(k not in promptspec.profile_notes_block()
              for k in ("必读", "选读", "可跳过")))
    # ★ 用户这次只改日报：周报第四节仍要两档，钉住防后续「顺手统一」。
    from core import weekly  # noqa: E402
    check("周报仍保留必读/选读两档（本次只改日报）",
          all(k in weekly.W0_USER for k in ("必读", "选读")))
    check("跨文章趋势/矛盾要标注推测", "（推测）" in S0)
    check("无大事要直说", "本日无大事" in S0)
    check("明确禁止复述统计（点名了具体指标）",
          "条目数" in S0 and "占比" in S0 and "集中度" in S0)
    check("元凶已删：不再**要求**解读统计（旧 prompt 写着「你只负责解读」）",
          "你只负责解读" not in S0)
    # ★ 禁止项要**点名**才有效（2026-10 教训：只说"别写"模型照写），所以
    #   「TOP3 源占」只能作为反例出现，且必须紧邻禁止措辞。
    check("统计只作为反例出现（禁止句里点名示例）", "TOP3 源占" in S0 and "不许" in S0)
    check("S0 不再接收数量统计块（{stats} 占位已移除）", "{stats}" not in S0)
    check("S0 注入读者画像（{profile}）", "{profile}" in S0)
    check("画像块声明「不是分级判据」", "不是分级判据" in S0)
    check("S0 的占位符能被真实参数填满（不会 KeyError）",
          bool(summary.S0_USER.format(
              window="10-26 06:00 → 10-27 06:00", n=10, p=3, unrated=0,
              profile="x", anomalies="（无）", body="y")))

    print("\n== 9. 护栏：未配置级别 ==")
    FAKE_BAD = ("四、各级判据\nL0（忽略，本源口径）——以下统统忽略\n"
                "L2（关注，本源口径）——三类机会\n"
                "L3（特别关注，本源口径细化）——与读者能力吻合的条目\n")
    check("「L3（特别关注，本源口径细化）」→ 判为模型自编（兼职源没配 L3）",
          pb.invented_levels(FAKE_BAD, S_JOB) == [3],
          str(pb.invented_levels(FAKE_BAD, S_JOB)))
    check("同一文本对四级齐全的源 → 不误伤",
          pb.invented_levels(FAKE_BAD, S_FULL) == [],
          str(pb.invented_levels(FAKE_BAD, S_FULL)))
    check("默认兜底级不算自编",
          pb.invented_levels("四、各级判据\nL1：未命中归此级\nL1 是兜底", S_JOB) == [])
    check("只在输出格式里提一次 → 不算自编",
          pb.invented_levels("四、各级判据\nL2：三类机会\n七、输出\nlevel 取 L0/L1/L2/L3 之一", S_JOB) == [])
    check("占位符检查只抓真残留，不误伤 JSON 示例",
          pb._PLACEHOLDER.search('{"id":1,"level":"L0"}') is None
          and pb._PLACEHOLDER.search("请你输出 {profile} 的内容") is not None)

    print("\n== 10. 配置表单源码扫描 ==")
    FORM = read("templates/triage/config_form.html")
    check("占位符不再出现特定分类字样（投资）", "投资" not in FORM)
    check("占位符给了中性示例", "纯广告软文" in FORM and "可立刻行动" in FORM)
    check("表单有「本源备注」输入框（name=note）", 'name="note"' in FORM)
    check("备注框标明「不是判据」", "不是判据" in FORM)
    VIEWS = read("apps/triage/views.py")
    check("config_save 会保存 note", 'fields["note"]' in VIEWS)

    print("\n== 11. 数据契约（假配置，结束删净）==")
    c910 = CategoryTriageConfig.objects.filter(category_id=FAKE_CAT).first()
    check("分类判据四段落库（update_or_create 生效）",
          bool(c910) and all(getattr(c910, f"l{i}_text") for i in range(4)))
    f9102 = FeedTriageConfig.objects.filter(feed_id=FAKE_FULL).first()
    check("本源备注确实住在 FeedTriageConfig.note",
          bool(f9102) and "三层复合文本" in (f9102.note or ""))
    check("MODEL：FeedTriageConfig.note 字段存在", hasattr(FeedTriageConfig, "note"))
finally:
    cleanup_fake()
    left = (CategoryTriageConfig.objects.filter(category_id=FAKE_CAT).exists()
            or FeedTriageConfig.objects.filter(feed_id__in=(FAKE_JOB, FAKE_FULL)).exists())
    check("★ 假配置已删净（不留测试残留）", not left)

print()
print("=" * 68)
print("结果：%d 通过 / %d 失败" % (ok, bad))
if fails:
    print("失败项：")
    for f in fails:
        print("  - %s" % f)
print("=" * 68)
sys.exit(1 if bad else 0)
