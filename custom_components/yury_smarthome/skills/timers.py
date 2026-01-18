from .abstract_skill import AbstractSkill
from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.helpers import intent, entity_registry, device_registry
from homeassistant.components.conversation import ConversationInput
from custom_components.yury_smarthome.entity import LocalLLMEntity
from custom_components.yury_smarthome.prompt_cache import PromptCache
from custom_components.yury_smarthome.qpl import QPL, QPLFlow
from custom_components.yury_smarthome.maybe import maybe
from dataclasses import dataclass
from jinja2 import Template
import json
import logging
import os
import re
import traceback


_LOGGER = logging.getLogger(__name__)


@dataclass
class TimerAction:
    action: str  # "start", "cancel", "pause", "resume"
    entity_id: str
    duration: str | None = None


@dataclass
class TrackedTimer:
    entity_id: str
    device_id: str | None
    conversation_id: str | None
    friendly_name: str | None  # Context/label from user request (e.g., "egg timer", "laundry")


class Timers(AbstractSkill):
    last_actions: list[TimerAction]
    qpl_provider: QPL
    # Track timers we started: entity_id -> TrackedTimer (class-level, shared)
    _tracked_timers: dict[str, TrackedTimer] = {}
    _listener_registered: bool = False

    def __init__(
        self,
        hass: HomeAssistant,
        client: LocalLLMEntity,
        prompt_cache: PromptCache,
        qpl_provider: QPL,
    ):
        super().__init__(hass, client, prompt_cache)
        self.qpl_provider = qpl_provider
        self.last_actions = []  # Instance variable, not shared
        self._register_timer_listener()

    def name(self) -> str:
        return "Timers"

    def _register_timer_listener(self):
        """Register the timer finished event listener."""

        @callback
        def _handle_timer_state_change(event: Event):
            """Handle timer state changes to detect when our timers finish."""
            entity_id = event.data.get("entity_id", "")
            if not entity_id.startswith("timer."):
                return

            new_state = event.data.get("new_state")
            old_state = event.data.get("old_state")

            if old_state is None or new_state is None:
                return

            # Check if timer just finished (went from active to idle)
            if old_state.state == "active" and new_state.state == "idle":
                self._on_timer_finished(entity_id)

        self.hass.bus.async_listen(EVENT_STATE_CHANGED, _handle_timer_state_change)
        Timers._listener_registered = True
        _LOGGER.debug("Timer state change listener registered")

    def _get_available_timer(self) -> str | None:
        """Find an available (idle) timer from the pool of exposed timers."""
        for state in self.hass.states.async_all():
            if not state.entity_id.startswith("timer."):
                continue
            # Timer is available if it's idle and not tracked by us
            if state.state == "idle" and state.entity_id not in Timers._tracked_timers:
                return state.entity_id
        return None

    def _on_timer_finished(self, entity_id: str):
        """Called when a timer finishes. Notify user if it's one we started."""
        tracked = Timers._tracked_timers.get(entity_id)
        if tracked is None:
            _LOGGER.debug(f"Timer {entity_id} finished but was not tracked by us")
            return

        # Create QPL flow for this timer finished event
        qpl_flow = self.qpl_provider.create_flow("timer_finished")
        point = qpl_flow.mark_subspan_begin("on_timer_finished")
        maybe(point).annotate("entity_id", entity_id)
        maybe(point).annotate("device_id", tracked.device_id)
        _LOGGER.info(
            f"Our timer {entity_id} finished, notifying user"
        )

        # Remove from tracked timers
        del Timers._tracked_timers[entity_id]

        # Use TTS to notify on the device that started the timer
        try:
            self.hass.async_create_task(self._notify_timer_finished(tracked, qpl_flow))
            qpl_flow.mark_subspan_end("on_timer_finished")
        except Exception:
            qpl_flow.mark_failed(traceback.format_exc())

    async def _notify_timer_finished(self, tracked: TrackedTimer, qpl_flow: QPLFlow):
        """Notify the user via TTS that their timer has finished."""
        if tracked.friendly_name is not None:
            message = f"{tracked.friendly_name} timer finished"
        else:
            message = "Timer finished"
        point = qpl_flow.mark_subspan_begin("notify_timer_finished")
        if tracked.conversation_id is not None:
            qpl_flow.annotate("conversation_id", tracked.conversation_id)
        maybe(point).annotate("message", message)

        notification_sent = False

        # Try to use TTS on the device that started the timer
        if tracked.device_id:
            qpl_flow.mark_subspan_begin("find_tts_target")
            target = await self._get_tts_target_for_device(tracked.device_id)
            point = qpl_flow.mark_subspan_end("find_tts_target")

            if target:
                tts_engine = "tts.piper"
                maybe(point).annotate("tts_target", target)
                maybe(point).annotate("tts_engine", tts_engine)
                qpl_flow.mark_subspan_begin("send_tts")
                try:
                    await self.hass.services.async_call(
                        "tts",
                        "speak",
                        {
                            "entity_id": tts_engine,
                            "media_player_entity_id": target,
                            "message": message,
                        },
                        blocking=False,
                    )
                    qpl_flow.mark_subspan_end("send_tts")
                    notification_sent = True
                    _LOGGER.info(f"TTS notification sent to {target}: {message}")
                except Exception:
                    qpl_flow.mark_subspan_end("send_tts")
                    qpl_flow.mark_failed(traceback.format_exc())
                    return
            else:
                qpl_flow.mark_subspan_end("notify_timer_finished")
                qpl_flow.mark_failed("no target found to send tts")
                return
        else:
            qpl_flow.mark_subspan_end("notify_timer_finished")
            qpl_flow.mark_failed("no device found to send tts")
            return

        # Finalize QPL flow
        qpl_flow.mark_subspan_end("notify_timer_finished")
        if notification_sent:
            qpl_flow.mark_success()
        else:
            qpl_flow.mark_failed("no notification sent")

    async def _get_tts_target_for_device(self, device_id: str) -> str | None:
        """Find a media_player entity associated with the given device for TTS."""
        er = entity_registry.async_get(self.hass)
        dr = device_registry.async_get(self.hass)

        device = dr.async_get(device_id)
        if device is None:
            return None

        # Look for media_player entities in the same area or on the same device
        for entity in er.entities.values():
            if not entity.entity_id.startswith("media_player."):
                continue

            # Check if entity is on the same device
            if entity.device_id == device_id:
                return entity.entity_id

            # Check if entity is in the same area
            if device.area_id and entity.area_id == device.area_id:
                return entity.entity_id

        return None

    async def process_user_request(
        self,
        request: ConversationInput,
        response: intent.IntentResponse,
        qpl_flow: QPLFlow,
    ):
        self.last_actions = []
        prompt = await self._build_prompt(request, qpl_flow)
        qpl_flow.mark_subspan_begin("sending_message_to_llm")
        llm_response = await self.client.send_message(prompt)
        point = qpl_flow.mark_subspan_end("sending_message_to_llm")
        llm_response = llm_response.replace("```json", "")
        llm_response = llm_response.replace("```", "")
        maybe(point).annotate("llm_response", llm_response)

        try:
            json_data = json.loads(llm_response)

            # Handle both single object and array of objects
            if isinstance(json_data, dict):
                commands = [json_data]
            elif isinstance(json_data, list):
                commands = json_data
            else:
                err = "Invalid response format"
                qpl_flow.mark_canceled(err)
                response.async_set_speech(err)
                return

            messages = []
            for cmd in commands:
                action = cmd.get("action")
                if action is None or action not in {"start", "cancel", "pause", "resume"}:
                    messages.append("Invalid action")
                    continue

                entity_id = cmd.get("entity_id")
                duration = cmd.get("duration")
                context = cmd.get("context")
                if context in {"", "timer"}:
                    context = None

                result = None
                if action == "start":
                    result = await self._start_timer(
                        entity_id, duration, context, request, qpl_flow
                    )
                elif action == "cancel":
                    result = await self._cancel_timer(entity_id, qpl_flow)
                elif action == "pause":
                    result = await self._pause_timer(entity_id, qpl_flow)
                elif action == "resume":
                    result = await self._resume_timer(entity_id, qpl_flow)

                if result:
                    messages.append(result)

            # Combine all messages into a single response
            if messages:
                response.async_set_speech(". ".join(messages))
            else:
                response.async_set_speech("No timer actions performed")

        except json.JSONDecodeError as err:
            qpl_flow.mark_failed(err.msg)
            response.async_set_speech("Failed to understand timer request")
        except Exception:
            qpl_flow.mark_failed(traceback.format_exc())
            response.async_set_speech("Failed to set timer")

    async def _start_timer(
        self,
        entity_id: str | None,
        duration: str | None,
        context: str | None,
        request: ConversationInput,
        qpl_flow: QPLFlow,
    ) -> str | None:
        point = qpl_flow.mark_subspan_begin("start_timer")

        if duration is None:
            err = "No duration was specified for the timer"
            qpl_flow.mark_canceled(err)
            return err

        # If no entity_id provided or the specified one is busy, find an available timer
        if entity_id is None or entity_id in Timers._tracked_timers:
            available_timer = self._get_available_timer()
            if available_timer is None:
                err = "No available timers. All timers are currently in use."
                qpl_flow.mark_canceled(err)
                return err
            entity_id = available_timer

        # Normalize duration to HH:MM:SS format if needed
        duration = self._normalize_duration(duration)

        service_data = {"duration": duration, "entity_id": entity_id}

        maybe(point).annotate("duration", duration)
        maybe(point).annotate("entity_id", entity_id)
        maybe(point).annotate("context", context if context else "default")

        try:
            await self.hass.services.async_call(
                "timer", "start", service_data, blocking=True
            )

            # Record action for undo
            self.last_actions.append(TimerAction("start", entity_id, duration))

            # Use context as friendly name, fall back to timer state name
            friendly_name = context if context else None

            Timers._tracked_timers[entity_id] = TrackedTimer(
                entity_id=entity_id,
                device_id=request.device_id,
                friendly_name=friendly_name,
                conversation_id=request.conversation_id,
            )
            _LOGGER.debug(
                f"Tracking timer {entity_id} from device {request.device_id}"
            )

            qpl_flow.mark_subspan_end("start_timer")

            # Build friendly response
            friendly_duration = self._format_duration_friendly(duration)
            if context:
                return f"{context} timer set for {friendly_duration}"
            else:
                return f"Timer set for {friendly_duration}"
        except Exception:
            qpl_flow.mark_failed(traceback.format_exc())
            return "Failed to set timer"

    async def _cancel_timer(
        self,
        entity_id: str | None,
        qpl_flow: QPLFlow,
    ) -> str | None:
        point = qpl_flow.mark_subspan_begin("cancel_timer")

        if entity_id is None:
            err = "No timer specified to cancel"
            qpl_flow.mark_canceled(err)
            return err

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
        self.last_actions.append(TimerAction("cancel", entity_id, remaining_duration))

        # Remove from tracked timers
        if entity_id in Timers._tracked_timers:
            del Timers._tracked_timers[entity_id]
            _LOGGER.debug(f"Stopped tracking cancelled timer {entity_id}")

        qpl_flow.mark_subspan_end("cancel_timer")
        return "Timer cancelled"

    async def _pause_timer(
        self,
        entity_id: str | None,
        qpl_flow: QPLFlow,
    ) -> str | None:
        point = qpl_flow.mark_subspan_begin("pause_timer")

        if entity_id is None:
            err = "No timer specified to pause"
            qpl_flow.mark_canceled(err)
            return err

        maybe(point).annotate("entity_id", entity_id)
        await self.hass.services.async_call(
            "timer", "pause", {"entity_id": entity_id}, blocking=True
        )

        # Record action for undo
        self.last_actions.append(TimerAction("pause", entity_id))

        qpl_flow.mark_subspan_end("pause_timer")
        return "Timer paused"

    async def _resume_timer(
        self,
        entity_id: str | None,
        qpl_flow: QPLFlow,
    ) -> str | None:
        point = qpl_flow.mark_subspan_begin("resume_timer")

        if entity_id is None:
            err = "No timer specified to resume"
            qpl_flow.mark_canceled(err)
            return err

        maybe(point).annotate("entity_id", entity_id)
        await self.hass.services.async_call(
            "timer", "start", {"entity_id": entity_id}, blocking=True
        )

        # Record action for undo
        self.last_actions.append(TimerAction("resume", entity_id))

        qpl_flow.mark_subspan_end("resume_timer")
        return "Timer resumed"

    def _normalize_duration(self, duration: str) -> str:
        """Normalize duration to HH:MM:SS format."""
        # If already in HH:MM:SS format, return as is
        if re.match(r"^\d{1,2}:\d{2}:\d{2}$", duration):
            return duration

        # If in MM:SS format, add hours
        if re.match(r"^\d{1,2}:\d{2}$", duration):
            return f"00:{duration}"

        # Try to parse natural language durations like "5 minutes", "1.5 hours"
        total_seconds = 0.0

        # Match decimal numbers for hours, minutes, seconds
        hour_match = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:hour|hr|h)", duration, re.IGNORECASE
        )
        min_match = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:minute|min|m)(?!s*\s*sec)", duration, re.IGNORECASE
        )
        sec_match = re.search(
            r"(\d+(?:\.\d+)?)\s*(?:second|sec|s)", duration, re.IGNORECASE
        )

        if hour_match:
            total_seconds += float(hour_match.group(1)) * 3600
        if min_match:
            total_seconds += float(min_match.group(1)) * 60
        if sec_match:
            total_seconds += float(sec_match.group(1))

        # If we found any time components, convert to HH:MM:SS
        if total_seconds > 0:
            total_seconds = round(total_seconds)
            hours = int(total_seconds // 3600)
            minutes = int((total_seconds % 3600) // 60)
            seconds = int(total_seconds % 60)
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        # If it's just a number (integer or decimal), assume minutes
        number_match = re.match(r"^(\d+(?:\.\d+)?)$", duration.strip())
        if number_match:
            total_seconds = round(float(number_match.group(1)) * 60)
            hours = int(total_seconds // 3600)
            minutes = int((total_seconds % 3600) // 60)
            seconds = int(total_seconds % 60)
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

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

        if not self.last_actions:
            maybe(point).annotate("no timer action to undo")
            response.async_set_speech("No timer action to undo")
            qpl_flow.mark_subspan_end("timers_undo")
            return

        messages = []
        # Undo actions in reverse order
        for action in reversed(self.last_actions):
            maybe(point).annotate("original_action", action.action)
            maybe(point).annotate("entity_id", action.entity_id)

            if action.action == "start":
                # Undo start by cancelling the timer
                await self.hass.services.async_call(
                    "timer", "cancel", {"entity_id": action.entity_id}, blocking=True
                )
                # Remove from tracked timers
                if action.entity_id in Timers._tracked_timers:
                    del Timers._tracked_timers[action.entity_id]
                messages.append("Timer cancelled")

            elif action.action == "cancel":
                # Undo cancel by restarting with the saved duration
                if action.duration:
                    await self.hass.services.async_call(
                        "timer",
                        "start",
                        {"entity_id": action.entity_id, "duration": action.duration},
                        blocking=True,
                    )
                    messages.append("Timer restored")
                else:
                    messages.append("Cannot restore timer - duration unknown")

            elif action.action == "pause":
                # Undo pause by resuming
                await self.hass.services.async_call(
                    "timer", "start", {"entity_id": action.entity_id}, blocking=True
                )
                messages.append("Timer resumed")

            elif action.action == "resume":
                # Undo resume by pausing
                await self.hass.services.async_call(
                    "timer", "pause", {"entity_id": action.entity_id}, blocking=True
                )
                messages.append("Timer paused")

        self.last_actions = []
        qpl_flow.mark_subspan_end("timers_undo")

        if messages:
            response.async_set_speech(". ".join(messages))

    async def _build_prompt(self, request: ConversationInput, qpl_flow: QPLFlow) -> str:
        entities = []

        qpl_flow.mark_subspan_begin("build_prompt")
        qpl_flow.mark_subspan_begin("querying_entities_from_ha")

        for state in self.hass.states.async_all():
            if not state.entity_id.startswith("timer."):
                continue

            entry = {
                "entity_id": state.entity_id,
                "friendly_name": state.name,
                "state": state.state,
            }

            # If this timer is tracked by us, include the context
            if state.entity_id in Timers._tracked_timers:
                entry["context"] = Timers._tracked_timers[state.entity_id].friendly_name

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
