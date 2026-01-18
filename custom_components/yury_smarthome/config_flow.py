"""Config flow for Local LLM Conversation integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SSL
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import llm
from homeassistant.components import conversation

from . import YuryLLMAPI
from .const import DOMAIN, YURY_LLM_API_ID, CONF_CHAT_MODEL, CONF_TTS_ENGINE, SUBENTRY_TYPE_TTS
from .entity import LocalLLMConfigEntry, LocalLLMClient

from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

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
        cls, _config_entry: config_entries.ConfigEntry
    ) -> dict[str, type[config_entries.ConfigSubentryFlow]]:
        """Return subentries supported by this integration."""
        return {
            conversation.DOMAIN: LLMSubentryFlowHandler,
            SUBENTRY_TYPE_TTS: TTSSubentryFlowHandler,
        }


def _build_llm_schema(available_models: list[str], current_model: str | None = None):
    """Build schema for LLM model selection."""
    default = current_model if current_model else (available_models[0] if available_models else "")
    return vol.Schema(
        {
            vol.Required(CONF_CHAT_MODEL, default=default): SelectSelector(
                SelectSelectorConfig(
                    options=available_models,
                    custom_value=True,
                    multiple=False,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def _build_tts_schema(available_tts_engines: list[str], current_tts: str | None = None):
    """Build schema for TTS engine selection."""
    default = current_tts if current_tts else (available_tts_engines[0] if available_tts_engines else "")
    return vol.Schema(
        {
            vol.Required(CONF_TTS_ENGINE, default=default): SelectSelector(
                SelectSelectorConfig(
                    options=available_tts_engines,
                    custom_value=True,
                    multiple=False,
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


class LLMSubentryFlowHandler(config_entries.ConfigSubentryFlow):
    """Flow for managing LLM conversation agent subentries."""

    def __init__(self) -> None:
        """Initialize the subentry flow."""
        super().__init__()
        self._data: dict[str, Any] = {}

    @property
    def _client(self) -> LocalLLMClient:
        """Return the Ollama client."""
        entry: LocalLLMConfigEntry = self._get_entry()
        return entry.runtime_data

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Handle new LLM subentry creation."""
        if self._get_entry().state != config_entries.ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")

        if user_input is not None:
            return self.async_create_entry(
                title=user_input[CONF_CHAT_MODEL],
                data=user_input,
            )

        entry = self._get_entry()
        available_models = await entry.runtime_data.async_get_available_models()

        return self.async_show_form(
            step_id="user",
            data_schema=_build_llm_schema(available_models),
            last_step=True,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Handle reconfiguration of existing LLM subentry."""
        if self._get_entry().state != config_entries.ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")

        subentry = self._get_reconfigure_subentry()

        if user_input is not None:
            return self.async_update_and_abort(
                subentry,
                title=user_input[CONF_CHAT_MODEL],
                data=user_input,
            )

        entry = self._get_entry()
        available_models = await entry.runtime_data.async_get_available_models()
        current_model = subentry.data.get(CONF_CHAT_MODEL)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_build_llm_schema(available_models, current_model),
            last_step=True,
        )

    async_step_init = async_step_user


class TTSSubentryFlowHandler(config_entries.ConfigSubentryFlow):
    """Flow for managing TTS engine subentries."""

    def __init__(self) -> None:
        """Initialize the subentry flow."""
        super().__init__()
        self._data: dict[str, Any] = {}

    def _get_available_tts_engines(self) -> list[str]:
        """Get available TTS engines from Home Assistant."""
        return [
            state.entity_id
            for state in self.hass.states.async_all()
            if state.entity_id.startswith("tts.")
        ]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Handle new TTS subentry creation."""
        if self._get_entry().state != config_entries.ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")

        if user_input is not None:
            return self.async_create_entry(
                title=user_input[CONF_TTS_ENGINE],
                data=user_input,
            )

        available_tts = self._get_available_tts_engines()

        return self.async_show_form(
            step_id="user",
            data_schema=_build_tts_schema(available_tts),
            last_step=True,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.SubentryFlowResult:
        """Handle reconfiguration of existing TTS subentry."""
        if self._get_entry().state != config_entries.ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_loaded")

        subentry = self._get_reconfigure_subentry()

        if user_input is not None:
            return self.async_update_and_abort(
                subentry,
                title=user_input[CONF_TTS_ENGINE],
                data=user_input,
            )

        available_tts = self._get_available_tts_engines()
        current_tts = subentry.data.get(CONF_TTS_ENGINE)

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_build_tts_schema(available_tts, current_tts),
            last_step=True,
        )

    async_step_init = async_step_user
