__author__ = "Bar Harel"
__version__ = "0.1.0a"
__license__ = "MIT"
__all__ = ["uTorrentAPI", "Priority", "Status", "File", "Torrent"]

import asyncio as _asyncio
from functools import update_wrapper as _update_wrapper
import urllib.parse as _parse
import types as _types
import logging as _logging
import json as _json
from datetime import datetime as _datetime
from pathlib import Path as _Path
from dataclasses import (dataclass as _dataclass,
                         fields as _fields, field as _field)
from typing import Union as _Union
import re as _re
from enum import Enum as _Enum, auto as _auto, Flag as _Flag


from functools import wraps as _wraps
from xml.etree import ElementTree as _ElementTree

import aiohttp as _aiohttp

_TOKEN_URL = "token.html"
_logger = _logging.getLogger(__name__)
_logger.addHandler(_logging.NullHandler())
_IP_NETLOC_RE = _re.compile(
    r"(?:(?:[0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])\.)"
    r"{3}(?:[0-9]|[1-9][0-9]|1[0-9]{2}|2[0-4][0-9]|25[0-5])(?:$|\:)")


class Priority(_Enum):
    DONT_DOWNLOAD = 0
    LOW = _auto()
    NORMAL = _auto()
    HIGH = _auto()


class Status(_Flag):
    STARTED = 1
    CHECKING = 2
    START_AFTER_CHECK = 4
    CHECKED = 8
    ERROR = 16
    PAUSED = 32
    QUEUED = 64
    LOADED = 128


@_dataclass(frozen=True)
class File:
    name: str
    path: _Path
    size: int
    downloaded: int
    priority: Priority


class _cachedproperty:
    def __init__(self, func):
        self.func = func
        _update_wrapper(self, func)

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, inst, owner):
        result = self.func.__get__(inst, owner)()
        setattr(owner if inst is None else inst, self.name, result)
        return result


@_dataclass(frozen=True)
class Torrent:
    hash: str
    status: Status
    name: str
    size: int  # bytes
    precent_progress: int  # per mil
    downloaded: int  # bytes
    uploaded: int  # bytes
    ratio: int  # per mil
    upload_speed: int  # bytes per second
    download_speed: int  # bytes per second
    eta: int  # seconds
    label: str
    peers_connected: int
    peers_in_swarm: int
    seeds_connected: int
    seeds_in_swarm: int
    availability: int  # in 1/65536ths
    torrent_queue_order: int
    remaining: int  # bytes
    _unknown_1: str = _field(repr=False)
    _unknown_2: str = _field(repr=False)
    status_message: str
    _unknown_3: str = _field(repr=False)
    added_on: _datetime
    completed_on: _Union[None, _datetime]
    _unknown_4: str = _field(repr=False)
    path: _Path  # Location of torrent folder
    _unknown_5: int = _field(repr=False)
    _unknown_6: str = _field(repr=False)

    _JSON_CONVERSION_TABLE = {
        "status": Status,
        "added_on": _datetime.fromtimestamp,
        "completed_on": lambda x: _datetime.fromtimestamp(x) if x else None,
        "path": _Path
    }

    @_cachedproperty
    @classmethod
    def _FIELD_INDEX_TABLE(cls):
        return {field.name: index for index, field
                in enumerate(_fields(cls))}

    @classmethod
    def from_json(cls, json):
        attributes = list(json)
        indicies_table = cls._FIELD_INDEX_TABLE
        for key, func in cls._JSON_CONVERSION_TABLE.items():
            index = indicies_table[key]
            attributes[index] = func(attributes[index])
        return cls(*attributes)


class uTorrentAPI:
    """Exposes uTorrent's remote web-based API"""

    def __init__(self, base_url, username, password, *, loop=None):
        """Initializes the API"""

        self.base_url = base_url
        self.username = username
        self.password = password
        self._token = None
        self._loop = loop or _asyncio.get_event_loop()
        self._client = None
        self._connected = False
        self._cache_id = None
        self._cache = {"label": {}, "torrents": {}}

    def _reset_cache(self):
        self._cache_id = None
        self._cache["label"].clear()
        self._cache["torrents"].clear()

    def _renew_token_on_failure(func):
        @_wraps(func)
        async def wrapped(self, *args, **kwargs):
            try:
                return await func(self, *args, **kwargs)
            except ConnectionError:
                await self._renew_token()
                return await func(self, *args, **kwargs)
        return wrapped

    def _if_connected(func):
        @_wraps(func)
        async def wrapped(self, *args, **kwargs):
            if not self._connected:
                raise TypeError(f"Cannot use {func.__name__} on "
                                f"a closed connection.")
            return await func(self, *args, **kwargs)
        return wrapped

    async def connect(self):
        self._reset_cache()

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

        self._connected = True
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
                                    params=params) as resp:
            if resp.status != 200:
                raise ConnectionError(f"Server responded with status "
                                      f"{resp.status} ({resp.reason}).")
            return await resp.text()

    @_if_connected
    async def start(self, *torrent_hashes):
        await self._simple_action("start", torrent_hashes)

    @_if_connected
    async def stop(self, *torrent_hashes):
        await self._simple_action("stop", torrent_hashes)

    @_if_connected
    async def pause(self, *torrent_hashes):
        await self._simple_action("pause", torrent_hashes)

    @_if_connected
    async def force_start(self, *torrent_hashes):
        await self._simple_action("forcestart", torrent_hashes)

    @_if_connected
    async def unpause(self, *torrent_hashes):
        await self._simple_action("unpause", torrent_hashes)

    @_if_connected
    async def recheck(self, *torrent_hashes):
        await self._simple_action("recheck", torrent_hashes)

    @_if_connected
    async def remove(self, *torrent_hashes):
        await self._simple_action("remove", torrent_hashes)

    @_if_connected
    async def remove_data(self, *torrent_hashes):
        await self._simple_action("removedata", torrent_hashes)

    @_if_connected
    async def set_priority(self, torrent_hash, priority, *file_indices):
        args = [("hash", torrent_hash), ("s", priority)]
        args.extend(("f", index) for index in file_indices)
        await self._action("setprio", args)

    @_if_connected
    async def add_url(self, torrent_url):
        await self._simple_action("add-URL", [("s", torrent_url)])

    @_if_connected
    async def list(self):
        params = [("list", "1")]
        cache_id = self._cache_id
        if cache_id is not None:
            params.append(("cid", cache_id))
        result = await self._get_request(params)
        result = _json.loads(result)

        cache = self._cache
        cached_torrents = cache["torrents"]

        try:
            # Check if it's a cached result (contains the "torrentm" key)
            hashes_to_delete = result["torrentm"]

        except KeyError:
            assert "torrents" in result
            # This is not a cached result
            # Reset cache if it exists.
            if cache_id is not None: self._reset_cache()

            # All torrents will come from the "torrents" part in the json
            torrent_update_key = "torrents"

        else:
            # Cached result
            # Remove all hashes for deletion
            for torrent_hash in hashes_to_delete:
                del cached_torrents[torrent_hash]

            # All torrents will come from the "torrentp" part in the json
            torrent_update_key = "torrentp"

        # Add torrents from the appropriate part
        torrents = map(Torrent.from_json, result[torrent_update_key])
        cached_torrents.update((t.hash, t) for t in torrents)

        cache["label"].update(result["label"])
        self._cache_id = result["torrentc"]

        # Make sure we create a weak copy of the dictionaries to prevent
        # cache modification.
        return (_types.MappingProxyType(cache["label"]),
                _types.MappingProxyType(cache["torrents"]))

    @_if_connected
    async def get_files(self, *torrent_hashes):
        pass

    async def disconnect(self):
        self._connected = False
        client = self._client
        self._client = None
        await client.close()

    async def __aenter__(self):
        if self._client is None:
            await self.connect()

        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.disconnect()

    # async def add_file(self, *torrent_hashes):
    #     await self._simple_action("remove", torrent_hashes)
