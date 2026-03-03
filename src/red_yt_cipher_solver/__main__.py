from __future__ import annotations

import argparse
import asyncio
import dataclasses
import enum
import json
import logging
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, NoReturn, TypeVar

import aiohttp
import yarl
from aiohttp import web
from typing_extensions import Self

from . import _platform_support, challenges, player

_T = TypeVar("_T")
log = logging.getLogger(__name__)


def post_route(path: str) -> Callable[[_T], _T]:
    def decorator(func: _T) -> _T:
        app_routes = getattr(func, "__app_routes__", [])
        app_routes.append(("POST", path))
        setattr(func, "__app_routes__", app_routes)
        return func

    return decorator


def get_required_key(payload: dict[str, str], key: str) -> str:
    try:
        return payload[key]
    except KeyError:
        raise web.HTTPBadRequest(reason=f"required key {key} is missing") from None


class SolverServer:
    def __init__(self, host: str, port: int, *, token: str = "") -> None:
        self.host = host
        self.port = port
        self.token = token
        self.web_app = web.Application(middlewares=[self._auth_middleware, self._error_middleware])
        self.web_app.add_routes(self._get_routes())
        self._stopped = asyncio.Event()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._session: aiohttp.ClientSession

    @web.middleware
    async def _auth_middleware(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        if self.token:
            auth_header = request.headers.get("Authorization")
            if auth_header != self.token:
                raise web.HTTPUnauthorized()

        return await handler(request)

    @web.middleware
    async def _error_middleware(
        self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]
    ) -> web.StreamResponse:
        try:
            return await handler(request)
        except web.HTTPException as ex:
            if ex.empty_body:
                raise
            return web.json_response({"error": ex.reason}, status=ex.status_code)

    def _get_routes(self) -> list[web.RouteDef]:
        routes: list[web.RouteDef] = []
        for base in reversed(self.__class__.__mro__):
            for attr_name, attr_value in base.__dict__.items():
                app_routes = getattr(attr_value, "__app_routes__", None)
                if not app_routes:
                    continue
                method = getattr(self, attr_name)
                for route_method, route_path in app_routes:
                    routes.append(web.route(route_method, route_path, method))
        return routes

    async def __aenter__(self) -> Self:
        await self.async_initialize()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
        /,
    ) -> None:
        await self.close()

    async def async_initialize(self) -> None:
        self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        await self._session.close()

    async def start(self) -> None:
        log.info("Starting the server...")
        self._runner = web.AppRunner(self.web_app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        log.info("The server has been started on http://%s:%s", self.host, self.port)

    async def stop(self) -> None:
        log.info("Stopping the server...")
        if self._site is not None:
            await self._site.stop()
        if self._runner is not None:
            await self._runner.cleanup()
        self._stopped.set()

    async def run(self) -> int:
        self._stopped.clear()
        await self.start()
        try:
            await self._stopped.wait()
        except asyncio.CancelledError:
            await self.stop()
            raise

        return 0

    @post_route("/get_sts")
    async def get_sts(self, request: web.Request) -> web.Response:
        payload = await request.json()
        try:
            player_url = player.normalize_player_url(get_required_key(payload, "player_url"))
        except ValueError as exc:
            raise web.HTTPBadRequest(reason=str(exc)) from None

        player_content = await _get_player_content(self.session, player_url)
        sts = player.get_sts(player_content)
        if not sts:
            raise web.HTTPNotFound(reason="timestamp could not be found in the player script")

        return web.json_response({"sts": sts})

    @post_route("/resolve_url")
    async def resolve_url(self, request: web.Request) -> web.Response:
        payload = await request.json()
        try:
            player_url = player.normalize_player_url(get_required_key(payload, "player_url"))
        except ValueError as exc:
            raise web.HTTPBadRequest(reason=str(exc)) from None
        encrypted_signature = payload.get("encrypted_signature")
        signature_key = payload.get("signature_key") or "sig"

        try:
            stream_url = yarl.URL(get_required_key(payload, "stream_url"))
        except ValueError:
            raise web.HTTPBadRequest(reason="stream URL appears to be invalid") from None
        n_param = payload.get("n_param")
        if not n_param:
            n_param = stream_url.query.get("n")
        if not n_param:
            raise web.HTTPBadRequest(reason="n_param not found in request or stream_url")

        player_content = await _get_player_content(self._session, player_url)
        challenge_requests: list[challenges.JsChallengeRequest] = []

        n_challenge_request = challenges.NChallengeRequest([n_param])
        challenge_requests.append(n_challenge_request)

        sig_challenge_request: challenges.SigChallengeRequest | None = None
        if encrypted_signature:
            sig_challenge_request = challenges.SigChallengeRequest([encrypted_signature])
            challenge_requests.append(sig_challenge_request)

        try:
            solve_output = await challenges.solve_js_challenges(
                player_content, *challenge_requests
            )
        except challenges.SolveOutputError as exc:
            if not exc.responses:
                raise web.HTTPNotFound(
                    reason=f"error occurred during challenge request: {exc}"
                ) from exc
            n_challenge_response = exc[n_challenge_request]
            sig_challenge_response = exc[sig_challenge_request] if sig_challenge_request else None
            if sig_challenge_response is not None and not sig_challenge_response:
                raise web.HTTPNotFound(
                    reason=f"error occurred during challenge request: {exc}"
                ) from exc
        else:
            n_challenge_response = solve_output[n_challenge_request]
            sig_challenge_response = (
                solve_output[sig_challenge_request] if sig_challenge_request else None
            )

        query = stream_url.query.copy()

        if n_challenge_response:
            query["n"] = n_challenge_response.solutions[0]

        if sig_challenge_response:
            query[signature_key] = sig_challenge_response.solutions[0]
            query.pop("s", None)

        resolved_url = stream_url.with_query(query)

        return web.json_response({"resolved_url": str(resolved_url)})


async def _get_player_content(session: aiohttp.ClientSession, player_url: str) -> str:
    # TODO: cache players
    async with session.get(player_url) as resp:
        return await resp.text()


async def _serve_command(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("RED_YT_CIPHER_SERVER_TOKEN", "")

    async with SolverServer(args.host, args.port, token=token) as server:
        return await server.run()


async def _solve_command(args: argparse.Namespace) -> int:
    player_url = player.normalize_player_url(args.player_url)
    stream_url = yarl.URL(args.stream_url)
    n_param = args.n_param
    if not n_param:
        n_param = stream_url.query.get("n")
    if not n_param:
        print("n_param not provided in stream_url nor by the --n-param option", file=sys.stderr)
        return 2

    challenge_requests: list[challenges.JsChallengeRequest] = []

    n_challenge_request = challenges.NChallengeRequest([n_param])
    challenge_requests.append(n_challenge_request)

    sig_challenge_request = None
    if args.encrypted_signature:
        sig_challenge_request = challenges.SigChallengeRequest([args.encrypted_signature])
        challenge_requests.append(sig_challenge_request)

    async with aiohttp.ClientSession() as session:
        player_content = await _get_player_content(session, player_url)
    try:
        solve_output = await challenges.solve_js_challenges(player_content, *challenge_requests)
    except challenges.SolveOutputError as exc:
        print(f"error occurred during challenge request: {exc}", file=sys.stderr)
        return 2

    query = stream_url.query.copy()

    if solve_output[n_challenge_request]:
        query["n"] = solve_output[n_challenge_request].solutions[0]

    if sig_challenge_request and solve_output[sig_challenge_request]:
        query[args.signature_key] = solve_output[sig_challenge_request].solutions[0]
        query.pop("s", None)

    resolved_url = stream_url.with_query(query)

    data: dict[str, Any] = {}
    if args.include_player_content:
        data["player"] = player_content
        data["preprocessed_player"] = solve_output.preprocessed_player
    data["resolved_url"] = str(resolved_url)
    data["player_url"] = player_url
    data["sts"] = player.get_sts(player_content)
    solve_output_data = dataclasses.asdict(solve_output)
    solve_output_data.pop("preprocessed_player", None)
    data["solve_output"] = solve_output_data

    def default(o: Any) -> Any:
        if isinstance(o, enum.Enum):
            return o.value
        raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")

    print(json.dumps(data, indent=4, default=default))

    return 0


async def _main() -> NoReturn:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(required=True, help="command to run")

    serve_parser = subparsers.add_parser(
        "serve", help="Run a Lavalink-compatible YT cipher server."
    )
    serve_parser.set_defaults(func=_serve_command)
    serve_parser.add_argument("host", nargs="?", default="localhost")
    serve_parser.add_argument("port", type=int, nargs="?", default=2334)

    solve_parser = subparsers.add_parser(
        "solve", help="Solve JS challenge request using the yt-dlp/ejs solver."
    )
    solve_parser.add_argument("player_url")
    solve_parser.add_argument("stream_url")
    solve_parser.add_argument("--encrypted-signature")
    solve_parser.add_argument("--n-param")
    solve_parser.add_argument("--signature-key", default="sig")
    solve_parser.add_argument("--include-player-content", action="store_true", default=False)
    solve_parser.set_defaults(func=_solve_command)

    args = parser.parse_args()
    raise SystemExit(await args.func(args))


def main() -> NoReturn:
    if _platform_support.GLIBC_UNSUPPORTED:
        print(
            f"The minimum supported version of glibc is {_platform_support.MIN_SUPPORTED_GLIBC}",
            file=sys.stderr,
        )
        raise SystemExit(1)

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        raise SystemExit(128 + signal.Signals.SIGINT) from None


if __name__ == "__main__":
    main()
