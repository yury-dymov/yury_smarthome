from .abstract_skill import AbstractSkill
import os
from jinja2 import Template
from homeassistant.helpers import intent
from homeassistant.components.conversation import ConversationInput
from custom_components.yury_smarthome.qpl import QPLFlow
from custom_components.yury_smarthome.maybe import maybe


class Other(AbstractSkill):
    def name(self) -> str:
        return "Other"

    async def process_user_request(
        self,
        request: ConversationInput,
        response: intent.IntentResponse,
        qpl_flow: QPLFlow,
    ):
        try:
            # Build the prompt
            prompt = await self._build_prompt(request, qpl_flow)

            # Send to LLM
            point = qpl_flow.mark_subspan_begin("sending_prompt_to_llm")
            maybe(point).annotate("prompt", prompt)
            llm_response = await self.client.send_message(prompt)
            point = qpl_flow.mark_subspan_end("sending_prompt_to_llm")
            maybe(point).annotate("llm_response", llm_response)

            # Return the response
            response.async_set_speech(llm_response.strip())

        except Exception as e:
            qpl_flow.mark_failed(str(e))
            response.async_set_speech("Sorry, I couldn't answer that question")

    async def _build_prompt(
        self, request: ConversationInput, qpl_flow: QPLFlow
    ) -> str:
        """Build prompt for the general question."""
        qpl_flow.mark_subspan_begin("build_prompt")

        prompt_key = os.path.join(os.path.dirname(__file__), "other.md")
        prompt_template = await self.prompt_cache.get(prompt_key)
        template = Template(prompt_template, trim_blocks=True)

        output = template.render(user_prompt=request.text)
        point = qpl_flow.mark_subspan_end("build_prompt")
        maybe(point).annotate("prompt", output)
        return output

    async def undo(self, response: intent.IntentResponse, qpl_flow: QPLFlow):
        # Nothing to undo for general questions
        response.async_set_speech("Nothing to undo")
