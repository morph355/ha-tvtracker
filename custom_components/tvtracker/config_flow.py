"""Config flow: TMDB key + region, then rooms in the options."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_REGION, CONF_ROOMS, CONF_TMDB_KEY, DEFAULT_REGION, DEFAULT_ROOMS, DOMAIN
from .tmdb import TMDB, TMDBAuthError, TMDBError


class TVTrackerConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            tmdb = TMDB(
                async_get_clientsession(self.hass),
                user_input[CONF_TMDB_KEY],
                user_input[CONF_REGION].upper(),
            )
            try:
                await tmdb.validate()
            except TMDBAuthError:
                errors["base"] = "invalid_auth"
            except TMDBError:
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title="TV Tracker",
                    data={
                        CONF_TMDB_KEY: user_input[CONF_TMDB_KEY].strip(),
                        CONF_REGION: user_input[CONF_REGION].upper(),
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TMDB_KEY): str,
                    vol.Required(CONF_REGION, default=DEFAULT_REGION): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return TVTrackerOptionsFlow()


class TVTrackerOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(CONF_ROOMS) or DEFAULT_ROOMS
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ROOMS, default=current): selector.ObjectSelector(),
                }
            ),
        )
