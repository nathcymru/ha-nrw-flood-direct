"""Explicit warning coverage, source freshness and request diagnostics."""

from datetime import timedelta

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import utcnow
from .const import ATTRIBUTION, DAILY_LIMIT, DOMAIN, NAME, POLL_SECONDS, STALE_SECONDS
from .model import DataError, timestamp


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        [
            WarningsSensor(coordinator),
            *[
                CountSensor(coordinator, key, name, level)
                for key, name, level in (
                    ("active_count", "Active count", None),
                    ("severe_flood_warning_count", "Severe flood warning count", 1),
                    ("flood_warning_count", "Flood warning count", 2),
                    ("flood_alert_count", "Flood alert count", 3),
                    ("cleared_count", "Cleared count", 4),
                )
            ],
            StatusSensor(coordinator),
            TimeSensor(coordinator),
            BudgetSensor(coordinator),
            CoverageSensor(coordinator),
        ]
    )


class FloodEntity(CoordinatorEntity, SensorEntity):
    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = True

    def __init__(self, coordinator, key, name):
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{key}"
        self.entity_id = f"sensor.{DOMAIN}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, DOMAIN)},
            name=NAME,
            manufacturer="Natural Resources Wales",
            model="Live Flood Warnings & Alerts API v3",
            configuration_url="https://api-portal.naturalresources.wales/",
        )

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        self.async_on_remove(async_track_time_interval(self.hass, self._tick, timedelta(minutes=1)))

    @callback
    def _tick(self, now):
        self.async_write_ha_state()

    @property
    def available(self):
        return self.coordinator.fresh

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        return {
            "scope": self.coordinator.scope,
            "fetched_at": data.get("fetched_at"),
            "response_time": data.get("response_time"),
            "stale_after_seconds": STALE_SECONDS,
        }


class WarningsSensor(FloodEntity):
    _attr_icon = "mdi:waves-arrow-up"

    def __init__(self, coordinator):
        super().__init__(coordinator, "warnings", "Warnings")

    @property
    def native_value(self):
        summary = self.coordinator.warnings()
        return summary["level"] if summary is not None else None

    @property
    def extra_state_attributes(self):
        summary = self.coordinator.warnings()
        return {
            **super().extra_state_attributes,
            "warnings": summary["warnings"] if summary is not None else None,
            "cleared_warnings": summary["cleared_warnings"] if summary is not None else None,
            "coverage_note": (
                "Home location is outside the published NRW warning and alert areas; this does not mean no flood risk"
                if self.coordinator.scope.get("coverage_status") == "outside_known_areas"
                else "Matching NRW flood warning and alert areas; not all sources of flooding are covered"
            ),
        }


class CountSensor(FloodEntity):
    def __init__(self, coordinator, key, name, level):
        super().__init__(coordinator, key, name)
        self.level = level

    @property
    def native_value(self):
        summary = self.coordinator.warnings()
        if summary is None:
            return None
        rows = summary["cleared_warnings"] if self.level == 4 else summary["warnings"]
        return sum(self.level is None or row["severity_code"] == self.level for row in rows)


class StatusSensor(FloodEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator, "api_status", "API status")

    @property
    def available(self):
        return True

    @property
    def native_value(self):
        return "available" if self.coordinator.feed_fresh else "unavailable"

    @property
    def extra_state_attributes(self):
        return {
            **super().extra_state_attributes,
            "last_error": self.coordinator.error_code,
            "refresh_interval_seconds": POLL_SECONDS,
        }


class TimeSensor(FloodEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator, "last_successful_update", "Last successful update")

    @property
    def available(self):
        return self.native_value is not None

    @property
    def native_value(self):
        data = self.coordinator.data or self.coordinator.client.store.data.get("cache") or {}
        try:
            return timestamp(data.get("fetched_at"))
        except DataError:
            return None


class BudgetSensor(FloodEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator, "requests_today", "Requests today")

    @property
    def available(self):
        return True

    @property
    def native_value(self):
        ledger = self.coordinator.client.store.data
        return ledger["count"] if ledger["day"] == utcnow().date().isoformat() else 0

    @property
    def extra_state_attributes(self):
        ledger = self.coordinator.client.store.data
        return {
            "local_daily_ceiling": DAILY_LIMIT,
            "count_scope": "This installation, UTC day",
            "provider_backoff_active": ledger["blocked_until"] > utcnow().timestamp(),
        }


class CoverageSensor(FloodEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator):
        super().__init__(coordinator, "coverage_status", "Coverage status")

    @property
    def available(self):
        return True

    @property
    def native_value(self):
        return self.coordinator.scope.get("coverage_status", "explicit_selection")
