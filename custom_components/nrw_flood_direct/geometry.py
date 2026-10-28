"""Official area intersections, never a distance or centroid approximation.

Polygon membership is adapted from ha-metoffice-direct (MIT, 2026 nathcymru).
DataMapWales advertises this WFS 2.0 endpoint and both NRW polygon layers.
"""

import asyncio
import hashlib
import json
import math
from datetime import timedelta

import aiohttp

from .api import ProviderError, retry_seconds, utcnow
from .const import MAX_BYTES
from .model import AREA_CODE, DataError, text, timestamp

WFS_URL = "https://datamap.gov.wales/geoserver/wfs"
LAYERS = ("NRW_FLOOD_WARNING", "NRW_FLOOD_WATCH_AREAS")
CRS84 = "urn:ogc:def:crs:OGC:1.3:CRS84"
REFRESH_SECONDS = 86400


def point(latitude, longitude):
    if any(type(v) not in (int, float) or not math.isfinite(v) for v in (latitude, longitude)):
        raise DataError("Home coordinates are unavailable")
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        raise DataError("Home coordinates are invalid")
    return longitude, latitude


def location_hash(latitude, longitude):
    return hashlib.sha256(json.dumps(point(latitude, longitude)).encode()).hexdigest()


def query_parameters(layer, latitude, longitude):
    if layer not in LAYERS:
        raise DataError("Unverified flood area layer")
    lon, lat = point(latitude, longitude)
    xml = (
        '<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0" '
        'xmlns:gml="http://www.opengis.net/gml/3.2"><fes:Intersects>'
        "<fes:ValueReference>geom</fes:ValueReference>"
        f'<gml:Point srsName="{CRS84}"><gml:pos>{lon} {lat}</gml:pos>'
        "</gml:Point></fes:Intersects></fes:Filter>"
    )
    return {
        "service": "WFS",
        "version": "2.0.0",
        "request": "GetFeature",
        "typeNames": "inspire-nrw:" + layer,
        "count": "100",
        "outputFormat": "application/json",
        "srsName": CRS84,
        "propertyName": "fwd_tacode,fwis_code,fwa_name,geom",
        "filter": xml,
    }


def _ring(point_value, ring):
    inside = False
    x, y = point_value
    for a, b in zip(ring, ring[1:]):
        cross = (y - a[1]) * (b[0] - a[0]) - (x - a[0]) * (b[1] - a[1])
        if (
            abs(cross) <= 1e-12
            and min(a[0], b[0]) <= x <= max(a[0], b[0])
            and min(a[1], b[1]) <= y <= max(a[1], b[1])
        ):
            return True, True
        if (a[1] > y) != (b[1] > y) and x < (b[0] - a[0]) * (y - a[1]) / (b[1] - a[1]) + a[0]:
            inside = not inside
    return inside, False


def contains(geometry, longitude, latitude):
    if not isinstance(geometry, dict) or geometry.get("type") not in ("Polygon", "MultiPolygon"):
        raise DataError("Official flood area is not a polygon")
    coordinates = geometry.get("coordinates")
    polygons = [coordinates] if geometry["type"] == "Polygon" else coordinates
    if not isinstance(polygons, list) or not polygons:
        raise DataError("Official flood area coordinates are missing")
    vertices = 0
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            raise DataError("Official flood area polygon is malformed")
        for ring in polygon:
            if not isinstance(ring, list) or len(ring) < 4:
                raise DataError("Official flood area ring is malformed")
            vertices += len(ring)
            if vertices > 250000:
                raise DataError("Official flood area geometry exceeds its bound")
            for position in ring:
                if not isinstance(position, list) or len(position) != 2:
                    raise DataError("Official flood area position is malformed")
                point(position[1], position[0])
            if ring[0] != ring[-1] or len(set(map(tuple, ring))) < 3:
                raise DataError("Official flood area ring is not closed")
    for polygon in polygons:
        inside, boundary = _ring((longitude, latitude), polygon[0])
        if boundary:
            return True
        if inside:
            holes = [_ring((longitude, latitude), ring) for ring in polygon[1:]]
            if any(boundary for _, boundary in holes):
                return True
            if not any(hole for hole, _ in holes):
                return True
    return False


def parse_areas(payload, latitude, longitude):
    if not isinstance(payload, dict) or payload.get("type") != "FeatureCollection":
        raise DataError("Official area lookup returned invalid data")
    features = payload.get("features")
    if not isinstance(features, list) or len(features) >= 100:
        raise DataError("Official area lookup exceeded its result bound")
    if any(payload.get(key) for key in ("error", "errors", "next", "nextLink", "links")):
        raise DataError("Official area lookup is incomplete")
    for key in ("numberMatched", "numberReturned", "totalFeatures"):
        if type(payload.get(key)) is not int or payload[key] != len(features):
            raise DataError("Official area lookup is incomplete")
    if features and payload.get("crs") != {
        "type": "name",
        "properties": {"name": "urn:ogc:def:crs:EPSG::4326"},
    }:
        raise DataError("Official area lookup has an unverified coordinate system")
    areas = {}
    for feature in features:
        if not isinstance(feature, dict) or feature.get("type") != "Feature":
            raise DataError("Official area lookup contains an invalid feature")
        props = feature.get("properties")
        if not isinstance(props, dict):
            raise DataError("Official area lookup properties are missing")
        code = props.get("fwd_tacode")
        if (
            not isinstance(code, str)
            or not AREA_CODE.fullmatch(code)
            or props.get("fwis_code") != code
        ):
            raise DataError("Official area identifiers are ambiguous")
        if not contains(feature.get("geometry"), longitude, latitude):
            raise DataError("Official area lookup did not intersect the home")
        name = text(props.get("fwa_name"), "official area name", 1000)
        if code in areas and areas[code] != name:
            raise DataError("Official area lookup has conflicting identifiers")
        areas[code] = name
    return areas


class HomeAreas:
    def __init__(self, hass, session, store):
        self.hass, self.session, self.store = hass, session, store

    def _cached(self):
        cached = self.store.data.get("geometry")
        if not isinstance(cached, dict):
            return None
        try:
            if cached["location_hash"] != location_hash(
                self.hass.config.latitude, self.hass.config.longitude
            ):
                return None
            if (
                not timedelta(0)
                <= utcnow() - timestamp(cached["resolved_at"])
                < timedelta(seconds=REFRESH_SECONDS)
            ):
                return None
            if not isinstance(cached["areas"], dict) or any(
                not AREA_CODE.fullmatch(code) or not isinstance(name, str)
                for code, name in cached["areas"].items()
            ):
                return None
            return cached
        except DataError, KeyError, TypeError:
            return None

    @property
    def status(self):
        cached = self._cached()
        if self.store.data.get("geometry_failed"):
            return "stale" if cached else "unknown"
        if not cached:
            return "unknown"
        return "current" if cached["areas"] else "no_matching_areas"

    @property
    def fresh(self):
        # A complete successful lookup is fresh even when no polygons match.
        # Match count is a selection diagnostic, not a provider failure.
        return self.status in ("current", "no_matching_areas")

    @property
    def scope(self):
        cached = self._cached()
        return {
            "type": "area_codes",
            "label": "Home flood areas",
            "area_codes": sorted(cached["areas"]) if cached else [],
            "selection_method": "home_polygon",
            "coverage_status": self.status,
            "resolved_at": cached["resolved_at"] if cached else None,
        }

    async def resolve(self):
        if self._cached() and not self.store.data.get("geometry_failed"):
            return
        lat, lon = self.hass.config.latitude, self.hass.config.longitude
        point(lat, lon)
        areas = {}
        retry_after = None
        try:
            for layer in LAYERS:
                await self.store.reserve_geometry(utcnow())
                async with asyncio.timeout(35):
                    async with self.session.get(
                        WFS_URL,
                        params=query_parameters(layer, lat, lon),
                        headers={"Accept": "application/json"},
                        timeout=aiohttp.ClientTimeout(total=30, connect=10),
                        allow_redirects=False,
                    ) as response:
                        if response.status != 200:
                            if response.status in (429, 503):
                                delay = retry_seconds(response.headers.get("Retry-After"), utcnow())
                                if delay is not None:
                                    retry_after = utcnow() + timedelta(seconds=delay)
                            raise ProviderError(
                                f"Official flood area lookup HTTP {response.status}"
                            )
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            body.extend(chunk)
                            if len(body) > MAX_BYTES:
                                raise ProviderError("Official area lookup exceeds its size bound")
                        try:
                            parsed = parse_areas(json.loads(body), lat, lon)
                        except ValueError, UnicodeDecodeError:
                            raise ProviderError(
                                "Official area lookup returned invalid geometry"
                            ) from None
                        if any(
                            code in areas and areas[code] != name for code, name in parsed.items()
                        ):
                            raise ProviderError("Official area layers disagree on area identifiers")
                        areas.update(parsed)
            await self.store.save_geometry(
                {
                    "location_hash": location_hash(lat, lon),
                    "resolved_at": utcnow().isoformat(),
                    "areas": areas,
                }
            )
        except ProviderError, DataError, aiohttp.ClientError, TimeoutError:
            await self.store.save_geometry(None, failure=True, retry_after=retry_after)
            raise ProviderError("Home flood area coverage could not be verified") from None
