from .abstract_skill import AbstractSkill
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
import json
import os
import re
from dataclasses import dataclass
from jinja2 import Template
from homeassistant.helpers import intent
from homeassistant.components.conversation import ConversationInput
from custom_components.yury_smarthome.qpl import QPLFlow
from custom_components.yury_smarthome.maybe import maybe
import traceback


@dataclass
class TimerAction:
    action: str  # "start", "cancel", "pause", "resume"
    entity_id: str
    duration: str | None = None


class Timers(AbstractSkill):
    last_action: TimerAction | None = None

    def name(self) -> str:
        return "Timers"

    async def process_user_request(
        self,
        request: ConversationInput,
        response: intent.IntentResponse,
        qpl_flow: QPLFlow,
    ):
        self.last_action = None
        prompt = await self._build_prompt(request, qpl_flow)
        qpl_flow.mark_subspan_begin("sending_message_to_llm")
        llm_response = await self.client.send_message(prompt)
        point = qpl_flow.mark_subspan_end("sending_message_to_llm")
        llm_response = llm_response.replace("```json", "")
        llm_response = llm_response.replace("```", "")
        maybe(point).annotate("llm_response", llm_response)

        try:
            json_data = json.loads(llm_response)
            action = json_data.get("action")
            if action is None or action not in {"start", "cancel", "pause", "resume"}:
                err = "No valid action was defined"
                qpl_flow.mark_canceled(err)
                response.async_set_speech(err)
                return

            entity_id = json_data.get("entity_id")
            duration = json_data.get("duration")

            if action == "start":
                await self._start_timer(entity_id, duration, response, qpl_flow)
            elif action == "cancel":
                await self._cancel_timer(entity_id, response, qpl_flow)
            elif action == "pause":
                await self._pause_timer(entity_id, response, qpl_flow)
            elif action == "resume":
                await self._resume_timer(entity_id, response, qpl_flow)

        except json.JSONDecodeError as e:
            qpl_flow.mark_failed(e.msg)
            response.async_set_speech("Failed to understand timer request")
        except Exception as e:
            qpl_flow.mark_failed(traceback.format_exc())
            response.async_set_speech("Failed to set timer")

    async def _start_timer(
        self,
        entity_id: str | None,
        duration: str | None,
        response: intent.IntentResponse,
        qpl_flow: QPLFlow,
    ):
        point = qpl_flow.mark_subspan_begin("start_timer")

        if duration is None:
            err = "No duration was specified for the timer"
            qpl_flow.mark_canceled(err)
            response.async_set_speech(err)
            return

        # Normalize duration to HH:MM:SS format if needed
        duration = self._normalize_duration(duration)

        service_data = {"duration": duration}
        if entity_id:
            service_data["entity_id"] = entity_id

        maybe(point).annotate("duration", duration)
        maybe(point).annotate("entity_id", entity_id)

        try:
            await self.hass.services.async_call(
                "timer", "start", service_data, blocking=True
            )

            # Record action for undo
            if entity_id:
                self.last_action = TimerAction("start", entity_id, duration)

            qpl_flow.mark_subspan_end("start_timer")

            # Build friendly response
            friendly_duration = self._format_duration_friendly(duration)
            response.async_set_speech(f"Timer set for {friendly_duration}")
        except Exception as e:
            qpl_flow.mark_failed(traceback.format_exc())
            response.async_set_speech("Failed to set timer")

    async def _cancel_timer(
        self,
        entity_id: str | None,
        response: intent.IntentResponse,
        qpl_flow: QPLFlow,
    ):
        point = qpl_flow.mark_subspan_begin("cancel_timer")

        if entity_id is None:
            err = "No timer specified to cancel"
            qpl_flow.mark_canceled(err)
            response.async_set_speech(err)
            return

        # Get remaining time before cancelling so we can restore on undo
        remaining_duration = None
        state = self.hass.states.get(entity_id)
        if state and state.state == "active" and "remaining" in state.attributes:
            remaining_duration = state.attributes["remaining"]

        maybe(point).annotate("entity_id", entity_id)
        await self.hass.services.async_call(
            "timer", "cancel", {"entity_id": entity_id}, blocking=True
        )

        # Record action for undo
        self.last_action = TimerAction("cancel", entity_id, remaining_duration)

        qpl_flow.mark_subspan_end("cancel_timer")
        response.async_set_speech("Timer cancelled")

    async def _pause_timer(
        self,
        entity_id: str | None,
        response: intent.IntentResponse,
        qpl_flow: QPLFlow,
    ):
        point = qpl_flow.mark_subspan_begin("pause_timer")

        if entity_id is None:
            err = "No timer specified to pause"
            qpl_flow.mark_canceled(err)
            response.async_set_speech(err)
            return

        maybe(point).annotate("entity_id", entity_id)
        await self.hass.services.async_call(
            "timer", "pause", {"entity_id": entity_id}, blocking=True
        )

        # Record action for undo
        self.last_action = TimerAction("pause", entity_id)

        qpl_flow.mark_subspan_end("pause_timer")
        response.async_set_speech("Timer paused")

    async def _resume_timer(
        self,
        entity_id: str | None,
        response: intent.IntentResponse,
        qpl_flow: QPLFlow,
    ):
        point = qpl_flow.mark_subspan_begin("resume_timer")

        if entity_id is None:
            err = "No timer specified to resume"
            qpl_flow.mark_canceled(err)
            response.async_set_speech(err)
            return

        maybe(point).annotate("entity_id", entity_id)
        await self.hass.services.async_call(
            "timer", "start", {"entity_id": entity_id}, blocking=True
        )

        # Record action for undo
        self.last_action = TimerAction("resume", entity_id)

        qpl_flow.mark_subspan_end("resume_timer")
        response.async_set_speech("Timer resumed")

    def _normalize_duration(self, duration: str) -> str:
        """Normalize duration to HH:MM:SS format."""
        # If already in HH:MM:SS format, return as is
        if re.match(r"^\d{1,2}:\d{2}:\d{2}$", duration):
            return duration

        # If in MM:SS format, add hours
        if re.match(r"^\d{1,2}:\d{2}$", duration):
            return f"00:{duration}"

        # Try to parse natural language durations like "5 minutes", "1 hour 30 minutes"
        hours = 0
        minutes = 0
        seconds = 0

        hour_match = re.search(r"(\d+)\s*(?:hour|hr|h)", duration, re.IGNORECASE)
        min_match = re.search(
            r"(\d+)\s*(?:minute|min|m)(?!s*\s*sec)", duration, re.IGNORECASE
        )
        sec_match = re.search(r"(\d+)\s*(?:second|sec|s)", duration, re.IGNORECASE)

        if hour_match:
            hours = int(hour_match.group(1))
        if min_match:
            minutes = int(min_match.group(1))
        if sec_match:
            seconds = int(sec_match.group(1))

        # If we found any time components, format them
        if hours or minutes or seconds:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        # If it's just a number, assume minutes
        if duration.isdigit():
            return f"00:{int(duration):02d}:00"

        # Return as-is and let Home Assistant handle it
        return duration

    def _format_duration_friendly(self, duration: str) -> str:
        """Convert HH:MM:SS to a friendly string."""
        parts = duration.split(":")
        if len(parts) != 3:
            return duration

        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = int(parts[2])

        components = []
        if hours > 0:
            components.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes > 0:
            components.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
        if seconds > 0:
            components.append(f"{seconds} second{'s' if seconds != 1 else ''}")

        if not components:
            return "0 seconds"

        if len(components) == 1:
            return components[0]
        elif len(components) == 2:
            return f"{components[0]} and {components[1]}"
        else:
            return f"{components[0]}, {components[1]}, and {components[2]}"

    async def undo(self, response: intent.IntentResponse, qpl_flow: QPLFlow):
        point = qpl_flow.mark_subspan_begin("timers_undo")

        if self.last_action is None:
            maybe(point).annotate("no timer action to undo")
            response.async_set_speech("No timer action to undo")
            qpl_flow.mark_subspan_end("timers_undo")
            return

        action = self.last_action
        maybe(point).annotate("original_action", action.action)
        maybe(point).annotate("entity_id", action.entity_id)

        if action.action == "start":
            # Undo start by cancelling the timer
            await self.hass.services.async_call(
                "timer", "cancel", {"entity_id": action.entity_id}, blocking=True
            )
            response.async_set_speech("Timer cancelled")

        elif action.action == "cancel":
            # Undo cancel by restarting with the saved duration
            if action.duration:
                await self.hass.services.async_call(
                    "timer",
                    "start",
                    {"entity_id": action.entity_id, "duration": action.duration},
                    blocking=True,
                )
                response.async_set_speech("Timer restored")
            else:
                response.async_set_speech("Cannot restore timer - duration unknown")

        elif action.action == "pause":
            # Undo pause by resuming
            await self.hass.services.async_call(
                "timer", "start", {"entity_id": action.entity_id}, blocking=True
            )
            response.async_set_speech("Timer resumed")

        elif action.action == "resume":
            # Undo resume by pausing
            await self.hass.services.async_call(
                "timer", "pause", {"entity_id": action.entity_id}, blocking=True
            )
            response.async_set_speech("Timer paused")

        self.last_action = None
        qpl_flow.mark_subspan_end("timers_undo")

    async def _build_prompt(self, request: ConversationInput, qpl_flow: QPLFlow) -> str:
        entities = []

        qpl_flow.mark_subspan_begin("build_prompt")
        qpl_flow.mark_subspan_begin("querying_entities_from_ha")

        for state in self.hass.states.async_all():
            if not async_should_expose(self.hass, conversation.DOMAIN, state.entity_id):
                continue
            if not state.entity_id.startswith("timer."):
                continue

            entry = {
                "entity_id": state.entity_id,
                "friendly_name": state.name,
                "state": state.state,
            }
            # Include remaining time if active
            if state.state == "active" and "remaining" in state.attributes:
                entry["remaining"] = state.attributes["remaining"]

            entities.append(entry)

        point = qpl_flow.mark_subspan_end("querying_entities_from_ha")
        timer_list = json.dumps(entities)
        maybe(point).annotate("timer_list", timer_list)

        qpl_flow.mark_subspan_begin("render_prompt")
        prompt_key = os.path.join(os.path.dirname(__file__), "timers.md")
        prompt_template = await self.prompt_cache.get(prompt_key)
        template = Template(prompt_template, trim_blocks=True)

        output = template.render(
            timer_list=timer_list,
            user_prompt=request.text,
        )
        point = qpl_flow.mark_subspan_end("render_prompt")
        maybe(point).annotate("prompt", output)
        qpl_flow.mark_subspan_end("build_prompt")
        return output
