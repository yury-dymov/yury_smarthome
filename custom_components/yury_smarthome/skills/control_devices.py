from .abstract_skill import AbstractSkill
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.helpers import entity_registry, area_registry, device_registry
import json
import os
import aiofiles
from jinja2 import Template
from homeassistant.helpers import intent
from homeassistant.components.conversation import ConversationInput


class ControlDevices(AbstractSkill):
    def name(self) -> str:
        return "Control Devices Other Than Music"

    async def process_user_request(
        self, request: ConversationInput, response: intent.IntentResponse
    ):
        entities = []

        er = entity_registry.async_get(self.hass)
        dr = device_registry.async_get(self.hass)
        ar = area_registry.async_get(self.hass)
        entity_dict = {}
        device_dict = {}
        user_location = None

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
                    if device and device.id == request.device_id:
                        user_location = area.name

            entities.append(entry)

        device_list = json.dumps(entities)
        file_path = os.path.join(os.path.dirname(__file__), "control_devices.md")
        async with aiofiles.open(file_path, mode="r") as file:
            template = Template(await file.read(), trim_blocks=True)

        prompt = template.render(
            device_list=device_list,
            user_prompt=request.text,
            user_location=user_location,
        )

        llm_response = await self.client.send_message(prompt)
        try:
            llm_response = llm_response.replace("```json", "")
            llm_response = llm_response.replace("```", "")
            json_data = json.loads(llm_response)
            for device in json_data["devices"]:
                entity_id = device["entity_id"]
                action = device["action"]
                if entity_id is None or action is None:
                    continue
                if action == "turn on":
                    await intent.async_handle(
                        self.hass,
                        "yury",
                        intent.INTENT_TURN_ON,
                        {"name": {"value": entity_id}},
                    )
                elif action == "turn off":
                    await intent.async_handle(
                        self.hass,
                        "yury",
                        intent.INTENT_TURN_OFF,
                        {"name": {"value": entity_id}},
                    )
                else:
                    continue

            response.async_set_speech("All done")
        except json.JSONDecodeError as e:
            response.async_set_speech("Failed")
