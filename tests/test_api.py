import asyncio
import json
import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.nrw_flood_direct.api import (
    Client,
    LimitError,
    ProviderError,
    StateStore,
    retry_seconds,
    utcnow,
)
from custom_components.nrw_flood_direct.const import DAILY_LIMIT, MAX_BYTES


class Response:
    def __init__(self, status=200, headers=None, payload=b"{}"):
        self.status, self.headers, self.payload = status, headers or {}, payload
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def iter_chunked(self, size):
        yield self.payload


@pytest.mark.asyncio
async def test_atomic_spacing_ceiling_and_restart_persistence(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        store = StateStore(hass)
        now = utcnow()
        results = await asyncio.gather(
            *(store.reserve(now) for _ in range(3)), return_exceptions=True
        )
        assert results.count(None) == 1
        assert sum(isinstance(x, LimitError) for x in results) == 2
        restored = StateStore(hass)
        await restored.load()
        assert restored.data["count"] == 1
        assert os.stat(store.path).st_mode & 0o777 == 0o600
        restored.data["count"] = DAILY_LIMIT
        with pytest.raises(LimitError):
            await restored.reserve(now + timedelta(seconds=7))
        await restored.reserve(now + timedelta(days=1))
        assert restored.data["count"] == 1
        with pytest.raises(LimitError):
            await restored.reserve(now)
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["{", "{}", '{"version":1,"day":"invalid"}', "null"])
async def test_corrupt_existing_ledger_never_resets(content, tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        store = StateStore(hass)
        store.path.parent.mkdir()
        store.path.write_text(content)
        with pytest.raises(ProviderError):
            await store.load()
        assert store.path.read_text() == content
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_dangling_symlink_and_failed_durable_write_block_network(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    try:
        store = StateStore(hass)
        store.path.parent.mkdir()
        store.path.symlink_to(tmp_path / "missing")
        with pytest.raises(ProviderError):
            await store.load()
        store.path.unlink()
        session = SimpleNamespace(get=Mock())
        client = Client(session, store, "SYNTHETIC_SECRET")
        monkeypatch.setattr(
            store, "_write", Mock(side_effect=HomeAssistantError("disk unavailable"))
        )
        with pytest.raises(ProviderError):
            await client.get()
        session.get.assert_not_called()
        assert "SYNTHETIC_SECRET" not in json.dumps(store.data)
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_429_blocks_across_restart(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        session = SimpleNamespace(get=Mock(return_value=Response(429, {"Retry-After": "3600"})))
        client = Client(session, StateStore(hass), "SYNTHETIC_SECRET")
        with pytest.raises(LimitError):
            await client.get()
        store = StateStore(hass)
        await store.load()
        with pytest.raises(LimitError):
            await Client(session, store, "SYNTHETIC_SECRET").get()
        assert session.get.call_count == 1
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_authentication_rejection_halts_queued_calls_without_reflecting_key(status, tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        session = SimpleNamespace(
            get=Mock(return_value=Response(status, payload=b"SYNTHETIC_SECRET"))
        )
        client = Client(session, StateStore(hass), "SYNTHETIC_SECRET")
        results = await asyncio.gather(client.get(), client.get(), return_exceptions=True)
        assert all(
            isinstance(x, ProviderError) and "SYNTHETIC_SECRET" not in str(x) for x in results
        )
        assert session.get.call_count == 1
        assert session.get.call_args.kwargs["allow_redirects"] is False
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        Response(302),
        Response(payload=b"{"),
        Response(payload=b"x" * (MAX_BYTES + 1)),
        Response(payload=b'{"error":"SYNTHETIC_SECRET"}'),
    ],
)
async def test_redirect_malformed_oversize_and_key_echo_fail_closed(response, tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        session = SimpleNamespace(get=Mock(return_value=response))
        with pytest.raises(ProviderError) as caught:
            await Client(session, StateStore(hass), "SYNTHETIC_SECRET").get()
        assert "SYNTHETIC_SECRET" not in str(caught.value)
    finally:
        await hass.async_stop()


@pytest.mark.parametrize(
    "url",
    [
        "http://api.naturalresources.wales/floodwarnings/v3/all",
        "https://example.invalid/floodwarnings/v3/all",
        "https://user@api.naturalresources.wales/floodwarnings/v3/all",
        "https://api.naturalresources.wales/floodwarnings/v3/all?redirect=1",
    ],
)
def test_credentials_cannot_follow_unapproved_url(url):
    with pytest.raises(ProviderError):
        Client.safe_url(url)


def test_retry_after_bounds_and_http_date():
    assert retry_seconds("60", utcnow()) == 60
    assert retry_seconds("9999999", utcnow()) == 172800
    assert retry_seconds("nan", utcnow()) is None


@pytest.mark.asyncio
async def test_503_explicit_retry_after_survives_restart(tmp_path, monkeypatch):
    from custom_components.nrw_flood_direct import api as module

    hass = HomeAssistant(str(tmp_path))
    try:
        now = utcnow()
        session = SimpleNamespace(get=Mock(return_value=Response(503, {"Retry-After": "7200"})))
        store = StateStore(hass)
        with pytest.raises(ProviderError):
            await Client(session, store, "SYNTHETIC_SECRET").get()
        restored = StateStore(hass)
        await restored.load()
        assert restored.data["blocked_until"] >= (now + timedelta(hours=2)).timestamp()
        monkeypatch.setattr(module, "utcnow", lambda: now + timedelta(minutes=30))
        with pytest.raises(LimitError):
            await Client(session, restored, "SYNTHETIC_SECRET").get()
        assert session.get.call_count == 1
    finally:
        await hass.async_stop()
