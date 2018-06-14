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
from typing import Union as _Union, List as _List, Dict as _Dict
import re as _re
from enum import (Enum as _Enum, auto as _auto, Flag as _Flag,
                  IntEnum as _IntEnum)


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
    LOW = 1
    NORMAL = 2
    HIGH = 3


class TorrentJobFlags(_IntEnum):
    NOT_ALLOWED = -1
    DISABLED = 0
    ENABLED = 1


class Status(_Flag):
    STARTED = 1
    CHECKING = 2
    START_AFTER_CHECK = 4
    CHECKED = 8
    ERROR = 16
    PAUSED = 32
    QUEUED = 64
    LOADED = 128


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


class _JSONConvertable:
    """Mixin class that allows creating a dataclass from a JSON object"""
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


@_dataclass(frozen=True)
class File(_JSONConvertable):
    name: _Path  # Path relative to torrent folder
    size: int
    downloaded: int
    priority: Priority
    _unknown_1: int = _field(repr=False)
    pieces: int
    _unknown_2: bool = _field(repr=False)
    _unknown_3: int = _field(repr=False)
    _unknown_4: int = _field(repr=False)
    _unknown_5: int = _field(repr=False)
    _unknown_6: int = _field(repr=False)
    _unknown_7: int = _field(repr=False)

    _JSON_CONVERSION_TABLE = {
        "priority": Priority,
        "name": _Path
    }


@_dataclass(frozen=True)
class Torrent(_JSONConvertable):
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


@_dataclass(frozen=True)
class TorrentJob(_JSONConvertable):
    hash: str
    trackers: _List[str]
    ulrate: int  # upload limit (bytes per second)
    dlrate: int  # download limit (bytes per second)
    superseed: TorrentJobFlags  # initial seeding
    dht: TorrentJobFlags  # use dht
    pex: TorrentJobFlags  # use pex
    seed_override: TorrentJobFlags  # override queueing
    seed_ratio: int  # per mil
    seed_time: int  # minimum time to seed in seconds, 0 means no minimum
    ulslots: int  # upload slots

    _JSON_CONVERSION_TABLE = {
        "superseed": TorrentJobFlags,
        "dht": TorrentJobFlags,
        "pex": TorrentJobFlags,
        "seed_override": TorrentJobFlags,
        "trackers": str.splitlines
    }


class uTorrentAPI:
    """Exposes uTorrent's remote web-based API"""
    @_dataclass
    class _CacheObj:
        label: _Dict[str, int] = _field(default_factory=dict)
        torrents: _Dict[str, Torrent] = _field(default_factory=dict)

    def __init__(self, base_url: str, username: str,
                 password: str, *, loop=None):
        """Initializes the API"""

        self.base_url: str = base_url
        self.username: str = username
        self.password: str = password
        self._token: _Union[str, None] = None
        self._loop = loop or _asyncio.get_event_loop()
        self._client = None
        self._connected = False
        self._cache_id = None
        self._cache = self._CacheObj()

    def _reset_cache(self):
        self._cache_id = None
        self._cache.label.clear()
        self._cache.torrents.clear()

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
        assert element is not None
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
        args = [("hash", torrent_hash), ("s", priority.value)]
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
        cached_torrents = cache.torrents

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

        cache.label.update(result["label"])
        self._cache_id = result["torrentc"]

        # Make sure we create a weak copy of the dictionaries to prevent
        # cache modification.
        return (_types.MappingProxyType(cache.label),
                _types.MappingProxyType(cache.torrents))

    @_if_connected
    async def get_files(self, *torrent_hashes):
        data = await self._simple_action("getfiles", torrent_hashes)
        json = _json.loads(data)
        iterator = iter(json["files"])
        pairwise = zip(iterator, iterator)  # Iterator of hash: file_list
        return {hash: list(map(File.from_json, files)) for hash, files
                in pairwise}

    @_if_connected
    async def get_props(self, *torrent_hashes):
        data = await self._simple_action("getprops", torrent_hashes)
        json = _json.loads(data)
        return list(map(TorrentJob.from_json, json["props"]))

    async def set_props(self, props):
        params = []
        for hash, job_props in props.items():
            params.append(("hash", hash))
            if "trackers" in job_props:
                job_props["trackers"] = "\r\n".join(job_props["trackers"])
            params.extend(job_props.items())
        await self._action("setprops", params)

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
