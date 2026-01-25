from .abstract_skill import AbstractSkill
import json
import os
from dataclasses import dataclass
from jinja2 import Template
from homeassistant.helpers import intent
from homeassistant.components.conversation import ConversationInput
from homeassistant.components.todo.intent import (
    INTENT_LIST_ADD_ITEM,
    INTENT_LIST_COMPLETE_ITEM,
)
from custom_components.yury_smarthome.qpl import QPLFlow
from custom_components.yury_smarthome.maybe import maybe
import traceback


@dataclass
class ExecutedAction:
    intent_item: intent.Intent
    action: str  # "add" or "complete"
    task: str


class InboxTasks(AbstractSkill):
    executed_actions: list[ExecutedAction]
    last_entity_id: str | None

    def __init__(self, hass, client, prompt_cache):
        super().__init__(hass, client, prompt_cache)
        self.executed_actions = []
        self.last_entity_id = None

    def name(self) -> str:
        return "TODO Tasks"

    async def process_user_request(
        self,
        request: ConversationInput,
        response: intent.IntentResponse,
        qpl_flow: QPLFlow,
    ):
        self.executed_actions = []
        self.last_entity_id = None

        try:
            # Step 1: Select the inbox-like TODO list
            entity_id = await self._select_todo_list(qpl_flow)
            if entity_id is None:
                err = "No TODO list was found"
                qpl_flow.mark_failed(err)
                response.async_set_speech(err)
                return

            self.last_entity_id = entity_id

            # Step 2: Query tasks from the selected list
            existing_tasks = self._get_tasks_from_list(entity_id)

            # Step 3: Determine actions and tasks
            action_prompt = await self._build_action_prompt(
                request, existing_tasks, qpl_flow
            )
            point = qpl_flow.mark_subspan_begin("sending_action_prompt_to_llm")
            maybe(point).annotate("prompt", action_prompt)
            llm_response = await self.client.send_message(action_prompt)
            point = qpl_flow.mark_subspan_end("sending_action_prompt_to_llm")
            llm_response = llm_response.replace("```json", "")
            llm_response = llm_response.replace("```", "")
            maybe(point).annotate("llm_response", llm_response)

            json_data = json.loads(llm_response)
            actions = json_data.get("actions", [])

            if not actions:
                err = "No actions were identified"
                qpl_flow.mark_failed(err)
                response.async_set_speech(err)
                return

            # Process each action
            added_tasks = []
            completed_tasks = []
            no_match_tasks = []

            for action_item in actions:
                action = action_item.get("action")
                task = action_item.get("task")

                if action == "no_match":
                    no_match_tasks.append(task)
                    continue

                if action not in {"add", "complete"}:
                    continue

                if not task or not task.strip():
                    continue

                if action == "add":
                    action_intent = INTENT_LIST_ADD_ITEM
                else:
                    action_intent = INTENT_LIST_COMPLETE_ITEM

                intent_item = intent.Intent(
                    self.hass,
                    "yury",
                    action_intent,
                    {"name": {"value": entity_id}, "item": {"value": task}},
                    None,
                    intent.Context(),
                    request.language,
                )

                point = qpl_flow.mark_subspan_begin("sending_intent")
                maybe(point).annotate("action", action_intent)
                maybe(point).annotate("task", task)
                maybe(point).annotate("entity_id", entity_id)
                handler = self.hass.data.get(intent.DATA_KEY, {}).get(action_intent)
                await handler.async_handle(intent_item)
                qpl_flow.mark_subspan_end("sending_intent")

                self.executed_actions.append(
                    ExecutedAction(intent_item=intent_item, action=action, task=task)
                )

                if action == "add":
                    added_tasks.append(task)
                else:
                    completed_tasks.append(task)

            # Build response message
            qpl_flow.mark_subspan_begin("building_response")
            answer = self._build_response_message(
                added_tasks, completed_tasks, no_match_tasks
            )
            point = qpl_flow.mark_subspan_end("building_response")
            maybe(point).annotate("response", answer)
            response.async_set_speech(answer)

        except json.JSONDecodeError as e:
            qpl_flow.mark_failed(e.msg)
            response.async_set_speech("Failed to process the request")
        except Exception:
            qpl_flow.mark_failed(traceback.format_exc())
            response.async_set_speech("Failed")

    def _build_response_message(
        self,
        added_tasks: list[str],
        completed_tasks: list[str],
        no_match_tasks: list[str],
    ) -> str:
        """Build a human-readable response message."""
        parts = []

        if added_tasks:
            if len(added_tasks) == 1:
                parts.append(f"{added_tasks[0]} was added")
            else:
                parts.append(f"{', '.join(added_tasks)} were added")

        if completed_tasks:
            if len(completed_tasks) == 1:
                parts.append(f"{completed_tasks[0]} was marked completed")
            else:
                parts.append(f"{', '.join(completed_tasks)} were marked completed")

        if no_match_tasks:
            if len(no_match_tasks) == 1:
                parts.append(f"could not find a match for '{no_match_tasks[0]}'")
            else:
                parts.append(
                    f"could not find matches for: {', '.join(no_match_tasks)}"
                )

        if not parts:
            return "No actions were performed"

        return "; ".join(parts)

    async def _select_todo_list(self, qpl_flow: QPLFlow) -> str | None:
        """Step 1: Ask LLM to select the most inbox-like TODO list."""
        qpl_flow.mark_subspan_begin("select_todo_list")

        # Query all todo lists (just entity_id and friendly_name)
        qpl_flow.mark_subspan_begin("querying_todo_entities")
        todo_lists = []
        for state in self.hass.states.async_all():
            if not state.entity_id.startswith("todo."):
                continue
            todo_lists.append({
                "entity_id": state.entity_id,
                "friendly_name": state.name,
            })
        point = qpl_flow.mark_subspan_end("querying_todo_entities")
        todo_lists_json = json.dumps(todo_lists)
        maybe(point).annotate("todo_lists", todo_lists_json)

        if not todo_lists:
            qpl_flow.mark_subspan_end("select_todo_list")
            return None

        # Build and send prompt to select list
        qpl_flow.mark_subspan_begin("render_select_list_prompt")
        prompt_key = os.path.join(
            os.path.dirname(__file__), "inbox_tasks_select_list.md"
        )
        prompt_template = await self.prompt_cache.get(prompt_key)
        template = Template(prompt_template, trim_blocks=True)
        prompt = template.render(todo_lists=todo_lists_json)
        point = qpl_flow.mark_subspan_end("render_select_list_prompt")
        maybe(point).annotate("prompt", prompt)

        qpl_flow.mark_subspan_begin("sending_select_list_to_llm")
        llm_response = await self.client.send_message(prompt)
        point = qpl_flow.mark_subspan_end("sending_select_list_to_llm")
        llm_response = llm_response.replace("```json", "")
        llm_response = llm_response.replace("```", "")
        maybe(point).annotate("llm_response", llm_response)

        try:
            json_data = json.loads(llm_response)
            entity_id = json_data.get("entity_id")
            point = qpl_flow.mark_subspan_end("select_todo_list")
            maybe(point).annotate("selected_entity_id", entity_id)
            return entity_id
        except json.JSONDecodeError:
            qpl_flow.mark_subspan_end("select_todo_list")
            return None

    def _get_tasks_from_list(self, entity_id: str) -> list[str]:
        """Step 2: Get existing tasks from the selected list."""
        existing_tasks = []
        state = self.hass.states.get(entity_id)
        if state:
            for item in state.attributes.get("items", []):
                if item.get("status") != "completed":
                    existing_tasks.append(item.get("summary", ""))
        return existing_tasks

    async def _build_action_prompt(
        self, request: ConversationInput, existing_tasks: list[str], qpl_flow: QPLFlow
    ) -> str:
        """Step 3: Build prompt for action/task identification."""
        qpl_flow.mark_subspan_begin("build_action_prompt")

        prompt_key = os.path.join(os.path.dirname(__file__), "inbox_tasks.md")
        prompt_template = await self.prompt_cache.get(prompt_key, request.conversation_id)
        template = Template(prompt_template, trim_blocks=True)

        tasks_json = json.dumps(existing_tasks)
        output = template.render(
            existing_tasks=tasks_json,
            user_prompt=request.text,
        )
        point = qpl_flow.mark_subspan_end("build_action_prompt")
        maybe(point).annotate("prompt", output)
        maybe(point).annotate("existing_tasks", tasks_json)
        return output

    async def undo(self, response: intent.IntentResponse, qpl_flow: QPLFlow):
        point = qpl_flow.mark_subspan_begin("inbox_tasks_undo")

        if not self.executed_actions:
            maybe(point).annotate("status", "no actions to undo")
            response.async_set_speech("Nothing to undo")
            qpl_flow.mark_subspan_end("inbox_tasks_undo")
            return

        undone_added = []
        undone_completed = []

        for executed in self.executed_actions:
            if executed.action == "add":
                undo_intent_type = INTENT_LIST_COMPLETE_ITEM
            else:
                undo_intent_type = INTENT_LIST_ADD_ITEM

            executed.intent_item.intent_type = undo_intent_type

            point = qpl_flow.mark_subspan_begin("undo_action")
            handler = self.hass.data.get(intent.DATA_KEY, {}).get(undo_intent_type)
            maybe(point).annotate("intent_type", undo_intent_type)
            maybe(point).annotate("task", executed.task)
            undo_response = await handler.async_handle(executed.intent_item)
            point = qpl_flow.mark_subspan_end("undo_action")
            maybe(point).annotate("result", undo_response.response_type.value)

            if executed.action == "add":
                undone_added.append(executed.task)
            else:
                undone_completed.append(executed.task)

        # Build undo response message
        parts = []
        if undone_added:
            if len(undone_added) == 1:
                parts.append(f"{undone_added[0]} was removed")
            else:
                parts.append(f"{', '.join(undone_added)} were removed")

        if undone_completed:
            if len(undone_completed) == 1:
                parts.append(f"{undone_completed[0]} was restored")
            else:
                parts.append(f"{', '.join(undone_completed)} were restored")

        response.async_set_speech("; ".join(parts) if parts else "Undo completed")

        self.executed_actions = []
        self.last_entity_id = None
        qpl_flow.mark_subspan_end("inbox_tasks_undo")
