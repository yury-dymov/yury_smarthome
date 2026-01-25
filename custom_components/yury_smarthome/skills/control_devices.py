from .abstract_skill import AbstractSkill
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.helpers import entity_registry, area_registry, device_registry
from dataclasses import dataclass
import json
import logging
import os
from jinja2 import Template
from homeassistant.helpers import intent
from homeassistant.components.conversation import ConversationInput
from custom_components.yury_smarthome.qpl import QPLFlow
from custom_components.yury_smarthome.maybe import maybe
from custom_components.yury_smarthome.prompt_cache import PromptCache
import traceback


_LOGGER = logging.getLogger(__name__)


@dataclass
class DeviceAction:
    """Tracks a device action for undo support."""
    entity_id: str
    action: str  # "turn on", "turn off", "set brightness", "brighten", "darken"
    previous_state: str | None = None  # "on" or "off"
    previous_brightness: int | None = None  # 0-255 (HA native scale)


class ControlDevices(AbstractSkill):
    last_actions: list[DeviceAction]

    def __init__(self, hass, client, prompt_cache):
        super().__init__(hass, client, prompt_cache)
        self.last_actions = []

    def name(self) -> str:
        return "Control Devices Other Than Music"

    async def process_user_request(
        self,
        request: ConversationInput,
        response: intent.IntentResponse,
        qpl_flow: QPLFlow,
    ):
        self.last_actions = []
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

        try:
            json_data = json.loads(llm_response)
            actions_performed = []

            for device in json_data["devices"]:
                entity_id = device.get("entity_id")
                action = device.get("action")
                brightness = device.get("brightness")

                if entity_id is None or action is None:
                    continue

                point = qpl_flow.mark_subspan_begin("executing_action")
                maybe(point).annotate("action", action)
                maybe(point).annotate("entity_id", entity_id)
                maybe(point).annotate("brightness", brightness)

                try:
                    if action == "turn on":
                        await self._turn_on(entity_id, qpl_flow)
                        actions_performed.append("turned on")
                    elif action == "turn off":
                        await self._turn_off(entity_id, qpl_flow)
                        actions_performed.append("turned off")
                    elif action == "set brightness":
                        if brightness is not None:
                            await self._set_brightness(entity_id, brightness, qpl_flow)
                            actions_performed.append(f"set brightness to {brightness}%")
                    elif action == "brighten":
                        amount = brightness if brightness is not None else 20
                        await self._adjust_brightness(entity_id, amount, qpl_flow)
                        actions_performed.append("increased brightness")
                    elif action == "darken":
                        amount = brightness if brightness is not None else 20
                        await self._adjust_brightness(entity_id, -amount, qpl_flow)
                        actions_performed.append("decreased brightness")
                finally:
                    qpl_flow.mark_subspan_end("executing_action")

            if actions_performed:
                # Use descriptive response so conversation history is useful for follow-ups
                response.async_set_speech(", ".join(set(actions_performed)))
            else:
                response.async_set_speech("Didn't find any device")
        except json.JSONDecodeError as e:
            qpl_flow.mark_failed(e.msg)
            response.async_set_speech("Failed")
        except Exception:
            qpl_flow.mark_failed(traceback.format_exc())
            response.async_set_speech("Failed to control devices")

    async def _turn_on(self, entity_id: str, qpl_flow: QPLFlow):
        """Turn on a device."""
        # Get current state for undo
        state = self.hass.states.get(entity_id)
        previous_state = state.state if state else None
        previous_brightness = None
        if state and "brightness" in state.attributes:
            previous_brightness = state.attributes["brightness"]

        await self.hass.services.async_call(
            "homeassistant",
            "turn_on",
            {"entity_id": entity_id},
            blocking=True,
        )

        self.last_actions.append(DeviceAction(
            entity_id=entity_id,
            action="turn on",
            previous_state=previous_state,
            previous_brightness=previous_brightness,
        ))

    async def _turn_off(self, entity_id: str, qpl_flow: QPLFlow):
        """Turn off a device."""
        # Get current state for undo
        state = self.hass.states.get(entity_id)
        previous_state = state.state if state else None
        previous_brightness = None
        if state and "brightness" in state.attributes:
            previous_brightness = state.attributes["brightness"]

        await self.hass.services.async_call(
            "homeassistant",
            "turn_off",
            {"entity_id": entity_id},
            blocking=True,
        )

        self.last_actions.append(DeviceAction(
            entity_id=entity_id,
            action="turn off",
            previous_state=previous_state,
            previous_brightness=previous_brightness,
        ))

    async def _set_brightness(self, entity_id: str, brightness_pct: int, qpl_flow: QPLFlow):
        """Set brightness to an absolute value (0-100%)."""
        # Get current state for undo
        state = self.hass.states.get(entity_id)
        previous_state = state.state if state else None
        previous_brightness = None
        if state and "brightness" in state.attributes:
            previous_brightness = state.attributes["brightness"]

        # Convert percentage (0-100) to HA brightness (0-255)
        brightness_pct = max(0, min(100, brightness_pct))
        brightness_ha = int(brightness_pct * 255 / 100)

        # Use light domain for brightness control
        domain = entity_id.split(".")[0]
        if domain == "light":
            await self.hass.services.async_call(
                "light",
                "turn_on",
                {"entity_id": entity_id, "brightness": brightness_ha},
                blocking=True,
            )
        else:
            # For non-lights, just turn on/off based on brightness
            if brightness_pct > 0:
                await self.hass.services.async_call(
                    "homeassistant",
                    "turn_on",
                    {"entity_id": entity_id},
                    blocking=True,
                )
            else:
                await self.hass.services.async_call(
                    "homeassistant",
                    "turn_off",
                    {"entity_id": entity_id},
                    blocking=True,
                )

        self.last_actions.append(DeviceAction(
            entity_id=entity_id,
            action="set brightness",
            previous_state=previous_state,
            previous_brightness=previous_brightness,
        ))

    async def _adjust_brightness(self, entity_id: str, amount_pct: int, qpl_flow: QPLFlow):
        """Adjust brightness by a relative amount (-100 to +100%)."""
        # Get current state for undo and current brightness
        state = self.hass.states.get(entity_id)
        previous_state = state.state if state else None
        previous_brightness = None
        current_brightness_pct = 50  # Default if unknown

        if state:
            previous_brightness = state.attributes.get("brightness")
            if previous_brightness is not None:
                current_brightness_pct = int(previous_brightness * 100 / 255)
            elif state.state == "on":
                current_brightness_pct = 100
            elif state.state == "off":
                current_brightness_pct = 0

        # Calculate new brightness
        new_brightness_pct = max(0, min(100, current_brightness_pct + amount_pct))
        new_brightness_ha = int(new_brightness_pct * 255 / 100)

        # Use light domain for brightness control
        domain = entity_id.split(".")[0]
        if domain == "light":
            if new_brightness_pct > 0:
                await self.hass.services.async_call(
                    "light",
                    "turn_on",
                    {"entity_id": entity_id, "brightness": new_brightness_ha},
                    blocking=True,
                )
            else:
                await self.hass.services.async_call(
                    "light",
                    "turn_off",
                    {"entity_id": entity_id},
                    blocking=True,
                )
        else:
            # For non-lights, just turn on/off
            if new_brightness_pct > 0:
                await self.hass.services.async_call(
                    "homeassistant",
                    "turn_on",
                    {"entity_id": entity_id},
                    blocking=True,
                )
            else:
                await self.hass.services.async_call(
                    "homeassistant",
                    "turn_off",
                    {"entity_id": entity_id},
                    blocking=True,
                )

        self.last_actions.append(DeviceAction(
            entity_id=entity_id,
            action="brighten" if amount_pct > 0 else "darken",
            previous_state=previous_state,
            previous_brightness=previous_brightness,
        ))

    async def undo(self, response: intent.IntentResponse, qpl_flow: QPLFlow):
        point = qpl_flow.mark_subspan_begin("control_devices_undo")

        try:
            if not self.last_actions:
                maybe(point).annotate("no actions to undo")
                response.async_set_speech("Nothing to undo")
                return

            for action in self.last_actions:
                maybe(point).annotate("undoing", action.action)
                maybe(point).annotate("entity_id", action.entity_id)

                domain = action.entity_id.split(".")[0]

                if action.action in ("turn on", "turn off"):
                    # Reverse the on/off action
                    if action.previous_state == "on":
                        if domain == "light" and action.previous_brightness is not None:
                            await self.hass.services.async_call(
                                "light",
                                "turn_on",
                                {"entity_id": action.entity_id, "brightness": action.previous_brightness},
                                blocking=True,
                            )
                        else:
                            await self.hass.services.async_call(
                                "homeassistant",
                                "turn_on",
                                {"entity_id": action.entity_id},
                                blocking=True,
                            )
                    elif action.previous_state == "off":
                        await self.hass.services.async_call(
                            "homeassistant",
                            "turn_off",
                            {"entity_id": action.entity_id},
                            blocking=True,
                        )
                    else:
                        # Unknown previous state, reverse the action
                        if action.action == "turn on":
                            await self.hass.services.async_call(
                                "homeassistant",
                                "turn_off",
                                {"entity_id": action.entity_id},
                                blocking=True,
                            )
                        else:
                            await self.hass.services.async_call(
                                "homeassistant",
                                "turn_on",
                                {"entity_id": action.entity_id},
                                blocking=True,
                            )

                elif action.action in ("set brightness", "brighten", "darken"):
                    # Restore previous brightness
                    if action.previous_brightness is not None:
                        if domain == "light":
                            await self.hass.services.async_call(
                                "light",
                                "turn_on",
                                {"entity_id": action.entity_id, "brightness": action.previous_brightness},
                                blocking=True,
                            )
                    elif action.previous_state == "off":
                        await self.hass.services.async_call(
                            "homeassistant",
                            "turn_off",
                            {"entity_id": action.entity_id},
                            blocking=True,
                        )

            self.last_actions = []
            response.async_set_speech("All done")
        finally:
            qpl_flow.mark_subspan_end("control_devices_undo")

    async def _build_prompt(self, request: ConversationInput, qpl_flow: QPLFlow) -> str:
        qpl_flow.mark_subspan_begin("fetching_device_list_from_ha")
        entities = []
        er = entity_registry.async_get(self.hass)
        dr = device_registry.async_get(self.hass)
        ar = area_registry.async_get(self.hass)
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

            entity = er.async_get(state.entity_id)
            device = None
            if entity and entity.device_id:
                device = dr.async_get(entity.device_id)

            if state.state not in {"on", "off"}:
                continue

            entry = {
                "entity_id": state.entity_id,
                "state": state.state,
                "friendly_name": state.name,
            }

            # Include brightness for lights
            if state.entity_id.startswith("light."):
                brightness_ha = state.attributes.get("brightness")
                if brightness_ha is not None:
                    entry["brightness"] = int(brightness_ha * 100 / 255)

            # area could be on device or entity. prefer device area
            area_id = None
            if device and device.area_id:
                area_id = device.area_id
            if entity and entity.area_id:
                area_id = entity.area_id

            if area_id:
                area = ar.async_get_area(area_id)
                if area:
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
