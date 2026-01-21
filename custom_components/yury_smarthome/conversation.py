"""Defines the various LLM Backend Agents"""

from __future__ import annotations

import logging
import os
from typing import Any, List, Literal, Tuple
from .qpl import QPL, QPLFlow

import aiofiles
from custom_components.yury_smarthome.skills.skill_registry import (
    SkillRegistry,
    UnknownSkillException,
)
from jinja2 import Template

from homeassistant.components import conversation
from homeassistant.components.conversation import (
    ConversationEntity,
    ConversationInput,
    ConversationResult,
)
from homeassistant.components.conversation.models import AbstractConversationAgent
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_LLM_HASS_API, MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_CHAT_MODEL, LLM_RETRY_COUNT
from .entity import LocalLLMClient, LocalLLMConfigEntry, LocalLLMEntity
from .prompt_cache import PromptCache
from .maybe import maybe
import json

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LocalLLMConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> bool:
    """Set up Local LLM Conversation from a config entry."""
    qpl_provider = QPL()
    for subentry in entry.subentries.values():
        if subentry.subentry_type != conversation.DOMAIN:
            continue

        if CONF_CHAT_MODEL not in subentry.data:
            _LOGGER.warning(
                "Conversation subentry %s missing required config key %s, You must delete the model and re-create it.",
                subentry.subentry_id,
                CONF_CHAT_MODEL,
            )
            continue

        # create one agent entity per conversation subentry
        agent_entity = LocalLLMAgent(
            hass, entry, subentry, entry.runtime_data, qpl_provider
        )
        # register the agent entity
        async_add_entities(
            [agent_entity],
            config_subentry_id=subentry.subentry_id,
        )

    return True


class LocalLLMAgent(ConversationEntity, AbstractConversationAgent, LocalLLMEntity):
    """Base Local LLM conversation agent."""

    skill_registry: SkillRegistry
    prompts: PromptCache
    qplProvider: QPL

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        subentry: ConfigSubentry,
        client: LocalLLMClient,
        qplProvider: QPL,
    ) -> None:
        super().__init__(hass, entry, subentry, client)

        self.qplProvider = qplProvider
        self.prompts = PromptCache()
        self.skill_registry = SkillRegistry(hass, self, self.prompts, qplProvider)

        if subentry.data.get(CONF_LLM_HASS_API):
            self._attr_supported_features = (
                conversation.ConversationEntityFeature.CONTROL
            )

    async def async_added_to_hass(self) -> None:
        """When entity is added to Home Assistant."""
        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self.entry, self)

    async def async_will_remove_from_hass(self) -> None:
        """When entity will be removed from Home Assistant."""
        conversation.async_unset_agent(self.hass, self.entry)
        await super().async_will_remove_from_hass()

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return a list of supported languages."""
        return MATCH_ALL

    async def send_message(self, prompt: str) -> str:
        model = self.subentry.data[CONF_CHAT_MODEL]
        response = await self.client.send_message(model, prompt)
        return response if response else "No response"

    async def _async_process(
        self, user_input: ConversationInput, qpl_flow: QPLFlow
    ) -> ConversationResult:
        qpl_flow.mark_subspan_begin("building_prompt")
        prompt_path = self._make_prompt_key("entry.md")
        entry_prompt_template = await self.prompts.get(prompt_path)
        template = Template(entry_prompt_template, trim_blocks=True)
        skill_list = self.skill_registry.skill_list()
        prompt = template.render(skill_list=skill_list, prompt=user_input.text)
        point = qpl_flow.mark_subspan_end("building_prompt")
        maybe(point).annotate("prompt", prompt)
        updated_prompt = None

        intent_response = intent.IntentResponse(language="en")
        for _ in range(LLM_RETRY_COUNT):
            try:
                qpl_flow.mark_subspan_begin("sending_prompt")
                llm_response = await self.send_message(
                    updated_prompt if updated_prompt is not None else prompt
                )
                llm_response = llm_response.strip()
                point = qpl_flow.mark_subspan_end("sending_prompt")
                maybe(point).annotate("llm_response", llm_response)
                qpl_flow.mark_subspan_begin("processing_user_request")
                await self.skill_registry.process_user_request(
                    llm_response, user_input, intent_response, qpl_flow
                )
                qpl_flow.mark_subspan_end("processing_user_request")
                qpl_flow.mark_success()
                return ConversationResult(
                    response=intent_response, conversation_id=user_input.conversation_id
                )
            except UnknownSkillException:
                if updated_prompt is None:
                    updated_prompt_path = self._make_prompt_key("entry_retry.md")
                    template = Template(updated_prompt_path, trim_blocks=True)
                    updated_prompt = template.render(
                        original_prompt=prompt, skill_list=skill_list
                    )
                continue

        error = "Failed to find appropriate skill"
        intent_response.async_set_speech(error)
        qpl_flow.mark_subspan_end("processing_user_prompt")
        qpl_flow.mark_failed(error)
        return ConversationResult(
            response=intent_response, conversation_id=user_input.conversation_id
        )

    async def async_process(self, user_input: ConversationInput) -> ConversationResult:
        qpl_flow = self.qplProvider.create_flow("processing_user_prompt")
        qpl_flow.mark_subspan_begin("async_process")
        qpl_flow.annotate("user_input", user_input.text)
        qpl_flow.annotate("conversation_id", user_input.conversation_id)
        context = "none"
        if user_input.context is not None:
            context = json.dumps(user_input.context.as_dict())
        qpl_flow.annotate("context", context)
        qpl_flow.annotate(
            "device_id",
            user_input.device_id if user_input.device_id else "unknown device",
        )
        qpl_flow.annotate(
            "satellite_id",
            user_input.satellite_id if user_input.satellite_id else "unknown satellite",
        )
        qpl_flow.annotate("language", user_input.language)
        qpl_flow.annotate("agent_id", user_input.agent_id)
        qpl_flow.annotate(
            "extra_system_prompt",
            user_input.extra_system_prompt
            if user_input.extra_system_prompt
            else "none",
        )

        result = await self._async_process(user_input, qpl_flow)
        qpl_flow.mark_subspan_end("async_process")
        return result

    def _make_prompt_key(self, name: str) -> str:
        return os.path.join(os.path.dirname(__file__), "prompts", name)
