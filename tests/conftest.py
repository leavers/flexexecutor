from typing import Union

from flexexecutor import AsyncPoolExecutor, ThreadPoolExecutor


def alive_threads(executor: Union[AsyncPoolExecutor, ThreadPoolExecutor]):
    return [t for t in executor._threads if t.is_alive()]