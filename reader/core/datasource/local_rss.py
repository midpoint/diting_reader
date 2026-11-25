"""本地 RSS engine 占位实现（未来移除 Miniflux 依赖时使用）。

目前不实现：M1/M2 只用 MinifluxDataSource。
"""
from .base import DataSource


class LocalRssDataSource(DataSource):
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("LocalRssDataSource 尚未实现（M1/M2 用 MinifluxDataSource）")
