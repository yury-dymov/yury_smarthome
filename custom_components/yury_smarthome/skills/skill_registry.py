from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from custom_components.yury_smarthome.entity import LocalLLMEntity
from .abstract_skill import AbstractSkill
from .control_devices import ControlDevices
import json
from homeassistant.components.conversation import ConversationInput
from typing import Tuple
from datetime import datetime


class SkillRegistry:
    registry: dict[str, AbstractSkill]
    history: dict[str, Tuple[datetime, str]]

    def __init__(self, hass: HomeAssistant, client: LocalLLMEntity):
        skills = [ControlDevices(hass, client)]
        registry = {}
        for skill in skills:
            registry[skill.name()] = skill
        self.registry = registry
        self.history = {}

    def skill_list(self) -> str:
        names = map(lambda x: '"' + x.name() + '"', self.registry.values())
        return ", ".join(names)

    async def process_user_request(
        self,
        llm_response: str,
        original_request: ConversationInput,
        response: intent.IntentResponse,
    ):
        if llm_response == "Undo":
            skill = self._get_skill_from_history(original_request)
            if skill is None:
                response.async_set_speech("Can't undo or too much time passed")
                return
            conversation_id = original_request.conversation_id
            if conversation_id is not None:
                del self.history[conversation_id]
            await skill.undo(response)
            return

        skill = self.registry[llm_response]
        if skill is not None:
            conversation_id = original_request.conversation_id
            if conversation_id is not None:
                self.history[conversation_id] = (datetime.now(), llm_response)
            await skill.process_user_request(original_request, response)

    def _get_skill_from_history(
        self, original_request: ConversationInput
    ) -> AbstractSkill | None:
        conversation_id = original_request.conversation_id
        if conversation_id is None:
            return None

        history_pair = self.history.get(conversation_id)
        if history_pair is None:
            return None
        if (datetime.now() - history_pair[0]).total_seconds() <= 30:
            return self.registry[history_pair[1]]
        return None
