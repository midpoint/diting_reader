from datetime import timedelta
from urllib.parse import urlsplit

from django import template
from django.utils import timezone

register = template.Library()


@register.filter
def reldt(value):
    """紧凑中文相对时间：刚刚 / N 分钟前 / N 小时后 / N 天前 / 日期。

    ★ 未来时间必须说「后」：票房站这类源的 published_at 是电影上映日期
      （未来），以前会把「10 天后」显示成「10 天前」。
    """
    if not value:
        return "-"
    if timezone.is_naive(value):
        value = timezone.make_aware(value)
    delta = timezone.now() - value
    future = delta < timedelta(0)
    if future:
        delta = -delta
    unit = "后" if future else "前"
    if delta < timedelta(minutes=1):
        return "即将" if future else "刚刚"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)} 分钟{unit}"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)} 小时{unit}"
    if delta.days < 30:
        return f"{delta.days} 天{unit}"
    return timezone.localtime(value).strftime("%Y-%m-%d")


@register.filter
def level_text(obj, index):
    """取配置对象的 l{n}_text（模板里不能拼属性名）。"""
    if obj is None:
        return ""
    return getattr(obj, f"l{index}_text", "") or ""


@register.filter
def num(value):
    """整数千分位（报销／token 数用）。"""
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return value


@register.filter
def divide(value, other):
    """除法（模板不能做算术）。other 为 0/空 → 0。"""
    try:
        other = float(other)
        return float(value) / other if other else 0.0
    except (TypeError, ValueError):
        return 0.0


@register.filter
def get_item(obj, key):
    """按变量键取 dict 值（DTL 不支持 `d[var]`）。"""
    if hasattr(obj, "get"):
        return obj.get(key)
    return None


@register.filter
def host(value):
    """URL → 裸域名（去掉 www. 与端口），给「打开原文」按钮标注去向。

    解析失败或空值一律返回空串，模板里配合 `{% if %}` 用，不会渲染出 'None'。
    ★ 只做展示，不做任何跳转判断；跳转一律用原始 entry.url。
    """
    try:
        net = urlsplit(str(value or "")).netloc
    except ValueError:
        return ""
    net = net.split("@")[-1].split(":")[0]          # 去掉 userinfo 与端口
    return net[4:] if net.startswith("www.") else net


# ------------------------------------------------------------------ 预设话术出口
# ★ 为什么走模板标签而不是「模板里再抄一遍」：快捷问题/自动总结的文本是
#   `core/ingest.py` 分诊的**判据**（这些字不是「我敲的字」，必须排除）。
#   文案在模板里、判据在 Python 里 = 两份必然漂；而漂了不会报错，只会让
#   「总结这篇文章」被当成我的判断混进沉淀链。所以文本只在 core.conversation
#   定义一次，模板从这儿取。
@register.simple_tag
def quick_asks():
    """快捷问题按钮（(标签, 文本) 列表）。"""
    from core.conversation import QUICK_ASKS
    return QUICK_ASKS


@register.simple_tag
def autosend_text():
    """打开 feed 文章时自动发的那句（"点开即总结"）。"""
    from core.conversation import AUTOSEND_TEXT
    return AUTOSEND_TEXT

