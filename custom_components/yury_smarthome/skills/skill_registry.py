from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from custom_components.yury_smarthome.entity import LocalLLMEntity
from custom_components.yury_smarthome.prompt_cache import PromptCache
from custom_components.yury_smarthome.maybe import maybe
from .abstract_skill import AbstractSkill
from .control_devices import ControlDevices
from .shopping_list import ShoppingList
from .timers import Timers
from .world_clock import WorldClock
from .inbox_tasks import InboxTasks
from .reminders import Reminders
from homeassistant.components.conversation import ConversationInput
from typing import Tuple
from datetime import datetime
from custom_components.yury_smarthome.qpl import QPL, QPLFlow


class SkillRegistry:
    registry: dict[str, AbstractSkill]
    history: dict[str, Tuple[datetime, str]]

    def __init__(
        self,
        hass: HomeAssistant,
        client: LocalLLMEntity,
        prompt_cache: PromptCache,
        qpl_provider: QPL,
    ):
        inbox_tasks = InboxTasks(hass, client, prompt_cache)
        reminders = Reminders(hass, client, prompt_cache, qpl_provider)
        # Set up dependency: Reminders can delegate to InboxTasks
        reminders.set_inbox_tasks_skill(inbox_tasks)

        skills = [
            ControlDevices(hass, client, prompt_cache),
            ShoppingList(hass, client, prompt_cache),
            Timers(hass, client, prompt_cache, qpl_provider),
            WorldClock(hass, client, prompt_cache),
            inbox_tasks,
            reminders,
        ]
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
        qpl_flow: QPLFlow,
    ):
        if llm_response == "Undo":
            point = qpl_flow.mark_subspan_begin("undo")
            skill = self._get_skill_from_history(original_request)
            maybe(point).annotate("skill", skill.name())
            if skill is None:
                err = "Can't undo or too much time passed"
                qpl_flow.mark_failed(err)
                response.async_set_speech(err)
                return
            conversation_id = original_request.conversation_id
            if conversation_id is not None:
                del self.history[conversation_id]
            await skill.undo(response, qpl_flow)
            qpl_flow.mark_subspan_end("undo")
            return

        skill = self.registry.get(llm_response)
        if skill is not None:
            conversation_id = original_request.conversation_id
            if conversation_id is not None:
                self.history[conversation_id] = (datetime.now(), llm_response)
            await skill.process_user_request(original_request, response, qpl_flow)
        else:
            raise UnknownSkillException

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
            return self.registry.get(history_pair[1])
        return None


class UnknownSkillException(Exception):
    pass
