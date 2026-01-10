"""Config flow for Local LLM Conversation integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.core import callback
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SSL
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import llm
from homeassistant.components import conversation

from . import YuryLLMAPI
from .const import DOMAIN, YURY_LLM_API_ID, CONF_CHAT_MODEL
from .entity import LocalLLMConfigEntry, LocalLLMClient

from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST, default=""): str,
        vol.Optional(CONF_PORT, default="11434"): str,
        vol.Required(CONF_SSL, default=False): bool,
    }
)


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Local LLM Conversation."""

    VERSION = 3

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Handle the initial step."""
        # make sure the API is registered
        if user_input is None:
            if not any(
                [x.id == YURY_LLM_API_ID for x in llm.async_get_apis(self.hass)]
            ):
                llm.async_register_api(self.hass, YuryLLMAPI(self.hass))
        else:
            return await self.async_step_finish(user_input)
        return self.async_show_form(step_id="user", data_schema=DATA_SCHEMA)

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        if user_input is None:
            raise AbortFlow("user input is null")
        host = user_input[CONF_HOST]
        port = user_input[CONF_PORT]
        title = "Ollama at " + host + ":" + port

        return self.async_create_entry(
            title=title,
            description="A Large Language Model Chat Agent",
            data={CONF_HOST: host, CONF_PORT: port},
        )

    @classmethod
    def async_get_supported_subentry_types(
        cls, config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        """Return subentries supported by this integration."""
        return {
            conversation.DOMAIN: LocalLLMSubentryFlowHandler,
        }


def STEP_REMOTE_MODEL_SELECTION_DATA_SCHEMA(
    available_models: list[str], chat_model: str | None = None
):
    _LOGGER.debug(f"available models: {available_models}")
    return vol.Schema(
        {
            vol.Required(
                CONF_CHAT_MODEL,
                default=chat_model if chat_model else available_models[0],
            ): SelectSelector(
                SelectSelectorConfig(
                    options=available_models,
                    custom_value=True,
                    multiple=False,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


class LocalLLMSubentryFlowHandler(config_entries.ConfigSubentryFlow):
    """Flow for managing Local LLM subentries."""

    def __init__(self) -> None:
        """Initialize the subentry flow."""
        super().__init__()

        # state for subentry flow
        self.model_config: dict[str, Any] = {}
        self.download_task = None
        self.download_error = None

    @property
    def _is_new(self) -> bool:
        """Return if this is a new subentry."""
        return self.source == "user"

    @property
    def _client(self) -> LocalLLMClient:
        """Return the Ollama client."""
        entry: LocalLLMConfigEntry = self._get_entry()
        return entry.runtime_data

    async def async_step_pick_model(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        if user_input is not None:
            return await self.async_step_finish(user_input)
        schema = vol.Schema({})
        errors = {}
        description_placeholders = {}
        entry = self._get_entry()
        schema = STEP_REMOTE_MODEL_SELECTION_DATA_SCHEMA(
            await entry.runtime_data.async_get_available_models()
        )

        return self.async_show_form(
            step_id="pick_model",
            data_schema=schema,
            errors=errors,
            description_placeholders=description_placeholders,
            last_step=True,
        )

    async def async_step_finish(
        self, user_input: dict[str, Any]
    ) -> config_entries.SubentryFlowResult:
        """Step after model downloading has succeeded."""

        # Model download completed, create/update the entry with stored config
        if self._is_new:
            return self.async_create_entry(
                title=user_input[CONF_CHAT_MODEL],
                data=user_input,
            )
        else:
            raise Exception("update not implemented")

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Handle model selection and configuration step."""

        # Ensure the parent entry is loaded before allowing subentry edits
        if self._get_entry().state != config_entries.ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")

        if not self.model_config:
            self.model_config = {}

        return await self.async_step_pick_model(user_input)

    async_step_init = async_step_user
