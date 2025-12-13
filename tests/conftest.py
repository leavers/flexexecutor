import time

from flexexecutor import AsyncPoolExecutor, ThreadPoolExecutor

def alive_threads(executor: AsyncPoolExecutor | ThreadPoolExecutor):
    if isinstance(executor, AsyncPoolExecutor):
        threads = [executor._thread] if executor._thread is not None else []
    else:
        threads = list(executor._threads)
    return [t for t in threads if t.is_alive()]


def wait_for_alive_threads(
    executor: AsyncPoolExecutor | ThreadPoolExecutor,
    expect: int,
    timeout: float,
) -> int:
    t = -1
    tick = time.monotonic()
    while True:
        t = len(alive_threads(executor))
        if t == expect:
            break
        if time.monotonic() - tick > timeout:
            break
        time.sleep(0.05)
    return t
