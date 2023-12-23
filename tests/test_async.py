from flexexecutor import AsyncPoolExecutor


async def simple_return(n: int = 1):
    return n


def test_simple_run():
    executor = AsyncPoolExecutor()
    future = executor.submit(simple_return)
    assert future.result() == 1
    executor.shutdown()
