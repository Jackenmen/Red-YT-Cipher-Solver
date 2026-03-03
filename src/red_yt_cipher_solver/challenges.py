from __future__ import annotations

import asyncio
import dataclasses
import enum
import json
import subprocess
from collections.abc import Iterable
from typing import Final, Literal, TypedDict

import deno
import yt_dlp_ejs.yt.solver
from typing_extensions import NotRequired

from . import _platform_support

_DENO_BIN: Final = deno.find_deno_bin()
_DENO_ARGS: Final = (
    "--ext=js",
    "--no-code-cache",
    "--no-prompt",
    "--no-remote",
    "--no-lock",
    "--node-modules-dir=none",
    "--no-config",
    "--no-npm",
    "--cached-only",
    "-",
)

__all__ = (
    "JsChallengeType",
    "NChallengeRequest",
    "SigChallengeRequest",
    "JsChallengeRequest",
    "JsChallengeResultResponse",
    "JsChallengeErrorResponse",
    "JsChallengeResponse",
    "SolveOutputError",
    "SolveOutput",
    "solve_js_challenges",
    "solve_js_challenges_sync",
)


class JsChallengeType(enum.Enum):
    N = "n"
    SIG = "sig"


@dataclasses.dataclass(frozen=True)
class NChallengeRequest:
    challenges: list[str] = dataclasses.field(default_factory=list)
    type: Literal[JsChallengeType.N] = dataclasses.field(init=False, default=JsChallengeType.N)


@dataclasses.dataclass(frozen=True)
class SigChallengeRequest:
    challenges: list[str] = dataclasses.field(default_factory=list)
    type: Literal[JsChallengeType.SIG] = dataclasses.field(init=False, default=JsChallengeType.SIG)


JsChallengeRequest = NChallengeRequest | SigChallengeRequest


class _PlayerInput(TypedDict):
    type: Literal["player"]
    player: str
    requests: list[_Request]
    output_preprocessed: bool


class _PreprocessedInput(TypedDict):
    type: Literal["preprocessed"]
    preprocessed_player: str
    requests: list[_Request]


_Input = _PlayerInput | _PreprocessedInput


class _Request(TypedDict):
    type: Literal["n", "sig"]
    challenges: list[str]


class _ResultChallengeResponse(TypedDict):
    type: Literal["result"]
    data: dict[str, str]


class _ErrorChallengeResponse(TypedDict):
    type: Literal["error"]
    error: str


_ChallengeResponse = _ResultChallengeResponse | _ErrorChallengeResponse


class _ResultOutput(TypedDict):
    type: Literal["result"]
    preprocessed_player: NotRequired[str]
    responses: list[_ChallengeResponse]


class _ErrorOutput(TypedDict):
    type: Literal["error"]
    error: str


_Output = _ResultOutput | _ErrorOutput


def _construct_stdin(
    player: str, requests: Iterable[JsChallengeRequest], /, *, preprocessed: bool = False
) -> bytes:
    json_requests: list[_Request] = [
        {
            "type": request.type.value,
            "challenges": request.challenges,
        }
        for request in requests
    ]
    data: _Input = (
        {
            "type": "preprocessed",
            "preprocessed_player": player,
            "requests": json_requests,
        }
        if preprocessed
        else {
            "type": "player",
            "player": player,
            "requests": json_requests,
            "output_preprocessed": True,
        }
    )
    return (
        f"{yt_dlp_ejs.yt.solver.lib()}\n"
        "Object.assign(globalThis, lib);\n"
        f"{yt_dlp_ejs.yt.solver.core()}\n"
        f"console.log(JSON.stringify(jsc({json.dumps(data)})));"
    ).encode()


@dataclasses.dataclass(frozen=True)
class JsChallengeResultResponse:
    request: JsChallengeRequest
    data: dataclasses.InitVar[dict[str, str]]
    solutions: tuple[str] = dataclasses.field(init=False)

    def __post_init__(self, data: dict[str, str]) -> None:
        object.__setattr__(
            self, "solutions", tuple(data[challenge] for challenge in self.request.challenges)
        )

    def __bool__(self) -> Literal[True]:
        return True


@dataclasses.dataclass(frozen=True)
class JsChallengeErrorResponse:
    request: JsChallengeRequest
    message: str

    def __bool__(self) -> Literal[False]:
        return False


JsChallengeResponse = JsChallengeResultResponse | JsChallengeErrorResponse


class SolveOutputError(Exception):
    """Failed to solve JS challenge requests."""

    def __init__(
        self,
        requests: tuple[JsChallengeRequest, ...],
        message: str,
        /,
        *,
        responses: tuple[JsChallengeResponse, ...] | None = None,
    ) -> None:
        super().__init__(message)
        self.requests = requests
        self.responses = responses

    def __getitem__(self, request: JsChallengeRequest) -> JsChallengeResponse:
        if self.responses is None:
            raise TypeError("no responses are available")
        return self.responses[self.requests.index(request)]


@dataclasses.dataclass(frozen=True)
class SolveOutput:
    requests: tuple[JsChallengeRequest, ...]
    responses: tuple[JsChallengeResultResponse, ...]
    preprocessed_player: str | None

    def __getitem__(self, request: JsChallengeRequest) -> JsChallengeResultResponse:
        return self.responses[self.requests.index(request)]


def _parse_output(requests: tuple[JsChallengeRequest, ...], stdout: bytes) -> SolveOutput:
    data: _Output = json.loads(stdout)
    if data["type"] == "error":
        raise SolveOutputError(requests, data["error"])

    responses: list[JsChallengeResponse] = []
    result_responses: list[JsChallengeResultResponse] = []
    has_errors = False
    for request, response_data in zip(requests, data["responses"], strict=True):
        if response_data["type"] == "error":
            has_errors = True
            responses.append(JsChallengeErrorResponse(request, response_data["error"]))
        else:
            response = JsChallengeResultResponse(request, response_data["data"])
            responses.append(response)
            result_responses.append(response)

    if has_errors:
        raise SolveOutputError(
            requests,
            "Some of the challenge requests could not be solved",
            responses=tuple(responses),
        )

    return SolveOutput(requests, tuple(result_responses), data.get("preprocessed_player"))


class UnsupportedGLibCError(Exception):
    """The glibc version used on this system is unsupported."""


async def solve_js_challenges(player_content: str, *requests: JsChallengeRequest) -> SolveOutput:
    """
    Solve JS challenge requests using the yt-dlp/ejs solver.

    Parameters
    ----------
    player_content: str
        The content of the player script.
    *requests: JsChallengeRequest
        The JS challenge requests to solve.

    Returns
    -------
    SolveOutput
        The parsed output from yt-dlp/ejs solver.

    Raises
    ------
    SolveOutputError
        Some or all of the challenge requests could not be solved.
    UnsupportedGLibCError
        The glibc version used on this system is unsupported.
    subprocess.CalledProcessError
        The yt-dlp/ejs script crashed/Deno failed to run.
    """
    if _platform_support.GLIBC_UNSUPPORTED:
        raise UnsupportedGLibCError(
            f"The minimum supported version of glibc is {_platform_support.MIN_SUPPORTED_GLIBC}"
        )
    args = (_DENO_BIN, *_DENO_ARGS)
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate(_construct_stdin(player_content, requests))
    if proc.returncode is None:
        raise RuntimeError("unreachable")
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, args, stdout)
    return _parse_output(requests, stdout)


def solve_js_challenges_sync(player_content: str, *requests: JsChallengeRequest) -> SolveOutput:
    """
    Solve JS challenge requests using the yt-dlp/ejs solver.

    Parameters
    ----------
    player_content: str
        The content of the player script.
    *requests: JsChallengeRequest
        The JS challenge requests to solve.

    Returns
    -------
    SolveOutput
        The parsed output from yt-dlp/ejs solver.

    Raises
    ------
    SolveOutputError
        Some or all of the challenge requests could not be solved.
    UnsupportedGLibCError
        The glibc version used on this system is unsupported.
    subprocess.CalledProcessError
        The yt-dlp/ejs script crashed/Deno failed to run.
    """
    if _platform_support.GLIBC_UNSUPPORTED:
        raise UnsupportedGLibCError(
            f"The minimum supported version of glibc is {_platform_support.MIN_SUPPORTED_GLIBC}"
        )
    proc = subprocess.run(
        (_DENO_BIN, *_DENO_ARGS),
        input=_construct_stdin(player_content, requests),
        check=True,
    )
    return _parse_output(requests, proc.stdout)
