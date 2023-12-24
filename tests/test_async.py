import asyncio
import threading
import time
from concurrent.futures.thread import BrokenThreadPool

import pytest
from pytest_mock import MockerFixture

from flexexecutor import AsyncPoolExecutor
from tests.conftest import alive_threads


async def simple_return(n: int = 1):
    return n


async def simple_sleep_return(n: int = 1, wait: float = 0.1):
    print("start", n)
    await asyncio.sleep(wait)
    print("finish", n)
    return n


def test_simple_run():
    executor = AsyncPoolExecutor()
    future = executor.submit(simple_return)
    assert future.result() == 1
    executor.shutdown()

    assert len(alive_threads(executor)) == 0


def test_with_statement(mocker: MockerFixture):
    with AsyncPoolExecutor(max_workers=1) as executor:
        spy = mocker.spy(executor, "shutdown")
        future = executor.submit(simple_return)
        assert future.result() == 1

    spy.assert_called_once()
    assert len(alive_threads(executor)) == 0


def test_multiple_tasks_on_same_thread():
    async def func():
        return threading.get_ident()

    with AsyncPoolExecutor() as executor:
        f0 = executor.submit(func)
        ident0 = f0.result()

        f1 = executor.submit(func)
        ident1 = f1.result()

    assert ident0 == ident1


def test_prohibit_sync_function():
    with pytest.raises(TypeError):
        AsyncPoolExecutor().submit(lambda: 1)


def test_check_broken_pool():
    executor = AsyncPoolExecutor()
    executor._broken = True
    with pytest.raises(BrokenThreadPool):
        executor.submit(simple_return)


def test_check_executor_shutdown():
    executor = AsyncPoolExecutor()
    with executor:
        executor.submit(simple_return)
    with pytest.raises(RuntimeError):
        executor.submit(simple_return)


def test_initializer():
    called_once = False
    return_value = None

    def initializer(val):
        nonlocal called_once, return_value
        called_once = True
        return_value = val
        return val

    executor = AsyncPoolExecutor(
        max_workers=1,
        initializer=initializer,
        initargs=("test",),
    )
    future = executor.submit(simple_return)
    assert future.result() == 1
    assert called_once is True
    assert return_value == "test"
    executor.shutdown()
    assert len(alive_threads(executor)) == 0


def test_initializer_with_error():
    called_once = False

    def initializer():
        nonlocal called_once
        called_once = True
        raise RuntimeError("test")

    executor = AsyncPoolExecutor(max_workers=1, initializer=initializer)

    with pytest.raises(BrokenThreadPool):
        future = executor.submit(simple_return)
        future.result()  # error is raised here

    with pytest.raises(BrokenThreadPool):
        # when submitted second time, error will be raised on submit
        executor.submit(simple_return)


def test_finite_timeout():
    with AsyncPoolExecutor(max_workers=1, idle_timeout=0.1) as executor:
        executor.submit(simple_sleep_return, wait=0.1)
        assert len(alive_threads(executor)) == 1

        time.sleep(0.3)
        assert len(alive_threads(executor)) == 0

        executor.submit(simple_sleep_return, wait=0.1)
        assert len(alive_threads(executor)) == 1

    assert len(alive_threads(executor)) == 0


def test_infinite_timeout():
    with AsyncPoolExecutor(idle_timeout=None) as executor:
        assert executor._idle_timeout == -1
        executor.submit(simple_return)
        assert len(alive_threads(executor)) == 1

        futures = [executor.submit(simple_sleep_return, n=i, wait=0.2) for i in range(10)]

        list([f.result() for f in futures])
        assert len(alive_threads(executor)) == 1

    assert len(alive_threads(executor)) == 0


def test_atexit():
    import flexexecutor
    from flexexecutor import _python_exit

    executor = AsyncPoolExecutor()
    executor.submit(simple_return)
    assert len(alive_threads(executor)) == 1

    try:
        _python_exit()
        assert len(alive_threads(executor)) == 0
        with pytest.raises(RuntimeError):
            executor.submit(simple_return)
        assert len(alive_threads(executor)) == 0
    finally:
        flexexecutor._shutdown = False
