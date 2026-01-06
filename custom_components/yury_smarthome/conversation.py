"""Defines the various LLM Backend Agents"""

from __future__ import annotations
from typing import Literal, List, Tuple, Any
import logging

from homeassistant.components.conversation import (
    ConversationInput,
    ConversationResult,
    ConversationEntity,
)
from homeassistant.components.conversation.models import AbstractConversationAgent
from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_LLM_HASS_API, MATCH_ALL
from homeassistant.exceptions import TemplateError, HomeAssistantError
from homeassistant.helpers import chat_session, intent, llm
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from jinja2 import Template

from .entity import LocalLLMEntity, LocalLLMClient, LocalLLMConfigEntry
from .const import (
    CONF_CHAT_MODEL,
    DOMAIN,
)
import os
import aiofiles

from custom_components.yury_smarthome.skills.skill_registry import SkillRegistry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LocalLLMConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> bool:
    """Set up Local LLM Conversation from a config entry."""

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
        agent_entity = LocalLLMAgent(hass, entry, subentry, entry.runtime_data)
        # register the agent entity
        async_add_entities(
            [agent_entity],
            config_subentry_id=subentry.subentry_id,
        )

    return True


class LocalLLMAgent(ConversationEntity, AbstractConversationAgent, LocalLLMEntity):
    """Base Local LLM conversation agent."""

    skill_registry: SkillRegistry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        subentry: ConfigSubentry,
        client: LocalLLMClient,
    ) -> None:
        super().__init__(hass, entry, subentry, client)

        self.skill_registry = SkillRegistry(hass, self)

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

    async def async_process(self, user_input: ConversationInput) -> ConversationResult:
        file_path = os.path.join(os.path.dirname(__file__), "prompts", "entry.md")
        async with aiofiles.open(file_path, mode="r") as file:
            template = Template(await file.read(), trim_blocks=True)
        prompt = template.render(
            skill_list=self.skill_registry.skill_list(), prompt=user_input.text
        )
        llm_response = await self.send_message(prompt)
        intent_response = intent.IntentResponse(language=user_input.language)
        await self.skill_registry.process_user_request(
            llm_response, user_input.text, intent_response
        )
        return ConversationResult(
            response=intent_response, conversation_id=user_input.conversation_id
        )
