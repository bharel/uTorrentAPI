__author__ = "Bar Harel"
__version__ = "0.1"
__all__ = ["uTorrentAPI"]

import asyncio as _asyncio
import urllib.parse as _parse
import logging as _logging
import re as _re


from functools import wraps as _wraps
from xml.etree import ElementTree as _ElementTree

import aiohttp as _aiohttp

_TOKEN_URL = "token.html"
_logger = _logging.getLogger(__name__)
_logger.addHandler(_logging.NullHandler())
_IP_NETLOC_RE = _re.compile(
    r"(?:(?:[0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\.)"
    r"{3}(?:[0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])(?:$|\:)")


def _renew_token_on_failure(func):
    @_wraps(func)
    async def wrapped(self, *args, **kwargs):
        try:
            return await func(self, *args, **kwargs)
        except ConnectionError:
            await self._renew_token()
            return await func(self, *args, **kwargs)
    return wrapped


class uTorrentAPI:
    """Exposes uTorrent's remote web-based API"""

    def __init__(self, base_url, username, password, *, loop=None):
        """Initializes the API"""

        self.base_url = base_url
        self.username = username
        self.password = password
        self._token = None
        self._cache_id = None
        self._loop = loop or _asyncio.get_event_loop()
        self._client = None
        self._connected = False
        self._cache = {}

    async def connect(self):
        if self._client is not None:
            raise TypeError("uTorrent API is already connected. "
                            "Disconnect first.")

        # Unsafe in case of a URL directing to an IP
        domain = _parse.urlparse(self.base_url).netloc
        unsafe = bool(_IP_NETLOC_RE.match(domain))

        if unsafe:
            import warnings
            warnings.warn("Using an IP instead of a hostname or domain to "
                          "contact uTorrent programmatically is unsafe. "
                          "Please consider contacting a domain-based URL.")

        self._client = _aiohttp.ClientSession(
            loop=self._loop,
            cookie_jar=_aiohttp.CookieJar(unsafe=unsafe),
            auth=_aiohttp.BasicAuth(self.username, self.password))

        await self._renew_token()

    async def _renew_token(self):
        """Fetches a new authentication token"""
        url = _parse.urljoin(self.base_url, _TOKEN_URL)

        async with self._client.get(url) as resp:
            if resp.status != 200:
                raise ConnectionError(f"Server responded with status "
                                      f"{resp.status} ({resp.reason}).")
            xml = await resp.text()

        element = _ElementTree.fromstring(xml).find("div[@id='token']")
        token = element.text
        self.token = token

    async def _simple_action(self, name, torrent_hashes):
        return await self._action(name, [("hash", h) for h in torrent_hashes])

    async def _action(self, name, args):
        args.insert(0, ("action", name))
        return await self._get_request(args)

    @_renew_token_on_failure
    async def _get_request(self, params):
        # First argument must be the action while token can't be last according
        # to uTorrent's API.
        # Tuple in order to prevent argument mutation and duplication of
        # token (in case of token renewal).
        params = tuple((params[0], ("token", self.token), *params[1:]))
        _logger.debug("Sending a get request to \"%s\" with "
                      "the following params: %s", self.base_url, params)

        async with self._client.get(self.base_url,
                                    params=tuple(params)) as resp:
            if resp.status != 200:
                raise ConnectionError(f"Server responded with status "
                                      f"{resp.status} ({resp.reason}).")
            return await resp.text()

    async def start(self, *torrent_hashes):
        await self._simple_action("start", torrent_hashes)

    async def stop(self, *torrent_hashes):
        await self._simple_action("stop", torrent_hashes)

    async def pause(self, *torrent_hashes):
        await self._simple_action("pause", torrent_hashes)

    async def force_start(self, *torrent_hashes):
        await self._simple_action("forcestart", torrent_hashes)

    async def unpause(self, *torrent_hashes):
        await self._simple_action("unpause", torrent_hashes)

    async def recheck(self, *torrent_hashes):
        await self._simple_action("recheck", torrent_hashes)

    async def remove(self, *torrent_hashes):
        await self._simple_action("remove", torrent_hashes)

    async def remove_data(self, *torrent_hashes):
        await self._simple_action("removedata", torrent_hashes)

    async def set_priority(self, torrent_hash, priority, *file_indices):
        args = [("hash", torrent_hash), ("s", priority)]
        args.extend(("f", index) for index in file_indices)
        await self._action("setprio", args)

    async def add_url(self, torrent_url):
        await self._simple_action("add-URL", [("s", torrent_url)])

    async def disconnect(self):
        self._connected = False
        await self._client.close()
        self._client = None

    async def __aenter__(self):
        if self._client is None:
            await self.connect()

        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.disconnect()

    # async def add_file(self, *torrent_hashes):
    #     await self._simple_action("remove", torrent_hashes)
