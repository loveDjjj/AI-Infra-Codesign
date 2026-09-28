"""在共享文件系统上有界等待排它锁；临时争用不能终止搜索目标。"""
import errno
import fcntl
import time


def acquire(handle, *, timeout=60, interval=.1):
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as exc:
            if exc.errno not in {errno.EAGAIN, errno.EACCES, errno.EINTR}:
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError('搜索状态锁争用超过时限') from exc
            time.sleep(min(interval, max(0, deadline - time.monotonic())))
