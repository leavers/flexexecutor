import threading
import time
from concurrent.futures.thread import BrokenThreadPool

import pytest
from pytest_mock import MockerFixture

from flexexecutor import ThreadPoolExecutor
from tests.conftest import alive_threads, wait_for_alive_threads


def simple_delay_return(n: int = 1, wait: float = 0.1):
    time.sleep(wait)
    return n


def test_simple_run():
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(lambda: 1)
    assert future.result() == 1
    executor.shutdown()

    assert len(alive_threads(executor)) == 0


def test_with_statement(mocker: MockerFixture):
    with ThreadPoolExecutor(max_workers=1) as executor:
        spy = mocker.spy(executor, "shutdown")
        future = executor.submit(lambda: 1)
        assert future.result() == 1

    spy.assert_called_once()
    assert len(alive_threads(executor)) == 0


def test_multiple_tasks_on_same_thread():
    with ThreadPoolExecutor() as executor:
        f0 = executor.submit(lambda: threading.get_ident())
        ident0 = f0.result()

        f1 = executor.submit(lambda: threading.get_ident())
        ident1 = f1.result()

    assert ident0 == ident1


def test_multiple_tasks_on_multiple_threads():
    def func():
        ident = threading.get_ident()
        time.sleep(0.2)
        return ident

    with ThreadPoolExecutor() as executor:
        futures = [executor.submit(func) for _ in range(10)]

    idents = [f.result() for f in futures]
    assert len(set(idents)) == len(idents)


def test_prohibit_sync_function():
    async def func():
        return 1

    with pytest.raises(TypeError):
        ThreadPoolExecutor().submit(func)


def test_check_broken_pool():
    executor = ThreadPoolExecutor()
    executor._broken = True
    with pytest.raises(BrokenThreadPool):
        executor.submit(lambda: 1)


def test_check_executor_shutdown():
    executor = ThreadPoolExecutor()
    with executor:
        executor.submit(lambda: 1)
    with pytest.raises(RuntimeError):
        executor.submit(lambda: 1)


def test_initializer():
    called_once = False
    return_value = None

    def initializer(val):
        nonlocal called_once, return_value
        called_once = True
        return_value = val
        return val

    executor = ThreadPoolExecutor(
        max_workers=1,
        initializer=initializer,
        initargs=("test",),
    )
    future = executor.submit(lambda: 1)
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

    executor = ThreadPoolExecutor(max_workers=1, initializer=initializer)
    with pytest.raises(BrokenThreadPool):
        future = executor.submit(lambda: 1)
        future.result()  # error is raised here

    with pytest.raises(BrokenThreadPool):
        # when submitted second time, error will be raised on submit
        executor.submit(lambda: 1)


def test_worker_alive():
    with ThreadPoolExecutor(idle_timeout=0.2) as executor:
        assert len(alive_threads(executor)) == 0
        time.sleep(0.2)
        assert len(alive_threads(executor)) == 0
        executor.submit(lambda: 1)
        assert len(alive_threads(executor)) == 1
        assert wait_for_alive_threads(executor, 0, 0.5) == 0


def test_max_workers():
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(simple_delay_return, wait=0.3) for _ in range(4)]
        while True:
            assert len(alive_threads(executor)) <= 2
            if len([f for f in futures if not f.done()]) == 0:
                break


def test_finite_timeout():
    with ThreadPoolExecutor(max_workers=1, idle_timeout=0.1) as executor:
        future = executor.submit(lambda: time.sleep(0.1))
        assert len(alive_threads(executor)) == 1
        future.result()

        assert wait_for_alive_threads(executor, 0, 0.5) == 0
        executor.submit(lambda: time.sleep(0.1))
        assert wait_for_alive_threads(executor, 1, 0.1) == 1

    assert len(alive_threads(executor)) == 0


def test_infinite_timeout():
    with ThreadPoolExecutor(idle_timeout=None) as executor:
        assert executor._idle_timeout == -1
        executor.submit(lambda: 1)
        assert len(alive_threads(executor)) == 1

        futures = [executor.submit(time.sleep, 0.2) for _ in range(10)]

        list([f.result() for f in futures])
        assert len(alive_threads(executor)) == len(futures)

    assert len(alive_threads(executor)) == 0


def test_wait_futures_on_shutdown():
    with ThreadPoolExecutor() as executor:
        f = executor.submit(simple_delay_return, wait=0.3)
        assert f.done() is False
    assert f.done() is True
    assert f.result() == 1


def test_atexit():
    import flexexecutor
    from flexexecutor import _python_exit

    executor = ThreadPoolExecutor()
    executor.submit(lambda: 1)
    assert len(alive_threads(executor)) == 1

    try:
        _python_exit()
        assert len(alive_threads(executor)) == 0
        with pytest.raises(RuntimeError):
            executor.submit(lambda: 1)
        assert len(alive_threads(executor)) == 0
    finally:
        flexexecutor._shutdown = False


def test_handle_executor_deleted_gracefully():
    import weakref

    executor = ThreadPoolExecutor()
    executor_ref = weakref.ref(executor)
    f = executor.submit(simple_delay_return, wait=0.5)
    del executor
    assert executor_ref() is None
    assert f.result() == 1
