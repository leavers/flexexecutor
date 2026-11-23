import multiprocessing as mp
import os
import sys
import time

import pytest

from flexexecutor import ProcessPoolExecutor


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_simple_run():
    with ProcessPoolExecutor(max_workers=1, idle_timeout=0.2) as executor:
        assert executor.submit(pow, 2, 10).result() == 1024
    assert not executor._processes


def test_map():
    with ProcessPoolExecutor(max_workers=2, idle_timeout=0.2) as executor:
        assert list(executor.map(pow, [2, 3], [2, 2])) == [4, 9]


@pytest.mark.skipif(sys.version_info < (3, 14), reason="buffersize added in 3.14")
def test_map_buffersize():
    with ProcessPoolExecutor(max_workers=1, idle_timeout=0.2) as executor:
        result = list(executor.map(pow, [2, 3], [2, 2], buffersize=2))
    assert result == [4, 9]


def test_max_tasks_per_child_replaces_process():
    with ProcessPoolExecutor(
        max_workers=1,
        max_tasks_per_child=1,
        idle_timeout=None,
    ) as executor:
        first_pid = executor.submit(os.getpid).result()
        second_pid = executor.submit(os.getpid).result()
    assert first_pid != second_pid


def test_max_tasks_per_child_validation():
    with pytest.raises(ValueError):
        ProcessPoolExecutor(max_tasks_per_child=0)
    with pytest.raises(ValueError):
        ProcessPoolExecutor(max_tasks_per_child=-1)
    with pytest.raises(TypeError):
        ProcessPoolExecutor(max_tasks_per_child="1")


def test_idle_worker_is_replaced():
    with ProcessPoolExecutor(max_workers=1, idle_timeout=0.2) as executor:
        first_pid = executor.submit(os.getpid).result()
        assert wait_until(lambda: not executor._processes)
        second_pid = executor.submit(os.getpid).result()
    assert first_pid != second_pid


def test_idle_timeout_none_keeps_worker():
    with ProcessPoolExecutor(max_workers=1, idle_timeout=None) as executor:
        first_pid = executor.submit(os.getpid).result()
        time.sleep(0.5)
        assert executor._processes is not None
        assert first_pid in executor._processes
        second_pid = executor.submit(os.getpid).result()
    assert first_pid == second_pid


def test_idle_timeout_uses_non_fork_context():
    with ProcessPoolExecutor(max_workers=1) as executor:
        assert executor._mp_context.get_start_method() != "fork"


def test_idle_timeout_none_preserves_default_context():
    with ProcessPoolExecutor(max_workers=1, idle_timeout=None) as executor:
        assert (
            executor._mp_context.get_start_method()
            == mp.get_context().get_start_method()
        )


@pytest.mark.skipif(sys.platform == "win32", reason="fork is unavailable")
def test_explicit_fork_with_idle_timeout_is_rejected():
    with pytest.raises(ValueError, match="idle_timeout is incompatible"):
        ProcessPoolExecutor(
            max_workers=1,
            mp_context=mp.get_context("fork"),
            idle_timeout=60,
        )


def test_shutdown_cancel_futures():
    executor = ProcessPoolExecutor(max_workers=1, idle_timeout=0.2)
    future = executor.submit(time.sleep, 10)
    executor.shutdown(wait=False, cancel_futures=True)
    with pytest.raises(Exception):
        future.result(timeout=1)


def test_initializer_failure_breaks_pool():
    from concurrent.futures.process import BrokenProcessPool

    with ProcessPoolExecutor(
        max_workers=1,
        initializer=int,
        initargs=("x",),
        idle_timeout=0.2,
    ) as executor:
        with pytest.raises(BrokenProcessPool):
            executor.submit(pow, 2, 3).result(timeout=5)
        with pytest.raises(BrokenProcessPool):
            executor.submit(pow, 2, 3)


def test_max_tasks_with_pending_work():
    with ProcessPoolExecutor(
        max_workers=1,
        max_tasks_per_child=1,
        idle_timeout=None,
    ) as executor:
        futures = [executor.submit(pow, 2, i) for i in range(5)]
        assert [future.result() for future in futures] == [1, 2, 4, 8, 16]
