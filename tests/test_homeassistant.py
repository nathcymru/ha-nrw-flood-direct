from datetime import timedelta
from email.utils import format_datetime
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fixtures import feature, feed
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import reload as reload_helper

import custom_components.nrw_flood_direct as integration
from custom_components.nrw_flood_direct.api import Client, ProviderError, StateStore, utcnow
from custom_components.nrw_flood_direct.coordinator import FloodCoordinator
from custom_components.nrw_flood_direct.sensor import CountSensor, StatusSensor, WarningsSensor


def entry():
    return ConfigEntry(
        version=1,
        minor_version=1,
        domain="nrw_flood_direct",
        title="NRW Flood Direct",
        data={},
        options={},
        source="import",
        unique_id="nrw_flood_direct",
        discovery_keys=MappingProxyType({}),
        subentries_data=None,
    )


def coordinator(hass):
    return FloodCoordinator(
        hass,
        entry(),
        Client(Mock(), StateStore(hass), "SYNTHETIC_SECRET"),
        {"mode": "area_codes", "area_codes": ["TEST001"]},
    )


def current_feed():
    value = feature()
    for key in ("TIMERAISED", "SEVERITY_CHANGED", "RIM_CHANGED"):
        value["properties"][key] = int((utcnow() - timedelta(minutes=1)).timestamp() * 1000)
    return feed(value)


@pytest.mark.asyncio
async def test_failed_fetch_and_age_never_become_no_warning(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        coord = coordinator(hass)
        coord.client.get = AsyncMock(return_value=(feed(), {"Date": format_datetime(utcnow())}))
        await coord.async_refresh()
        warning, count, status = (
            WarningsSensor(coord),
            CountSensor(coord, "active_count", "Active count", None),
            StatusSensor(coord),
        )
        assert warning.entity_id == "sensor.nrw_flood_direct_warnings"
        assert warning.available and warning.native_value == "none"
        assert count.native_value == 0
        assert warning.extra_state_attributes["warnings"] == []
        coord.last_update_success = False
        assert not warning.available and warning.native_value is None
        assert warning.extra_state_attributes["warnings"] is None
        assert count.native_value is None
        assert status.available and status.native_value == "unavailable"
        coord.last_update_success = True
        coord.data["fetched_at"] = (utcnow() - timedelta(minutes=31)).isoformat()
        assert not warning.available
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_cache_survives_restart_without_polling_but_failed_attempt_invalidates(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        coord = coordinator(hass)
        now = utcnow()
        await coord.client.store.save_cache(
            {
                "payload": current_feed(),
                "fetched_at": now.isoformat(),
                "response_time": now.isoformat(),
            }
        )
        coord.client.get = AsyncMock()
        await coord.async_refresh()
        assert coord.fresh
        coord.client.get.assert_not_called()
        # A request after the good response failed. Reload cannot resurrect old success.
        coord.client.store.data["last_request"] = (now + timedelta(seconds=1)).timestamp()
        await coord.async_refresh()
        assert not coord.fresh
        assert WarningsSensor(coord).native_value is None
        coord.client.get.assert_not_called()
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_import_payload_and_actual_yaml_setup_never_persist_key(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    try:
        (tmp_path / "configuration.yaml").write_text(
            "nrw_flood_direct:\n  api_key: !secret nrw_key\n  mode: all_wales\n"
        )
        (tmp_path / "secrets.yaml").write_text("nrw_key: SYNTHETIC_SECRET\n")
        component = SimpleNamespace(
            domain="nrw_flood_direct",
            documentation="https://example.invalid/",
            async_get_component=AsyncMock(return_value=integration),
            platforms_exists=Mock(return_value=False),
        )
        monkeypatch.setattr(
            reload_helper, "async_get_integration", AsyncMock(return_value=component)
        )
        hass.config_entries = ConfigEntries(hass, {})
        hass.config_entries.flow.async_init = AsyncMock()
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        await integration.async_setup(hass, {"nrw_flood_direct": {"api_key": "SYNTHETIC_SECRET"}})
        await hass.async_block_till_done()
        assert hass.config_entries.flow.async_init.call_args.kwargs["data"] == {
            "name": "NRW Flood Direct"
        }
        monkeypatch.setattr(
            Client, "get", AsyncMock(return_value=(feed(), {"Date": format_datetime(utcnow())}))
        )
        monkeypatch.setattr(integration, "async_get_clientsession", Mock(return_value=Mock()))
        configured = entry()
        assert await integration.async_setup_entry(hass, configured)
        assert configured.data == {}
        assert configured.runtime_data.client.key == "SYNTHETIC_SECRET"
        assert configured.runtime_data.coordinator.scope["label"] == "All Wales"
        assert (
            "SYNTHETIC_SECRET"
            not in (tmp_path / ".storage" / "nrw_flood_direct_runtime.json").read_text()
        )
        hass.config_entries.async_forward_entry_setups.assert_awaited_once()
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_initial_api_failure_still_forwards_unavailable_entities(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    try:
        hass.config_entries = ConfigEntries(hass, {})
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        monkeypatch.setattr(
            integration,
            "async_integration_yaml_config",
            AsyncMock(
                return_value={
                    "nrw_flood_direct": {"api_key": "SYNTHETIC_SECRET", "mode": "all_wales"}
                }
            ),
        )
        monkeypatch.setattr(integration, "async_get_clientsession", Mock(return_value=Mock()))
        monkeypatch.setattr(
            Client, "get", AsyncMock(side_effect=ProviderError("NRW connection failed"))
        )
        configured = entry()
        assert await integration.async_setup_entry(hass, configured)
        assert not configured.runtime_data.coordinator.fresh
        hass.config_entries.async_forward_entry_setups.assert_awaited_once()
    finally:
        await hass.async_stop()
