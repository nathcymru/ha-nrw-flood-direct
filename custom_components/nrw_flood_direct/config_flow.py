"""Metadata-only entry: account keys are never copied into config-entry storage."""

from homeassistant import config_entries

from .const import DOMAIN, NAME


class NRWFloodFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        return self.async_abort(reason="yaml_required")

    async def async_step_import(self, import_data):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=NAME, data={})
