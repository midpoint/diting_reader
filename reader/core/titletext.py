"""标题文本判定层（纯函数）：哪些标题值得翻、译文是否有效、模型输出解析。

从 core/translate.py 抽出（2026-10-24）：判定与解析不碰数据源 / 数据库 /
LLM —— 输入输出都是字符串，可被单测直接覆盖；translate.py 只留
worker（调度 / 预算 / 退避 / 写库）。
"""
import hashlib
import html
import re

from django.conf import settings

from core import settingkit as sk
from django.utils.html import strip_tags

from core import jsontool


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
KANA_RE = re.compile(r"[\u3040-\u30ff]")
LATIN_RE = re.compile(r"[A-Za-z]")
URL_RE = re.compile(r"^https?://", re.I)


def clean_title(raw) -> str:
    """去标签/实体/连续空白 —— 送模型和算 hash 都用这一份。"""
    text = strip_tags(str(raw or ""))
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def title_hash(title) -> str:
    return hashlib.sha256(clean_title(title).encode("utf-8")).hexdigest()


def check_title(raw):
    """→ (可翻?, 原因, 清洗后的标题)。

    原因：empty / too_long / not_needed。
    ★ v1 的判定刻意简单：含中日韩字符就跳过（主要应对全英文标题）；
      法/德等拉丁语标题也会被翻，日/韩/中英混排不翻。
    """
    title = clean_title(raw)
    if not title:
        return False, "empty", title
    if len(title) > sk.effective("translate_char_limit"):
        return False, "too_long", title
    if CJK_RE.search(title) or KANA_RE.search(title):
        return False, "not_needed", title
    latin = LATIN_RE.findall(title)
    if len(latin) < 3:
        # 2 个字母的标题（OK / AI / Go）翻出来多数是噪音，不值得花钱
        return False, "not_needed", title
    letters = [c for c in title if c.isalpha()]
    if letters and len(latin) / len(letters) < 0.6:
        return False, "not_needed", title
    if URL_RE.match(title):
        return False, "not_needed", title
    return True, "", title


def is_translatable(raw) -> bool:
    return check_title(raw)[0]


def reason_text(reason: str) -> str:
    if reason == "too_long":
        return f"标题超过 {sk.effective('translate_char_limit')} 字，暂不翻译"
    if reason == "not_needed":
        return "标题不含英文，无需翻译"
    if reason == "empty":
        return "标题为空，无需翻译"
    return "标题不可翻译"


def _clean_zh(value) -> str:
    zh = strip_tags(str(value or ""))
    zh = html.unescape(zh)
    zh = re.sub(r"\s+", " ", zh).strip()
    zh = re.sub(r"^(译文|中文|翻译)\s*[:：]\s*", "", zh)
    if len(zh) >= 2 and zh[0] in "\"'“”‘’" and zh[-1] in "\"'“”‘’":
        zh = zh[1:-1].strip()
    return zh


def _invalid_zh(zh: str, title: str) -> bool:
    if not zh:
        return True
    if not re.search(r"[0-9A-Za-z\u4e00-\u9fff]", zh):
        return True
    # 模型加了解释/扩写：中文标题长度通常是原文的 0.5~1.5 倍，给到 2 倍 + 30 的余量
    return len(zh) > max(40, len(title) * 2 + 30)


def parse_translations(text: str) -> dict:
    """从模型输出里捞出 {entry_id: 中文标题}（JSON 提取见 core.jsontool）。
    解析失败返回空 dict。"""
    data = next(jsontool.iter_json(text), None)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return {}

    out = {}
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            entry_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue
        zh = _clean_zh(row.get("zh") or row.get("title_zh") or row.get("translation"))
        if zh:
            out[entry_id] = zh
    return out

