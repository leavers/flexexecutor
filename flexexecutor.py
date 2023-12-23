import asyncio
import atexit
import itertools
from concurrent.futures import ProcessPoolExecutor, _base
from concurrent.futures import thread as _thread
from inspect import iscoroutinefunction
from queue import Empty
from threading import Event, Lock, Thread
from time import monotonic
from weakref import WeakKeyDictionary, ref

__all__ = (
    "__version__",
    "AsyncPoolExecutor",
    "ProcessPoolExecutor",
    "ThreadPoolExecutor",
)

__version__ = "0.0.1"

_threads_queues = WeakKeyDictionary()  # type: ignore
_shutdown = False
_global_shutdown_lock = Lock()


def _python_exit():
    global _shutdown
    with _global_shutdown_lock:
        _shutdown = True
    items = list(_threads_queues.items())
    for t, q in items:
        q.put(None)
    for t, q in items:
        t.join()


atexit.register(_python_exit)


def _worker(executor_ref, work_queue, initializer, initargs, idle_timeout):
    if initializer is not None:
        try:
            initializer(*initargs)
        except BaseException:
            _base.LOGGER.critical("Exception in initializer:", exc_info=True)
            executor = executor_ref()
            if executor is not None:
                executor._initializer_failed()
            return

    idle_tick = monotonic()
    try:
        while True:
            if idle_timeout >= 0 and monotonic() - idle_tick > idle_timeout:
                executor = executor_ref()
                if executor is not None:
                    executor._idle_semaphore.acquire(timeout=0)
                break
            try:
                work_item = work_queue.get(block=True, timeout=0.1)
            except Empty:
                continue
            if work_item is not None:
                work_item.run()
                del work_item

                executor = executor_ref()
                if executor is not None:
                    executor._idle_semaphore.release()
                del executor
                idle_tick = monotonic()
                continue
            executor = executor_ref()
            if _thread._shutdown or executor is None or executor._shutdown:
                if executor is not None:
                    executor._shutdown = True
                work_queue.put(None)
                return
            del executor
    except BaseException:
        _base.LOGGER.critical("Exception in worker", exc_info=True)


class ThreadPoolExecutor(_thread.ThreadPoolExecutor):
    def __init__(
        self,
        max_workers=None,
        thread_name_prefix="",
        initializer=None,
        initargs=(),
        idle_timeout=60.0,
    ):
        super().__init__(max_workers, thread_name_prefix, initializer, initargs)
        if idle_timeout is None or idle_timeout < 0:
            self._idle_timeout = -1
        else:
            self._idle_timeout = max(0.1, idle_timeout)

    def _adjust_thread_count(self):
        if self._idle_semaphore.acquire(timeout=0):
            return
        threads = self._threads
        dead_threads = [t for t in threads if not t.is_alive()]
        for t in dead_threads:
            threads.remove(t)  # type: ignore

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(threads)
        if num_threads < self._max_workers:
            t = Thread(
                name=f"{self._thread_name_prefix or self}_{num_threads}",
                target=_worker,
                args=(
                    ref(self, weakref_cb),
                    self._work_queue,
                    self._initializer,
                    self._initargs,
                    self._idle_timeout,
                ),
            )
            t.start()
            threads.add(t)  # type: ignore
            _threads_queues[t] = self._work_queue


class _AsyncWorkItem(_thread._WorkItem):
    async def run(self):
        if not self.future.set_running_or_notify_cancel():
            return

        try:
            result = await self.fn(*self.args, **self.kwargs)
            self.future.set_result(result)
        except BaseException as exc:
            self.future.set_exception(exc)
        finally:
            del self


async def _async_worker(
    executor_ref,
    work_queue,
    initializer,
    initargs,
    max_workers,
    idle_timeout,
):
    executor = executor_ref()
    if executor is not None:
        executor._running.set()

    if initializer is not None:
        try:
            initializer(*initargs)
        except BaseException:
            _base.LOGGER.critical("Exception in initializer:", exc_info=True)
            if executor is not None:
                executor._running.set()
                executor._initializer_failed()
            return

    idle_tick = monotonic()
    curr_tasks = set()
    loop = asyncio.get_running_loop()
    asleep = asyncio.sleep

    try:
        while True:
            if idle_timeout >= 0 and monotonic() - idle_tick > idle_timeout:
                executor = executor_ref()
                if executor is not None:
                    executor._idle_semaphore.acquire(timeout=0)
                break
            try:
                work_item = work_queue.get(block=True, timeout=0.1)
            except Empty:
                pass
            if work_item is not None:
                task = loop.create_task(work_item.run())
                curr_tasks.add(task)
                await asleep(0)  # ugly but working
                del work_item

                finished_tasks = [t for t in curr_tasks if t.done()]
                for t in finished_tasks:
                    curr_tasks.remove(t)
                if curr_tasks:
                    idle_tick = monotonic()
                continue

            executor = executor_ref()
            if _thread._shutdown or executor is None or executor._shutdown:
                if executor is not None:
                    executor._shutdown = True
                work_queue.put(None)

                for t in curr_tasks:
                    await t
                return
            del executor
    except BaseException:
        _base.LOGGER.critical("Exception in worker", exc_info=True)
    finally:
        executor = executor_ref()
        if executor is not None:
            executor._running.clear()


class AsyncWorker(Thread):
    def __init__(
        self,
        name,
        executor_ref,
        work_queue,
        initializer,
        initargs,
        max_workers,
        idle_timeout,
    ):
        super().__init__(name=name)
        self._executor_ref = executor_ref
        self._work_queue = work_queue
        self._initializer = initializer
        self._initargs = initargs
        self._max_workers = max_workers
        self._idle_timeout = idle_timeout

    def run(self):
        loop = asyncio.new_event_loop()
        loop.run_until_complete(
            _async_worker(
                self._executor_ref,
                self._work_queue,
                self._initializer,
                self._initargs,
                self._max_workers,
                self._idle_timeout,
            )
        )


class AsyncPoolExecutor(_thread.ThreadPoolExecutor):
    _counter = itertools.count().__next__

    def __init__(
        self,
        max_workers=None,
        thread_name_prefix="",
        initializer=None,
        initargs=(),
        idle_timeout=60.0,
    ):
        if max_workers is None:
            max_workers = 262144
        if not thread_name_prefix:
            thread_name_prefix = f"AsyncPoolExecutor-{self._counter()}"  # type: ignore
        super().__init__(max_workers, thread_name_prefix, initializer, initargs)
        del self._idle_semaphore
        self._running = Event()
        if idle_timeout is None or idle_timeout < 0:
            self._idle_timeout = -1
        else:
            self._idle_timeout = max(0.1, idle_timeout)

    def submit(self, fn, /, *args, **kwargs):
        if not iscoroutinefunction(fn):
            raise TypeError("fn must be a coroutine function")
        with self._shutdown_lock, _global_shutdown_lock:
            if self._broken:
                raise _thread.BrokenThreadPool(self._broken)

            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            if _thread._shutdown:
                raise RuntimeError(
                    "cannot schedule new futures after interpreter shutdown"
                )

            f = _base.Future()  # type: ignore
            w = _AsyncWorkItem(f, fn, args, kwargs)

            self._work_queue.put(w)
            self._adjust_thread_count()
            return f

    submit.__doc__ = _base.Executor.submit.__doc__

    def _adjust_thread_count(self):
        if self._running.is_set():
            return
        threads = self._threads
        threads.clear()  # type: ignore

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        w = AsyncWorker(
            f"{self._thread_name_prefix or self}_0",
            ref(self, weakref_cb),
            self._work_queue,
            self._initializer,
            self._initargs,
            self._max_workers,
            self._idle_timeout,
        )
        w.start()
        threads.add(w)  # type: ignore
        _threads_queues[w] = self._work_queue
