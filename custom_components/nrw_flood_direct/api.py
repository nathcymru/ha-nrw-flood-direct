"""Bounded NRW transport and durable request accounting.

Adapted from nathcymru/ha-metoffice-direct (MIT, copyright 2026 nathcymru).
Credentials remain only in runtime memory and one fixed-origin request header.
"""

import asyncio
import copy
import json
import math
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.json import save_json

from .const import DAILY_LIMIT, ENDPOINT, MAX_BYTES, MIN_REQUEST_SECONDS


class ProviderError(Exception):
    """A deliberately sanitized error safe to expose in status/logs."""


class LimitError(ProviderError):
    """A durable budget or retry guard prevented a request."""


def utcnow():
    return datetime.now(UTC)


def retry_seconds(value, now):
    if not value:
        return None
    try:
        seconds = float(value)
    except TypeError, ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                return None
            seconds = (deadline.astimezone(UTC) - now).total_seconds()
        except TypeError, ValueError, OverflowError:
            return None
    return max(1, min(seconds, 172800)) if math.isfinite(seconds) and seconds >= 0 else None


def _finite(value):
    return type(value) in (float, int) and math.isfinite(value) and value >= 0


class StateStore:
    """Never reset a corrupt existing ledger or send before a durable reservation."""

    def __init__(self, hass):
        self.hass = hass
        self.path = Path(hass.config.path(".storage", "nrw_flood_direct_runtime.json"))
        self.lock = asyncio.Lock()
        self.data = {
            "version": 1,
            "day": utcnow().date().isoformat(),
            "count": 0,
            "last_request": 0,
            "blocked_until": 0,
            "failures": 0,
            "cache": None,
        }

    async def load(self):
        missing = object()

        def read():
            if self.path.is_symlink():
                raise ProviderError("Persistent request ledger is invalid")
            if not self.path.exists():
                return missing
            if self.path.is_symlink() or self.path.stat().st_size > MAX_BYTES * 3:
                raise ProviderError("Persistent request ledger is invalid")
            try:
                return json.loads(self.path.read_bytes())
            except OSError, ValueError:
                raise ProviderError("Persistent request ledger cannot be read") from None

        value = await self.hass.async_add_executor_job(read)
        if value is missing:
            return
        try:
            valid = (
                isinstance(value, dict)
                and value.get("version") == 1
                and isinstance(value.get("day"), str)
                and date.fromisoformat(value["day"]).isoformat() == value["day"]
                and type(value.get("count")) is int
                and value["count"] >= 0
                and type(value.get("failures")) is int
                and 0 <= value["failures"] <= 10
                and _finite(value.get("last_request"))
                and _finite(value.get("blocked_until"))
                and (value.get("cache") is None or isinstance(value.get("cache"), dict))
            )
        except TypeError, ValueError:
            valid = False
        if not valid:
            raise ProviderError("Persistent request ledger is invalid; limits were not reset")
        self.data = value
        geometry_budget = value.get("geometry_budget")
        if geometry_budget is not None:
            try:
                valid = (
                    isinstance(geometry_budget, dict)
                    and date.fromisoformat(geometry_budget["day"]).isoformat()
                    == geometry_budget["day"]
                    and type(geometry_budget["count"]) is int
                    and geometry_budget["count"] >= 0
                    and _finite(geometry_budget["retry_after"])
                )
            except KeyError, TypeError, ValueError:
                valid = False
            if not valid:
                raise ProviderError("Persistent geometry request ledger is invalid")

    def _write(self, value):
        self.path.parent.mkdir(exist_ok=True, parents=True)
        if self.path.is_symlink():
            raise ProviderError("Persistent request ledger path is invalid")
        save_json(str(self.path), value, private=True, atomic_writes=True)

    async def _save(self):
        try:
            await self.hass.async_add_executor_job(self._write, copy.deepcopy(self.data))
        except OSError, ValueError, HomeAssistantError:
            raise ProviderError(
                "Persistent request ledger cannot be saved; request blocked"
            ) from None

    async def reserve(self, now):
        async with self.lock:
            day = now.astimezone(UTC).date().isoformat()
            if self.data["day"] > day:
                raise LimitError("Clock precedes persistent request ledger date")
            if self.data["day"] < day:
                self.data.update(day=day, count=0)
            if now.timestamp() < self.data["blocked_until"]:
                raise LimitError("NRW provider retry delay is active")
            if now.timestamp() < self.data["last_request"] + MIN_REQUEST_SECONDS:
                raise LimitError("NRW minimum request spacing is active")
            if self.data["count"] >= DAILY_LIMIT:
                raise LimitError("NRW local daily request limit reached")
            self.data.update(count=self.data["count"] + 1, last_request=now.timestamp())
            await self._save()

    async def block(self, deadline, *, failure=False):
        async with self.lock:
            self.data["blocked_until"] = max(self.data["blocked_until"], deadline.timestamp())
            if failure:
                self.data["failures"] = min(10, self.data["failures"] + 1)
            await self._save()

    async def save_cache(self, value):
        async with self.lock:
            self.data["cache"] = copy.deepcopy(value)
            self.data["failures"] = 0
            await self._save()

    async def reserve_geometry(self, now):
        async with self.lock:
            day = now.date().isoformat()
            budget = self.data.setdefault(
                "geometry_budget", {"day": day, "count": 0, "retry_after": 0}
            )
            if budget["day"] > day:
                raise LimitError("Clock precedes the geometry request ledger")
            if budget["day"] < day:
                budget.update(day=day, count=0)
            if now.timestamp() < budget["retry_after"] or budget["count"] >= 20:
                raise LimitError("Official area lookup request limit is active")
            budget["count"] += 1
            await self._save()

    async def save_geometry(self, value, *, failure=False, retry_after=None):
        async with self.lock:
            if not failure:
                self.data["geometry"] = copy.deepcopy(value)
            budget = self.data.setdefault(
                "geometry_budget",
                {"day": utcnow().date().isoformat(), "count": 0, "retry_after": 0},
            )
            budget["retry_after"] = (
                max(
                    budget["retry_after"],
                    (utcnow() + timedelta(minutes=15)).timestamp(),
                    retry_after.timestamp() if retry_after is not None else 0,
                )
                if failure
                else 0
            )
            self.data["geometry_failed"] = failure
            await self._save()


class Client:
    def __init__(self, session, store, key):
        self.session, self.store, self.key = session, store, key
        self.lock = asyncio.Lock()
        self.auth_failed = False

    @staticmethod
    def safe_url(url):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "api.naturalresources.wales"
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
            or parsed.path != "/floodwarnings/v3/all"
            or parsed.query
            or parsed.fragment
        ):
            raise ProviderError("Unexpected NRW resource URL")
        return url

    async def get(self):
        async with self.lock:
            if self.auth_failed:
                raise ProviderError("NRW authorization rejected; update YAML secret and reload")
            if not self.key:
                raise ProviderError("NRW API secret is not configured")
            await self.store.reserve(utcnow())
            try:
                # Covers the complete body-read phase as well as connection setup.
                async with asyncio.timeout(35):
                    async with self.session.get(
                        self.safe_url(ENDPOINT),
                        headers={
                            "Ocp-Apim-Subscription-Key": self.key,
                            "Accept": "application/geo+json, application/json",
                        },
                        timeout=aiohttp.ClientTimeout(total=30, connect=10),
                        allow_redirects=False,
                    ) as response:
                        if response.status in (401, 403):
                            self.auth_failed = True
                            raise ProviderError(
                                "NRW authorization rejected; update YAML secret and reload"
                            )
                        if response.status == 429:
                            now = utcnow()
                            seconds = retry_seconds(response.headers.get("Retry-After"), now)
                            deadline = (
                                now + timedelta(seconds=seconds)
                                if seconds
                                else (now + timedelta(days=1)).replace(
                                    hour=0, minute=0, second=5, microsecond=0
                                )
                            )
                            await self.store.block(deadline)
                            raise LimitError("NRW provider quota reached")
                        if response.status != 200:
                            if response.status >= 500:
                                await self._backoff()
                                if response.status == 503:
                                    now = utcnow()
                                    delay = retry_seconds(response.headers.get("Retry-After"), now)
                                    if delay is not None:
                                        await self.store.block(now + timedelta(seconds=delay))
                            raise ProviderError(f"NRW HTTP {response.status}")
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            body.extend(chunk)
                            if len(body) > MAX_BYTES:
                                raise ProviderError("NRW response exceeds its size bound")
                        try:
                            value = json.loads(body)
                        except UnicodeDecodeError, ValueError:
                            raise ProviderError("NRW response is malformed") from None
                        if self.key.encode() in body:
                            raise ProviderError(
                                "NRW response contains unexpected authentication data"
                            )
                        headers = {
                            name: response.headers[name]
                            for name in ("Date", "Age")
                            if name in response.headers
                        }
                        return value, headers
            except aiohttp.ClientError, TimeoutError:
                await self._backoff()
                raise ProviderError("NRW connection failed") from None

    async def _backoff(self):
        delay = min(3600, 60 * 2 ** min(self.store.data["failures"], 6))
        await self.store.block(utcnow() + timedelta(seconds=delay), failure=True)
