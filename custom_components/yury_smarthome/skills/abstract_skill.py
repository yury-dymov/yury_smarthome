from homeassistant.core import HomeAssistant
from custom_components.yury_smarthome.entity import LocalLLMEntity
from custom_components.yury_smarthome.prompt_cache import PromptCache
from custom_components.yury_smarthome.qpl import QPLFlow
from abc import abstractmethod
from homeassistant.helpers import intent
from homeassistant.components.conversation import ConversationInput


class AbstractSkill:
    hass: HomeAssistant
    client: LocalLLMEntity
    prompt_cache: PromptCache

    def __init__(
        self, hass: HomeAssistant, client: LocalLLMEntity, prompt_cache: PromptCache
    ):
        self.hass = hass
        self.client = client
        self.prompt_cache = prompt_cache

    @abstractmethod
    def name(self) -> str:
        """Returns skill name"""

    @abstractmethod
    async def process_user_request(
        self,
        request: ConversationInput,
        response: intent.IntentResponse,
        qplFlow: QPLFlow,
    ):
        """Proccesses user request"""

    @abstractmethod
    async def undo(self, response: intent.IntentResponse, qplFlow: QPLFlow):
        """Revert the last action"""
