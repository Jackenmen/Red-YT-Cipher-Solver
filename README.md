# Red-YT-Cipher-Solver

[![Red-YT-Cipher-Solver on PyPI](https://img.shields.io/pypi/v/Red-YT-Cipher-Solver)](https://pypi.org/project/Red-YT-Cipher-Solver)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A thin wrapper over [yt-dlp/ejs](https://github.com/yt-dlp/ejs) for deciphering YT signatures.
Aside from yt-dlp/ejs, this uses the [official Deno PyPI package](https://github.com/denoland/deno_pypi),
meaning no additional setup is required beyond installing the package and then using it either as a library
or as a [Lavalink-compatible cipher server](https://github.com/lavalink-devs/youtube-source#using-a-remote-cipher-server).

## Using this project

This package only functions on systems supported by Deno JS runtime.
At the time of writing, this includes:
-   Windows x86_64
-   macOS x86_64 & arm64
-   Linux x86_64 & aarch64 with glibc 2.27 or higher

> [!NOTE]
> If you intend to add this as a dependency to your project and want to support other platforms as well,
ensure to specify appropriate environment markers for the dependency and guard your imports appropriately
as the `deno` dependency of this project will not install on unsupported platforms:
> -   `pyproject.toml`
>     ```toml
>     [project]
>     # [...]
>     dependencies = [
>         """\
>         Red-YT-Cipher-Solver; \
>             (sys_platform == 'win32' and platform_machine == 'AMD64') \
>             or (sys_platform == 'linux' and (platform_machine == 'x86_64' or platform_machine == 'aarch64')) \
>             or (sys_platform == 'darwin' and (platform_machine == 'x86_64' or platform_machine == 'arm64')) \
>         """,
>     ]
>
>     # [...]
>     ```
> -   `requirements.txt`
>     ```
>     Red-YT-Cipher-Solver; (sys_platform == 'win32' and platform_machine == 'AMD64') or (sys_platform == 'linux' and (platform_machine == 'x86_64' or platform_machine == 'aarch64')) or (sys_platform == 'darwin' and (platform_machine == 'x86_64' or platform_machine == 'arm64'))
>     # [...]
>     ```

### Installation

Install the package:
-   Linux & macOS
    ```console
    python3.10 -m venv red_yt_cipher_solver
    . red_yt_cipher_solver/bin/activate
    python -m pip install -U Red-YT-Cipher-Solver
    ```
-   Windows
    ```powershell
    py -3.10 -m venv red_yt_cipher_solver
    red_yt_cipher_solver\Scripts\Activate.ps1
    python -m pip install -U Red-YT-Cipher-Solver
    ```

### Running as a server

Run the server with the default configuration (listening on `http://localhost:2334` with no authentication):
```console
red-yt-cipher-solver serve
```

To specify custom hostname and port, use the positional arguments:
```console
red-yt-cipher-solver 0.0.0.0 4242
```

You can require the clients to send an `Authorization` header with a token
by specifying one in the `RED_YT_CIPHER_SERVER_TOKEN` environment variable.

### Using as a standalone solver

```
$ red-yt-cipher-solver solve --help
usage: red-yt-cipher-solver solve [-h] [--encrypted-signature ENCRYPTED_SIGNATURE]
                                  [--n-param N_PARAM] [--signature-key SIGNATURE_KEY]
                                  [--include-player-content]
                                  player_url stream_url
```

Solve a JS challenge request using the yt-dlp/ejs solver:
```console
red-yt-cipher-solver solve \
    /s/player/00c52fa0/player_ias.vflset/de_DE/base.js \
    "https://rr4---sn-4g5e6nzl.googlevideo.com/videoplayback?expire=1772476120&n=Fc9IL2b0xD7Lybd7&ei=..." \
    --encrypted-signature "R=ANHkhNZInqwBBEsHpvykqHsygJje6J4T_Q-aL2VO7PkCQIC4ruoYYg2TeWFSfKXFTeQF=B_hR1UlnJw75Wfb24g6nQgIQRw4MNqEHA"
```

### Using as a library

While this is mostly a thin wrapper over yt-dlp/ejs, it does come with Deno out of the box,
so it might be of interest to some to use that wrapper directly.

The following functions are currently exposed in `red_yt_cipher_solver`

#### `solve_js_challenges()` / `solve_js_challenges_sync()`

Solve JS challenge requests using the yt-dlp/ejs solver.

The variant without the `_sync` suffix is an asynchronous function.

**Arguments:**
-   `player_content` (`str`) - The content of the player script.
-   `*requests` (`JsChallengeRequest`) - The JS challenge requests to solve.

**Returns:**<br>
`SolveOutput` - The parsed output from yt-dlp/ejs solver.

**Raises:**
-   `SolveOutputError` - Some or all of the challenge requests could not be solved.
-   `UnsupportedGLibCError` - The glibc version used on this system is unsupported.
-   `subprocess.CalledProcessError` - The yt-dlp/ejs script crashed/Deno failed to run.

#### `get_sts()`

Get timestamp from the player script.

**Parameters:**
-   `player_content` (`str`) - The content of the player script.

**Returns:**<br>
`str` - The timestamp extracted from the player script. When this is an empty string, the timestamp could not be found.

#### `normalize_player_url()`

Normalize the provided player URL.

This will prepend the YT URL to a path-only URL in case of relative URLs
and validate the URL and its hostname in case of absolute URLs.

**Parameters:**
-   `player_url` (`str`) - The player URL.

**Returns:**<br>
`str` - The normalized player URL.
