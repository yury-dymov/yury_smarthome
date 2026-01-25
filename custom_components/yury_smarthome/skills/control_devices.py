from .abstract_skill import AbstractSkill
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.helpers import entity_registry, area_registry, device_registry
import json
import os
from jinja2 import Template
from homeassistant.helpers import intent
from homeassistant.components.conversation import ConversationInput
from custom_components.yury_smarthome.qpl import QPLFlow
from custom_components.yury_smarthome.maybe import maybe
from custom_components.yury_smarthome.prompt_cache import PromptCache
import traceback


class ControlDevices(AbstractSkill):
    intents: list[intent.Intent]

    def name(self) -> str:
        return "Control Devices Other Than Music"

    async def process_user_request(
        self,
        request: ConversationInput,
        response: intent.IntentResponse,
        qpl_flow: QPLFlow,
    ):
        qpl_flow.mark_subspan_begin("building_prompt")
        prompt = await self._build_prompt(request, qpl_flow)
        point = qpl_flow.mark_subspan_end("building_prompt")
        maybe(point).annotate("prompt", prompt)
        qpl_flow.mark_subspan_begin("sending_message_to_llm")
        llm_response = await self.client.send_message(prompt)
        point = qpl_flow.mark_subspan_end("sending_message_to_llm")
        llm_response = llm_response.replace("```json", "")
        llm_response = llm_response.replace("```", "")
        maybe(point).annotate("llm_response", llm_response)
        did_something = False
        try:
            json_data = json.loads(llm_response)
            for device in json_data["devices"]:
                entity_id = device["entity_id"]
                action = device["action"]
                if entity_id is None or action is None:
                    continue
                if action == "turn on":
                    action_intent = intent.INTENT_TURN_ON
                elif action == "turn off":
                    action_intent = intent.INTENT_TURN_OFF
                else:
                    continue
                did_something = True
                intent_item = intent.Intent(
                    self.hass,
                    "yury",
                    action_intent,
                    {"name": {"value": entity_id}},
                    None,
                    intent.Context(),
                    request.language,
                )
                point = qpl_flow.mark_subspan_begin("sending_intent")
                maybe(point).annotate("action", action_intent)
                maybe(point).annotate("entity_id", entity_id)
                handler = self.hass.data.get(intent.DATA_KEY, {}).get(action_intent)
                await handler.async_handle(intent_item)
                qpl_flow.mark_subspan_end("sending_intent")
                self.intents.append(intent_item)

            if did_something:
                response.async_set_speech("All done")
            else:
                response.async_set_speech("Didn't find any device")
        except json.JSONDecodeError as e:
            qpl_flow.mark_failed(e.msg)
            response.async_set_speech("Failed")
        except Exception as e:
            qpl_flow.mark_failed(traceback.format_exc())
            response.async_set_speech("Failed to control devices")

    async def undo(self, response: intent.IntentResponse, qpl_flow: QPLFlow):
        point = qpl_flow.mark_subspan_begin("control_devices_undo")
        if len(self.intents) == 0:
            maybe(point).annotate("no intents")
            response.async_set_speech("All done")
            point = qpl_flow.mark_subspan_end("control_devices_undo")
            return

        for intent_elem in self.intents:
            if intent_elem.intent_type == intent.INTENT_TURN_ON:
                intent_elem.intent_type = intent.INTENT_TURN_OFF
            elif intent_elem.intent_type == intent.INTENT_TURN_OFF:
                intent_elem.intent_type = intent.INTENT_TURN_ON
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
            maybe(point).annotate("entity_id", intent_elem.slots["name"]["value"])
            undo_response = await handler.async_handle(intent_elem)
            point = qpl_flow.mark_subspan_end("undo_action")
            maybe(point).annotate("result", undo_response.response_type.value)

        response.async_set_speech("All done")
        self.intents = []
        point = qpl_flow.mark_subspan_end("control_devices_undo")

    async def _build_prompt(self, request: ConversationInput, qpl_flow: QPLFlow) -> str:
        qpl_flow.mark_subspan_begin("fetching_device_list_from_ha")
        entities = []
        self.intents = []
        er = entity_registry.async_get(self.hass)
        dr = device_registry.async_get(self.hass)
        ar = area_registry.async_get(self.hass)
        entity_dict = {}
        device_dict = {}
        user_location = None

        # Determine user's location from the device they're using (e.g., voice assistant)
        if request.device_id:
            user_device = dr.async_get(request.device_id)
            if user_device and user_device.area_id:
                user_area = ar.async_get_area(user_device.area_id)
                if user_area:
                    user_location = user_area.name

        for state in self.hass.states.async_all():
            if not async_should_expose(self.hass, conversation.DOMAIN, state.entity_id):
                continue
            entry = {}
            entry["entity_id"] = state.entity_id
            entity_dict[state.entity_id] = state
            entity = er.async_get(state.entity_id)
            device = None
            if entity and entity.device_id:
                device = dr.async_get(entity.device_id)
                device_dict[state.entity_id] = entity.device_id

            if state.state not in {"on", "off"}:
                continue

            attributes = dict(state.attributes)
            attributes["state"] = state.state
            entry["state"] = state.state
            entry["friendly_name"] = state.name

            if entity:
                if entity.aliases:
                    attributes["aliases"] = entity.aliases

                if entity.unit_of_measurement:
                    attributes["state"] = (
                        attributes["state"] + " " + entity.unit_of_measurement
                    )

            # area could be on device or entity. prefer device area
            area_id = None
            if device and device.area_id:
                area_id = device.area_id
            if entity and entity.area_id:
                area_id = entity.area_id

            if area_id:
                area = ar.async_get_area(area_id)
                if area:
                    attributes["area_id"] = area.id
                    attributes["area_name"] = area.name
                    entry["area"] = area.name

            entities.append(entry)

        qpl_flow.mark_subspan_end("fetching_device_list_from_ha")
        qpl_flow.mark_subspan_begin("rendering_prompt_template")
        device_list = json.dumps(entities)
        prompt_key = os.path.join(os.path.dirname(__file__), "control_devices.md")
        prompt_template = await self.prompt_cache.get(prompt_key, request.conversation_id)
        template = Template(prompt_template, trim_blocks=True)

        result = template.render(
            device_list=device_list,
            user_prompt=request.text,
            user_location=user_location,
        )
        qpl_flow.mark_subspan_end("rendering_prompt_template")
        return result
