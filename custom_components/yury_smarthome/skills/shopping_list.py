from .abstract_skill import AbstractSkill
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.helpers import entity_registry, area_registry, device_registry
import json
import os
from jinja2 import Template
from homeassistant.helpers import intent
from homeassistant.components.conversation import ConversationInput
from homeassistant.components.todo.intent import (
    INTENT_LIST_ADD_ITEM,
    INTENT_LIST_COMPLETE_ITEM,
)
from custom_components.yury_smarthome.qpl import QPLFlow
from custom_components.yury_smarthome.maybe import maybe
from custom_components.yury_smarthome.prompt_cache import PromptCache


class ShoppingList(AbstractSkill):
    intents: list[intent.Intent]

    def name(self) -> str:
        return "Shopping List"

    async def process_user_request(
        self,
        request: ConversationInput,
        response: intent.IntentResponse,
        qpl_flow: QPLFlow,
    ):
        self.intents = []
        prompt = await self._build_prompt(request, qpl_flow)
        qpl_flow.mark_subspan_begin("sending_message_to_llm")
        llm_response = await self.client.send_message(prompt)
        point = qpl_flow.mark_subspan_end("sending_message_to_llm")
        llm_response = llm_response.replace("```json", "")
        llm_response = llm_response.replace("```", "")
        maybe(point).annotate("llm_response", llm_response)
        try:
            json_data = json.loads(llm_response)
            action = json_data["action"]
            if action is None or action not in {"add", "remove"}:
                err = "No action was defined"
                qpl_flow.mark_canceled(err)
                response.async_set_speech(err)
                return
            items = json_data["items"]
            entity_id = json_data["entity_id"]
            if entity_id is None:
                err = "No matching shopping list was found"
                qpl_flow.mark_canceled(err)
                response.async_set_speech(err)
                return
            if items is None or len(items) == 0:
                err = "No items to add were identified"
                qpl_flow.mark_canceled(err)
                response.async_set_speech(err)
                return

            for item in json_data["items"]:
                if action == "add":
                    action_intent = INTENT_LIST_ADD_ITEM
                elif action == "remove":
                    action_intent = INTENT_LIST_COMPLETE_ITEM
                else:
                    continue
                intent_item = intent.Intent(
                    self.hass,
                    "yury",
                    action_intent,
                    {"name": {"value": entity_id}, "item": {"value": item}},
                    None,
                    intent.Context(),
                    request.language,
                )
                point = qpl_flow.mark_subspan_begin("sending_intent")
                maybe(point).annotate("action", action_intent)
                maybe(point).annotate("item", item)
                handler = self.hass.data.get(intent.DATA_KEY, {}).get(action_intent)
                await handler.async_handle(intent_item)
                qpl_flow.mark_subspan_end("sending_intent")
                self.intents.append(intent_item)

            answer = self._build_answer(action, items)
            response.async_set_speech(answer)
        except json.JSONDecodeError as e:
            qpl_flow.mark_failed(e.msg)
            response.async_set_speech("Failed")

    def _build_answer(self, action: str, items: list[str]) -> str:
        if len(items) == 1:
            return "{action_name} {item} {article} the shopping list".format(
                action_name="Added" if action == "add" else "Removed",
                article="to" if action == "add" else "from",
                item=items[0],
            )
        else:
            return "{action_name} {count} items {article} the shopping list: {list_of_items}".format(
                action_name="Added" if action == "add" else "Removed",
                article="to" if action == "add" else "from",
                count=len(items),
                list_of_items=", ".join(items),
            )

    async def undo(self, response: intent.IntentResponse, qpl_flow: QPLFlow):
        point = qpl_flow.mark_subspan_begin("shopping_list_undo")
        if len(self.intents) == 0:
            maybe(point).annotate("no intents")
            response.async_set_speech("All done")
            point = qpl_flow.mark_subspan_end("shopping_list_undo")
            return

        for intent_elem in self.intents:
            if intent_elem.intent_type == INTENT_LIST_ADD_ITEM:
                intent_elem.intent_type = INTENT_LIST_COMPLETE_ITEM
            elif intent_elem.intent_type == INTENT_LIST_COMPLETE_ITEM:
                intent_elem.intent_type = INTENT_LIST_ADD_ITEM
            else:
                err = (
                    "Unsupported intent to undo in control devices skill: "
                    + intent_elem.intent_type
                )
                response.async_set_speech(err)
                qpl_flow.mark_failed(err)
                return

            point = qpl_flow.mark_subspan_begin("undo_action")
            handler = self.hass.data.get(intent.DATA_KEY, {}).get(
                intent_elem.intent_type
            )
            maybe(point).annotate("intent_type", intent_elem.intent_type)
            maybe(point).annotate("item", intent_elem.slots["item"]["value"])
            maybe(point).annotate("name", intent_elem.slots["name"]["value"])
            undo_response = await handler.async_handle(intent_elem)
            point = qpl_flow.mark_subspan_end("undo_action")
            maybe(point).annotate("result", undo_response.response_type.value)

        response.async_set_speech("All done")
        self.intents = []
        point = qpl_flow.mark_subspan_end("shopping_list_undo")

    async def _build_prompt(self, request: ConversationInput, qpl_flow: QPLFlow) -> str:
        entities = []

        qpl_flow.mark_subspan_begin("build_prompt")
        qpl_flow.mark_subspan_begin("quering_entities_from_ha")
        for state in self.hass.states.async_all():
            if not async_should_expose(self.hass, conversation.DOMAIN, state.entity_id):
                continue
            entry = {}
            if "todo" not in state.entity_id:
                continue
            entry["entity_id"] = state.entity_id
            attributes = dict(state.attributes)
            attributes["state"] = state.state
            entry["friendly_name"] = state.name
            entities.append(entry)

        point = qpl_flow.mark_subspan_end("quering_entities_from_ha")
        device_list = json.dumps(entities)
        maybe(point).annotate("entity_list", device_list)
        qpl_flow.mark_subspan_begin("render_prompt")
        prompt_key = os.path.join(
            os.path.dirname(__file__), "shopping_list_todo_list.md"
        )
        prompt_template = await self.prompt_cache.get(prompt_key)
        template = Template(prompt_template, trim_blocks=True)

        output = template.render(
            device_list=device_list,
            user_prompt=request.text,
        )
        point = qpl_flow.mark_subspan_end("render_prompt")
        maybe(point).annotate("prompt", output)
        qpl_flow.mark_subspan_end("build_prompt")
        return output
