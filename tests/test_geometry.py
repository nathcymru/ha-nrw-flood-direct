import json
from datetime import timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.core import HomeAssistant
from test_api import Response

from custom_components.nrw_flood_direct.api import ProviderError, StateStore, utcnow
from custom_components.nrw_flood_direct.geometry import (
    HomeAreas,
    contains,
    location_hash,
    parse_areas,
    query_parameters,
)
from custom_components.nrw_flood_direct.model import DataError


@pytest.mark.asyncio
@pytest.mark.parametrize("status,as_date", [(429, False), (503, True)])
async def test_geometry_provider_retry_after_survives_restart(
    tmp_path, monkeypatch, status, as_date
):
    from custom_components.nrw_flood_direct import api as api_module
    from custom_components.nrw_flood_direct import geometry as geo_module

    hass = HomeAssistant(str(tmp_path))
    hass.config.latitude, hass.config.longitude = 53, -3
    try:
        now = utcnow()
        header = format_datetime(now + timedelta(hours=2)) if as_date else "7200"
        session = SimpleNamespace(get=Mock(return_value=Response(status, {"Retry-After": header})))
        store = StateStore(hass)
        with pytest.raises(ProviderError):
            await HomeAreas(hass, session, store).resolve()
        restored = StateStore(hass)
        await restored.load()
        assert (
            restored.data["geometry_budget"]["retry_after"]
            >= (now + timedelta(hours=2) - timedelta(seconds=1)).timestamp()
        )
        monkeypatch.setattr(api_module, "utcnow", lambda: now + timedelta(minutes=30))
        monkeypatch.setattr(geo_module, "utcnow", lambda: now + timedelta(minutes=30))
        with pytest.raises(ProviderError):
            await HomeAreas(hass, session, restored).resolve()
        assert session.get.call_count == 1
        assert (
            restored.data["geometry_budget"]["retry_after"]
            >= (now + timedelta(hours=2) - timedelta(seconds=1)).timestamp()
        )
    finally:
        await hass.async_stop()


def polygon(hole=False):
    rings = [[[-4, 52], [-2, 52], [-2, 54], [-4, 54], [-4, 52]]]
    if hole:
        rings.append([[-3.5, 52.5], [-2.5, 52.5], [-2.5, 53.5], [-3.5, 53.5], [-3.5, 52.5]])
    return {"type": "Polygon", "coordinates": rings}


def areas(code="TEST001"):
    features = (
        []
        if code is None
        else [
            {
                "type": "Feature",
                "properties": {"fwd_tacode": code, "fwis_code": code, "fwa_name": "Synthetic area"},
                "geometry": polygon(),
            }
        ]
    )
    return {
        "type": "FeatureCollection",
        "features": features,
        "numberMatched": len(features),
        "numberReturned": len(features),
        "totalFeatures": len(features),
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:EPSG::4326"}},
    }


def test_membership_includes_boundaries_and_excludes_holes():
    assert contains(polygon(), -3, 53)
    assert contains(polygon(), -4, 53)
    assert not contains(polygon(), 0, 53)
    assert not contains(polygon(True), -3, 53)
    assert contains(polygon(True), -3.5, 53)
    assert contains({"type": "MultiPolygon", "coordinates": [polygon()["coordinates"]]}, -3, 53)


@pytest.mark.parametrize(
    "geometry",
    [
        None,
        {"type": "Point", "coordinates": [-3, 53]},
        {"type": "Polygon", "coordinates": []},
        {"type": "Polygon", "coordinates": [[[-4, 52], [-2, 52], [-2, 54], [-4, 54]]]},
    ],
)
def test_bad_geometries_are_unknown_not_outside(geometry):
    with pytest.raises(DataError):
        contains(geometry, -3, 53)


def test_counts_join_crs_and_local_intersection_are_verified():
    assert parse_areas(areas(), 53, -3) == {"TEST001": "Synthetic area"}
    assert parse_areas(areas(None), 53, -3) == {}
    mutations = [
        lambda x: x.update(numberMatched=2),
        lambda x: x.update(crs=None),
        lambda x: x["features"][0]["properties"].update(fwis_code="DIFFERENT"),
        lambda x: x.update(links=[{"rel": "next"}]),
    ]
    for change in mutations:
        data = areas()
        change(data)
        with pytest.raises(DataError):
            parse_areas(data, 53, -3)
    with pytest.raises(DataError):
        parse_areas(areas(), 53, 0)


def test_query_uses_polygon_intersection_and_lon_lat_crs84():
    query = query_parameters("NRW_FLOOD_WARNING", 53, -3)
    assert "<gml:pos>-3 53</gml:pos>" in query["filter"]
    assert "<fes:Intersects>" in query["filter"]
    assert query["count"] == "100"
    assert "distance" not in str(query)
    assert "CRS84" in query["srsName"]
    with pytest.raises(DataError):
        query_parameters("arbitrary", 53, -3)


@pytest.mark.asyncio
async def test_two_layers_cached_and_home_change_invalidates_without_leaking_coordinates(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    hass.config.latitude, hass.config.longitude = 53, -3
    try:
        session = SimpleNamespace(
            get=Mock(
                side_effect=[
                    Response(payload=json.dumps(areas()).encode()),
                    Response(payload=json.dumps(areas("TEST002")).encode()),
                ]
            )
        )
        store = StateStore(hass)
        resolver = HomeAreas(hass, session, store)
        await resolver.resolve()
        assert resolver.fresh
        assert resolver.scope["area_codes"] == ["TEST001", "TEST002"]
        assert resolver.scope["type"] == "area_codes"
        assert "latitude" not in json.dumps(store.data) and "longitude" not in json.dumps(
            store.data
        )
        assert store.data["geometry_budget"]["count"] == 2
        await resolver.resolve()
        assert session.get.call_count == 2
        assert all(
            "Ocp-Apim-Subscription-Key" not in call.kwargs["headers"]
            for call in session.get.call_args_list
        )
        hass.config.latitude = 54
        assert not resolver.fresh and resolver.scope["area_codes"] == []
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_complete_empty_area_selection_is_fresh(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    hass.config.latitude, hass.config.longitude = 53, -3
    try:
        session = SimpleNamespace(
            get=Mock(return_value=Response(payload=json.dumps(areas(None)).encode()))
        )
        resolver = HomeAreas(hass, session, StateStore(hass))
        await resolver.resolve()
        assert resolver.status == "no_matching_areas"
        assert resolver.scope["area_codes"] == []
        assert resolver.fresh
    finally:
        await hass.async_stop()


@pytest.mark.asyncio
async def test_failed_refresh_preserves_last_known_but_never_current_clear(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    hass.config.latitude, hass.config.longitude = 53, -3
    try:
        store = StateStore(hass)
        await store.save_geometry(
            {
                "location_hash": location_hash(53, -3),
                "resolved_at": utcnow().isoformat(),
                "areas": {"TEST001": "Synthetic"},
            }
        )
        resolver = HomeAreas(hass, SimpleNamespace(get=Mock(return_value=Response(500))), store)
        await store.save_geometry(None, failure=True)
        assert resolver.status == "stale" and not resolver.fresh
        assert resolver.scope["area_codes"] == ["TEST001"]
        with pytest.raises(ProviderError):
            await resolver.resolve()
        assert resolver.session.get.call_count == 0
        assert store.data["geometry"]["areas"] == {"TEST001": "Synthetic"}
        store.data["geometry"]["resolved_at"] = (utcnow() - timedelta(days=2)).isoformat()
        assert resolver.status == "unknown"
    finally:
        await hass.async_stop()
