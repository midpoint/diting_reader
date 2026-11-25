"""全局设置的注册表与读写（设置页的后端）。

三层配置、一套优先级：**AppSetting（设置页写入）> django settings（.env）> 代码默认**。
与预算池既有的 `_setting_float`（costreport）同一语义——这里是它的推广与收编。

★ 设计边界（2026-10-26 设置页 v1）：
  - 只收「使用中会想调」的项；部署基线（Django_*/Miniflux 凭据/日志轮转）不进；
  - **不收密钥**：LLM key 仍在 `.env`（BYOK 非必要，见开源决策）；
  - 每项声明生效级别：`instant`（读取点每次现查，立即生效）/ `restart`（调度注册
    类参数，重启后生效——设置页如实标注，不做 reschedule）。
  - `effective()` 只认注册表内的 key：拼错的 key 直接 KeyError，不静默回落。
"""
import logging
from dataclasses import dataclass, field as dc_field

from django.conf import settings as dj_settings

from core.models import AppSetting

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Field:
    key: str            # AppSetting 键（小写）；settings.py 里的同名项 = key.upper()
    label: str
    type: str = "int"   # bool | int | float | str
    effect: str = "instant"
    minimum: float = None
    maximum: float = None
    help: str = ""
    placeholder: str = ""


@dataclass(frozen=True)
class Group:
    key: str
    label: str
    fields: list = dc_field(default_factory=list)


# ------------------------------------------------------------------ 注册表
# ★ 顺序即设置页导航顺序。改这里 = 改设置页；读取点用 effective(key) 跟着生效。
GROUPS = [
    Group("triage", "分级", [
        Field("triage_enabled", "启用分级", "bool",
              help="关闭后不再自动判定新条目（已有判定保留）"),
        Field("triage_interval_min", "轮询间隔（分钟）", "int", "restart",
              1, 1440, help="多少分钟跑一轮分级扫描"),
        Field("triage_repeat", "每条投票次数", "int", "instant", 1, 5,
              help="票数上限；自适应开启时只有「全是 L0」才用到满票，关闭时每条跑满"),
        Field("triage_adaptive", "自适应投票", "bool",
              help="逐票评估：出现 L2/L3 即停、L1 满 2 票定、全 L0 满票才判（省调用）"),
        Field("triage_lookback_days", "判级窗口（天）", "int", "instant", 1, 60,
              help="只判最近 N 天发布的条目，更早的按设计永不判"),
        Field("triage_max_calls", "单轮调用上限", "int", "instant", 1, 500,
              help="一轮分级最多调多少次 LLM，超出留到下一轮"),
        Field("triage_batch_size", "每批条数", "int", "instant", 1, 30,
              help="一次调用判定多少条（prompt 是 feed 级的，不跨源混批）"),
        Field("triage_char_limit", "单条正文字数上限", "int", "instant", 200, 5000,
              help="喂给模型的单条正文截断长度"),
    ]),
    Group("summary", "日报与总结", [
        Field("summary_enabled", "启用日报", "bool",
              help="关闭后不再生成 / 补跑日报"),
        Field("summary_hour", "日报时间·时", "int", "restart", 0, 23,
              help="每天几点编排（0–23）"),
        Field("summary_minute", "日报时间·分", "int", "restart", 0, 59,
              help="（0–59）"),
        Field("summary_window_hour", "窗口边界（点）", "int", "instant", 0, 23,
              help="日报汇总 [前一日 N 点, 当日 N 点) 的内容，跨夜内容归同一个晚上"),
        Field("summary_max_attempt", "单日自动重试上限", "int", "instant", 1, 10,
              help="失败后最多自动重试几次，超过只列在缺口页等人点"),
        Field("summary_max_calls", "单次运行调用上限", "int", "instant", 5, 200,
              help="一次日报最多调多少次 LLM"),
    ]),
    Group("translate", "标题翻译", [
        Field("translate_enabled", "启用标题翻译", "bool",
              help="总开关；单个源的开关在树上右键「翻译标题」"),
        Field("translate_lookback_days", "回填窗口（天）", "int", "instant", 1, 30,
              help="开启一个源时回填最近 N 天的标题"),
        Field("translate_backfill_max", "回填条数上限", "int", "instant", 1, 200,
              help="每个源最多回填多少条（从旧到新逐轮补）"),
        Field("translate_char_limit", "跳过超长标题（字）", "int", "instant", 50, 1000,
              help="超过此长度的标题不翻（不截半句）"),
    ]),
    Group("weekly", "周报", [
        Field("weekly_enabled", "启用周报", "bool",
              help="关闭后不再生成周报（已生成的仍在本地）"),
        Field("weekly_weekday", "周报·星期", "int", "restart", 0, 6,
              help="0=周一 … 6=周日"),
        Field("weekly_hour", "周报时间·时", "int", "restart", 0, 23,
              help="当天日报跑完之后（默认 12 点）"),
        Field("weekly_minute", "周报时间·分", "int", "restart", 0, 59),
        Field("weekly_retry_hours", "等当天日报（小时）", "int", "instant", 0, 12,
              help="当天日报还没出来时最多再等几小时；缺的是**历史**日报则立即失败并通知"),
    ]),
    Group("notify", "系统通知", [
        Field("notify_enable", "启用系统通知", "bool",
              help="关闭后所有系统通知静默（分级与推送不受影响）"),
        Field("notify_l3", "L3 点名通知", "bool",
              help="模型判出 L3 时弹通知，点击直达该条"),
        Field("notify_daily", "日报完成通知", "bool"),
        Field("notify_daily_fail", "日报失败通知", "bool",
              help="失败单独提醒：静默漏推比失败更糟"),
        Field("notify_weekly", "周报完成通知", "bool"),
        Field("notify_ignore_dnd", "穿透勿扰模式", "bool"),
        Field("notify_sound", "提示音", "str",
              help="通知提示音名（macOS 系统音，如 default / Glass / Ping）"),
        Field("notify_fail_sound", "失败提示音", "str",
              help="留空 = 失败静音"),
    ]),
]

REG: dict = {}
GROUP_OF: dict = {}
for _g in GROUPS:
    for _f in _g.fields:
        assert _f.key not in REG, f"重复的设置项：{_f.key}"
        REG[_f.key] = _f
        GROUP_OF[_f.key] = _g.key


# ------------------------------------------------------------------ 读
def _coerce(f: Field, raw):
    """按声明类型转值（settings 来源已是类型化值，AppSetting 来源是字符串）。"""
    if f.type == "bool":
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "on", "yes")
    if f.type == "int":
        return int(float(raw))
    if f.type == "float":
        return float(raw)
    return str(raw)


def effective(key: str):
    """读取某项当前生效值：AppSetting > django settings > 注册表默认。

    ★ 只认注册表内的 key（拼错立即 KeyError，绝不静默回落）。
    ★ 15s 本地缓存：列表页每条渲染都会查 lookback 之类的项，别让设置页
      拖慢页面；write() 会立即清缓存，「即时生效」最多延迟 15 秒。
    """
    from django.core.cache import cache

    f = REG[key.lower()]
    ck = f"settingkit:{f.key}"
    try:
        got = cache.get(ck)
    except Exception:  # noqa: BLE001  缓存故障时退化为直查，不能拖垮功能
        got = None
    if got is not None:
        return got

    try:
        row = AppSetting.objects.filter(key=f.key).first()
    except Exception:  # noqa: BLE001
        # 表未建（migrate 前启动）/ 库故障：回退 .env/默认。★ 设置读取绝不能
        # 炸掉功能链路 —— 与上面"缓存故障退化为直查"同一原则；不建库的纯
        # 单元测试也因此不需要 DB。
        row = None
    if row is not None and (row.value or "").strip() != "":
        try:
            val = _coerce(f, row.value)
        except (TypeError, ValueError):
            log.warning("AppSetting[%s] 值非法（%r），回退 .env/默认", f.key, row.value)
        else:
            _cache_set(cache, ck, val)
            return val
    env = getattr(dj_settings, f.key.upper(), None)
    val = env if env is not None else (
        _coerce(f, 0) if f.type in ("int", "float") else False)
    _cache_set(cache, ck, val)
    return val


def _cache_set(cache, ck, val):
    try:
        cache.set(ck, val, 15)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ 写
def validate(f: Field, raw) -> tuple:
    """校验并转型。返回 (ok, 值或错误文案)。"""
    s = str(raw).strip()
    try:
        if f.type == "bool":
            if s.lower() not in ("1", "0", "true", "false", "on", "off", "yes", "no"):
                return False, "布尔值只认 1/0/true/false"
            return True, s.lower() in ("1", "true", "on", "yes")
        if f.type == "int":
            v = int(float(s))
        elif f.type == "float":
            v = float(s)
        else:
            if len(s) > 120:
                return False, "过长（≤120 字符）"
            return True, s
    except (TypeError, ValueError):
        return False, "不是合法的数字"
    if f.minimum is not None and v < f.minimum:
        return False, f"不能小于 {f.minimum:g}"
    if f.maximum is not None and v > f.maximum:
        return False, f"不能大于 {f.maximum:g}"
    return True, v


def write(key: str, raw) -> tuple:
    """校验并写 AppSetting。返回 (ok, err)。"""
    from django.core.cache import cache

    f = REG[key.lower()]
    ok, val = validate(f, raw)
    if not ok:
        return False, f"{f.label}：{val}"
    AppSetting.objects.update_or_create(key=f.key, defaults={"value": str(val)})
    try:
        cache.delete(f"settingkit:{f.key}")
    except Exception:  # noqa: BLE001
        pass
    return True, ""


def reset(key: str):
    """删掉 AppSetting 行，回退 .env / 默认值。"""
    from django.core.cache import cache

    f = REG[key.lower()]
    AppSetting.objects.filter(key=f.key).delete()
    try:
        cache.delete(f"settingkit:{f.key}")
    except Exception:  # noqa: BLE001
        pass
