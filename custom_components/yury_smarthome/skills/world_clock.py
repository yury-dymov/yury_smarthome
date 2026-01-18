from .abstract_skill import AbstractSkill
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from jinja2 import Template
from homeassistant.helpers import intent
from homeassistant.components.conversation import ConversationInput
from custom_components.yury_smarthome.qpl import QPLFlow
from custom_components.yury_smarthome.maybe import maybe
import traceback


class WorldClock(AbstractSkill):
    def name(self) -> str:
        return "World Clock"

    async def process_user_request(
        self,
        request: ConversationInput,
        response: intent.IntentResponse,
        qpl_flow: QPLFlow,
    ):
        prompt = await self._build_prompt(request, qpl_flow)
        qpl_flow.mark_subspan_begin("sending_message_to_llm")
        llm_response = await self.client.send_message(prompt)
        point = qpl_flow.mark_subspan_end("sending_message_to_llm")
        llm_response = llm_response.replace("```json", "")
        llm_response = llm_response.replace("```", "")
        maybe(point).annotate("llm_response", llm_response)

        try:
            json_data = json.loads(llm_response)
            timezone_str = json_data.get("timezone")
            location = json_data.get("location", "the requested location")

            if timezone_str is None:
                err = "Could not determine the timezone for the requested location"
                qpl_flow.mark_failed(err)
                response.async_set_speech(err)
                return

            qpl_flow.mark_subspan_begin("getting_time")
            try:
                tz = ZoneInfo(timezone_str)
                current_time = datetime.now(tz)
                formatted_time = current_time.strftime("%I:%M %p")
            except Exception as e:
                err = f"Invalid timezone: {timezone_str}"
                qpl_flow.mark_failed(err)
                response.async_set_speech(err)
                return
            point = qpl_flow.mark_subspan_end("getting_time")
            maybe(point).annotate("timezone", timezone_str)
            maybe(point).annotate("time", formatted_time)

            answer = f"It's {formatted_time} in {location}"
            response.async_set_speech(answer)

        except json.JSONDecodeError as e:
            qpl_flow.mark_failed(e.msg)
            response.async_set_speech("Failed to process the request")
        except Exception:
            qpl_flow.mark_failed(traceback.format_exc())
            response.async_set_speech("Failed")

    async def undo(self, response: intent.IntentResponse, qpl_flow: QPLFlow):
        # World clock is read-only, nothing to undo
        response.async_set_speech("Nothing to undo for time queries")

    async def _build_prompt(self, request: ConversationInput, qpl_flow: QPLFlow) -> str:
        qpl_flow.mark_subspan_begin("build_prompt")
        prompt_key = os.path.join(os.path.dirname(__file__), "world_clock.md")
        prompt_template = await self.prompt_cache.get(prompt_key)
        template = Template(prompt_template, trim_blocks=True)

        output = template.render(user_prompt=request.text)
        point = qpl_flow.mark_subspan_end("build_prompt")
        maybe(point).annotate("prompt", output)
        return output
