from homeassistant.core import HomeAssistant
from custom_components.yury_smarthome.entity import LocalLLMEntity
from abc import abstractmethod
from homeassistant.helpers import intent


class AbstractSkill:
    hass: HomeAssistant
    client: LocalLLMEntity

    def __init__(self, hass: HomeAssistant, client: LocalLLMEntity):
        self.hass = hass
        self.client = client

    @abstractmethod
    def name(self) -> str:
        """Returns skill name"""

    @abstractmethod
    async def process_user_request(self, request: str, response: intent.IntentResponse):
        """Proccesses user request"""
