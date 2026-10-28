"""One whole-feed poll shared by every flood-warning sensor."""

import logging
from datetime import timedelta

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ProviderError, utcnow
from .const import DOMAIN, POLL_SECONDS, STALE_SECONDS
from .geometry import HomeAreas
from .model import DataError, normalize, scope, select, source_time, timestamp

LOGGER = logging.getLogger(__name__)


class FloodCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, client, config):
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=POLL_SECONDS),
        )
        self.client = client
        self._scope = scope(config)
        self.home_areas = (
            HomeAreas(hass, client.session, client.store) if config["mode"] == "home" else None
        )
        self.error_code = None

    @property
    def scope(self):
        return self.home_areas.scope if self.home_areas else self._scope

    def _cached(self):
        value = self.client.store.data.get("cache")
        if not value:
            return None
        try:
            fetched = timestamp(value["fetched_at"])
            source = timestamp(value["response_time"])
            now = utcnow()
            # A failed request after a previous good response remains failed
            # across restarts. Neither a restart nor clock rollback clears it.
            if fetched.timestamp() < self.client.store.data["last_request"]:
                return None
            if not timedelta(0) <= now - fetched < timedelta(seconds=POLL_SECONDS):
                return None
            if not timedelta(0) <= now - source < timedelta(seconds=STALE_SECONDS):
                return None
            rows = normalize(value["payload"], now)
            return {
                "fetched_at": fetched.isoformat(),
                "response_time": source.isoformat(),
                "rows": rows,
            }
        except DataError, KeyError, TypeError:
            return None

    async def _async_update_data(self):
        if self.home_areas:
            try:
                await self.home_areas.resolve()
            except ProviderError as err:
                self.error_code = str(err)
                raise UpdateFailed(self.error_code) from None
        cached = self._cached()
        if cached is not None:
            self.error_code = None
            return cached
        now = utcnow()
        if now.timestamp() < self.client.store.data["last_request"] + POLL_SECONDS:
            self.error_code = "Waiting for the next permitted NRW refresh"
            raise UpdateFailed(self.error_code)
        try:
            payload, headers = await self.client.get()
            fetched = utcnow()
            response_time = source_time(headers, fetched)
            rows = normalize(payload, fetched)
            await self.client.store.save_cache(
                {
                    "payload": payload,
                    "fetched_at": fetched.isoformat(),
                    "response_time": response_time,
                }
            )
            self.error_code = None
            return {"fetched_at": fetched.isoformat(), "response_time": response_time, "rows": rows}
        except (ProviderError, DataError) as err:
            self.error_code = str(err)
            raise UpdateFailed(self.error_code) from None

    @property
    def feed_fresh(self):
        if not self.last_update_success or not self.data:
            return False
        try:
            now = utcnow()
            return all(
                timedelta(0) <= now - timestamp(self.data[key]) < timedelta(seconds=STALE_SECONDS)
                for key in ("fetched_at", "response_time")
            )
        except DataError, KeyError, TypeError:
            return False

    @property
    def fresh(self):
        return self.feed_fresh and (self.home_areas is None or self.home_areas.fresh)

    def warnings(self):
        return select(self.data["rows"], self.scope) if self.fresh else None
