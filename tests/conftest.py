import time
from typing import Union

from flexexecutor import AsyncPoolExecutor, ThreadPoolExecutor

AnyPoolExecutor = Union[AsyncPoolExecutor, ThreadPoolExecutor]


def alive_threads(executor: AnyPoolExecutor):
    return [t for t in executor._threads if t.is_alive()]


def wait_for_alive_threads(
    executor: AnyPoolExecutor,
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
