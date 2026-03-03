import re

import yarl

_STS_RE = re.compile(r"(signatureTimestamp|sts):(\d+)")
VALID_YT_HOSTNAMES = ("youtube.com", "www.youtube.com", "m.youtube.com")


__all__ = ("VALID_YT_HOSTNAMES", "normalize_player_url", "get_sts")


def normalize_player_url(player_url: str) -> str:
    """
    Normalize the provided player URL.

    This will prepend the YT URL to a path-only URL in case of relative URLs
    and validate the URL and its hostname in case of absolute URLs.

    Parameters
    -------
    player_url: str
        The player URL.

    Returns
    -------
    str
        The normalized player URL.
    """
    if player_url.startswith("/"):
        if player_url.startswith("/s/player"):
            return f"https://www.youtube.com{player_url}"
        raise ValueError(f"invalid player path: {player_url}")

    try:
        url = yarl.URL(player_url)
        if url.host in VALID_YT_HOSTNAMES:
            return player_url
        raise ValueError(f"unexpected hostname in player url: {player_url}")
    except ValueError as exc:
        raise ValueError(f"invalid player url: {player_url}") from exc


def get_sts(player_content: str) -> str:
    """
    Get timestamp from the player script.

    Parameters
    ----------
    player_content: str
        The content of the player script.

    Returns
    -------
    str
        The timestamp extracted from the player script.
        When this is an empty string, the timestamp could not be found.
    """
    match = _STS_RE.search(player_content)
    if not match:
        return ""
    return match.group(2)
