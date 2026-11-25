"""全局单例：返回当前使用的数据源实现。"""
from django.conf import settings

from .greader import GReaderDataSource
from .miniflux import MinifluxDataSource

_ds = None


def _make_datasource():
    engine = getattr(settings, "DATASOURCE_ENGINE", "miniflux")
    if engine == "miniflux":
        return MinifluxDataSource()
    if engine == "greader":
        return GReaderDataSource()
    raise NotImplementedError(f"未知数据源：{engine}")


def get_datasource():
    """全局单例：给请求/主线程用。

    ★ `MinifluxDataSource` 内部持有 `requests.Session`，**不是线程安全的**。
      补救刷新要并发时用 `new_datasource()` 给每个 worker 建独立客户端。
    """
    global _ds
    if _ds is None:
        _ds = _make_datasource()
    return _ds


def new_datasource():
    """新建一个独立客户端（线程/并发刷新用，不碰全局单例）。"""
    return _make_datasource()


def reset():
    """丢弃全局单例；下次 get_datasource() 按当前 settings 重建。

    ★ 设置页切换数据源 / 改地址后调用。已在跑的线程各自持有旧实例，
      它们最多跑完当前一轮；新请求与下一轮调度立即用新配置。
    """
    global _ds
    _ds = None
