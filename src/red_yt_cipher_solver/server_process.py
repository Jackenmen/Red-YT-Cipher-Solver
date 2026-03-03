import asyncio
import logging
import os
import signal
import subprocess
import sys
import time
from types import TracebackType
from typing import Any

import aiohttp
from typing_extensions import Self

__all__ = ("ProcessStartError", "RestartError", "SolverServerProcess")

_DEFAULT_CLIENT_TIMEOUT = aiohttp.ClientTimeout(sock_connect=1.0, total=2.0)
_IS_WINDOWS = sys.platform == "win32"

log = logging.getLogger(__name__)


class ProcessStartError(Exception):
    """The server failed to start."""


class RestartError(Exception):
    """The server failed to be restarted."""


class SolverServerProcess:
    """
    An YT cipher server ran in a subprocess.

    Parameters
    ----------
    host: str
        The host to listen on.
    port: int
        The port to listen on.
    token: str
        The token that the server should require from requests, if any.
    log_file: Path | str
        A path to a file that solver server should log to.
    start_timeout: float
        The time to wait for the process to start responding to requests.
        If this is exceeded, the `start()` method will raise a `TimeoutError`.
    client_timeout: aiohttp.ClientTimeout
        The `aiohttp.ClientTimeout` instance passed on every request attempt
        during the `start()` execution and, once started, when regularly
        testing connection healthiness in the `run()` method to decide,
        if the server needs to be restarted.
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 2334,
        *,
        log_file: os.PathLike | str = "",
        token: str = "",
        start_timeout: float = 10.0,
        client_timeout: aiohttp.ClientTimeout = _DEFAULT_CLIENT_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.log_file = log_file
        self.start_timeout = start_timeout
        self._client_timeout = client_timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._stopped = asyncio.Event()
        self._running = False
        self._restarting = False
        self._proc_watcher: asyncio.Task[None] | None = None
        self._health_checker: asyncio.Task[None] | None = None
        self._restart_task: asyncio.Task[None] | None = None
        self._canceled_tasks: set[asyncio.Task[Any]] = set()
        self._session = aiohttp.ClientSession(timeout=self._client_timeout, raise_for_status=True)
        if self.token:
            self._session.headers["Authorization"] = self.token

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        """Close all of the resources held by this object."""
        await self.stop()
        await self._session.close()

    def is_running(self) -> bool:
        """Whether the server is currently running."""
        return self._running

    def is_restarting(self) -> bool:
        """Whether the server is currently restarting."""
        return self._restarting

    def _safe_cancel(self, task: asyncio.Task[Any]) -> None:
        # https://github.com/python/cpython/issues/91887
        task.add_done_callback(self._canceled_tasks.discard)
        task.cancel()
        self._canceled_tasks.add(task)

    def _get_args(self) -> tuple[str, ...]:
        args = [sys.executable, "-m", "red_yt_cipher_solver", "serve", self.host, str(self.port)]
        if self.log_file:
            log_file = os.fspath(self.log_file)
            args.append("--log-file")
            args.append(log_file)

        return tuple(args)

    async def start(self) -> None:
        """
        Start the YT cipher server in a subprocess.

        Raises
        ------
        TimeoutError
            The server did not start in time.
        aiohttp.ClientResponseError
            The server returned an unexpected response.
        """
        if self._proc is not None:
            raise RuntimeError("The server is already starting/running!")

        env = os.environ.copy()
        if self.token:
            env["RED_YT_CIPHER_SERVER_TOKEN"] = self.token
        log.debug("Starting a solver server process...")
        self._proc = await asyncio.create_subprocess_exec(
            *self._get_args(),
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if _IS_WINDOWS else 0,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        process_start = time.perf_counter()

        base_url = self.base_url
        start_timeout = self.start_timeout
        wait = True
        while wait:
            wait = (time.perf_counter() - process_start) < start_timeout
            if self._proc.returncode is not None:
                raise ProcessStartError("The process failed to start.")

            request_start = time.perf_counter()
            try:
                async with self._session.get(base_url) as resp:
                    await resp.json()
            except aiohttp.ClientResponseError as exc:
                if exc.code >= 500:
                    pass
                raise
            except (aiohttp.ClientConnectionError, asyncio.TimeoutError):
                pass
            else:
                break
            request_duration = time.perf_counter() - request_start
            await asyncio.sleep(1.0 - request_duration)
        else:
            await self.stop(timeout=1.0)
            raise TimeoutError("The server did not start in time.")

        self._running = True
        self._health_checker = asyncio.create_task(self._health_check_loop())
        self._proc_watcher = asyncio.create_task(self._watch_process(self._proc))
        log.info("A solver server process has been started successfully.")

    async def stop(self, *, timeout: float = 5.0) -> None:
        """
        Stop the YT cipher server.

        Parameters
        ----------
        timeout: float
            Time to wait for process to terminate after sending interrupt signal
            before proceeding to kill it instead.
        """
        await self._stop_process(timeout=timeout)
        self._stop_cleanup()

    async def _stop_process(self, *, timeout: float) -> None:
        if self._proc_watcher is not None:
            self._safe_cancel(self._proc_watcher)
            self._proc_watcher = None
        if self._health_checker is not None:
            self._safe_cancel(self._health_checker)
            self._health_checker = None
        if self._proc is not None:
            try:
                self._proc.send_signal(signal.CTRL_C_EVENT if _IS_WINDOWS else signal.SIGINT)
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
                self._proc.kill()
                await self._proc.wait()
            except ProcessLookupError:
                pass
            else:
                log.info("A solver server process has been stopped.")
            self._proc = None
        self._running = False

    def _stop_cleanup(self) -> None:
        if self._restart_task is not None:
            self._safe_cancel(self._restart_task)
        self._stopped.set()

    async def restart(self, *, stop_timeout: float = 5.0) -> None:
        """
        Restart the YT cipher server.

        Parameters
        ----------
        stop_timeout: float
            Time to wait for process to terminate after sending interrupt signal
            before proceeding to kill it instead.

        Raises
        ------
        RestartError
            The server failed to restart.
        """
        self._restarting = True
        try:
            await self._stop_process(timeout=stop_timeout)
            await self.start()
        except Exception as exc:
            raise RestartError("The server failed to be restarted") from exc
        finally:
            self._restarting = False

    def _restart_done(self, task: asyncio.Task) -> None:
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            pass
        else:
            log.error("Restart task crashed due to an exception", exc_info=exc)
        self._stop_cleanup()

    def _schedule_restart(self) -> None:
        if self._restarting:
            return
        if self._restart_task is not None:
            self._safe_cancel(self._restart_task)
            self._restart_task = None
        self._restart_task = asyncio.create_task(self.restart())
        self._restart_task.add_done_callback(self._restart_done)

    async def _health_check_loop(self) -> None:
        max_failed_attempts = 5
        failed_attempts = 0

        base_url = self.base_url
        while failed_attempts < max_failed_attempts:
            await asyncio.sleep(15)
            try:
                async with self._session.get(base_url) as resp:
                    await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError):
                failed_attempts += 1
            else:
                failed_attempts = 0

        log.info("The solver server failed a health check, attempting a process restart...")
        self._schedule_restart()

    async def _watch_process(self, proc: asyncio.subprocess.Process, /) -> None:
        await proc.wait()
        self._proc = None
        log.info(
            "The solver server process terminated unexpectedly, attempting a process restart..."
        )
        self._schedule_restart()

    async def run(self) -> None:
        """
        Run the YT cipher server.

        This function will not return until the server is stopped.

        The `close()` method()

        Raises
        ------
        TimeoutError
            The server did not start in time.
        aiohttp.ClientResponseError
            The server returned an unexpected response, while starting.
        RestartError
            The server failed to restart.
        """
        async with self:
            await self.start()
            await self._stopped.wait()
            if self._restart_task is not None:
                await self._restart_task
