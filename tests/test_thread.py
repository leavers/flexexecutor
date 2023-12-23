from flexexecutor import ThreadPoolExecutor


def test_simple_run():
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(lambda: 1)
    assert future.result() == 1
    executor.shutdown()
