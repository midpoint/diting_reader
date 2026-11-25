"""跨平台 flock 兼容层。

Unix 直接复用 fcntl.flock；Windows 用 msvcrt.locking 模拟同样的接口
（flock(fd, flags) + LOCK_EX / LOCK_NB / LOCK_UN 常量），使调用方代码不变。

用法（在原来 `import fcntl` 的地方）：:

    try:
        import fcntl
    except ImportError:  # Windows 没有 fcntl
        from core import flock_compat as fcntl

设计要点：
- msvcrt.locking 锁的是「字节记录」，空文件需先写入 1 字节占位，否则锁会失败。
- LOCK_NB 用 LK_NBLCK，失败立即抛 OSError —— 与 fcntl 的非阻塞语义一致。
- 阻塞锁（无 LOCK_NB）自己做最多 30 秒重试，避免 msvcrt.LK_LOCK 只重试 1 秒就放弃。
"""
import os
import sys
import time

if sys.platform == "win32":
    import msvcrt

    # 与 POSIX 保持一致的值，调用方可能做位或运算
    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8

    def flock(fd, operation):
        # 记录锁要求文件至少有 1 字节
        try:
            if os.fstat(fd).st_size == 0:
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, b"\0")
        except OSError:
            pass
        os.lseek(fd, 0, os.SEEK_SET)

        if operation & LOCK_UN:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        elif operation & LOCK_NB:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            deadline = time.monotonic() + 30
            while True:
                try:
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    return
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.05)
else:
    import fcntl as _fcntl

    LOCK_EX = _fcntl.LOCK_EX
    LOCK_NB = _fcntl.LOCK_NB
    LOCK_UN = _fcntl.LOCK_UN
    flock = _fcntl.flock
