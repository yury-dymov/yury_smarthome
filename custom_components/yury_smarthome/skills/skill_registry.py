from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from custom_components.yury_smarthome.entity import LocalLLMEntity
from .abstract_skill import AbstractSkill
from .control_devices import ControlDevices
import json


class SkillRegistry:
    registry: dict[str, AbstractSkill]

    def __init__(self, hass: HomeAssistant, client: LocalLLMEntity):
        skills = [ControlDevices(hass, client)]
        registry = {}
        for skill in skills:
            registry[skill.name()] = skill
        self.registry = registry

    def skill_list(self) -> str:
        names = map(lambda x: '"' + x.name() + '"', self.registry.values())
        return ", ".join(names)

    async def process_user_request(
        self, llm_response: str, original_request: str, response: intent.IntentResponse
    ):
        skill = self.registry[llm_response]
        if skill is not None:
            await skill.process_user_request(original_request, response)
