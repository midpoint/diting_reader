"""后台任务的进程内状态（锁 + dict + 快照）—— 唯一一份实现。

此前「模块级 dict + 锁 + running 标志」在 translate 与 refreshjob 各写一份
（结构同构）。这里是共享原语：

  · `begin(**kw)`   开工：running=True 并重置本轮字段；
  · `update(**kw)`  进度：任意字段覆盖；
  · `mutate(fn)`    复合修改：fn(state) 在锁内执行（跨字段不一致的更新用它）；
  · `finish(**kw)`  收工：running=False；
  · `snapshot()`    锁内拷贝（list/dict 值也拷一层，不给调用方可变引用；
                    嵌套更深的结构由调用方在自己的封装里再补）。

★★ 刻意取舍：状态只活在**进程内存**，重启即丢。单进程设计（Web + 调度
  同进程）下这是接受的代价 —— 任务本身会跑完，只是这一次 UI 拿不到进度；
  前端轮询有超时兜底。需要跨重启的任务状态走数据库（SummaryRun）。
"""
import threading


class JobState:
    def __init__(self, **initial):
        # ★ RLock：start() 持锁时会调 snapshot()（可重入避免自锁）
        self._lock = threading.RLock()
        self._state = {"running": False, **initial}

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._state.get("running"))

    def begin(self, **kw):
        with self._lock:
            self._state.update(running=True, **kw)

    def update(self, **kw):
        with self._lock:
            self._state.update(kw)

    def mutate(self, fn):
        """fn(state) 在锁内执行 —— 多字段要保持一致的更新用它。"""
        with self._lock:
            fn(self._state)

    def finish(self, **kw):
        with self._lock:
            self._state.update(running=False, **kw)

    def snapshot(self) -> dict:
        """锁内拷贝；list/dict **值**也拷一层 —— 调用方拿不到可变引用
        （refreshjob 的教训：前端轮询拿到快照后改了它，会污染任务状态）。
        嵌套更深的结构由调用方在自己的封装里再补（如 refreshjob 的 errors）。
        """
        with self._lock:
            out = {}
            for k, v in self._state.items():
                if isinstance(v, dict):
                    out[k] = dict(v)
                elif isinstance(v, list):
                    out[k] = list(v)
                else:
                    out[k] = v
            return out
