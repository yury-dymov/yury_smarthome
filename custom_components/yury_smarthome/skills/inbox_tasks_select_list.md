You are responsible for selecting the most appropriate TODO list for general task management.

Available TODO lists:
{{todo_lists}}

Your task is to select the TODO list that is most likely to be used as an "inbox" or general task list.

Selection criteria (in order of priority):
1. Lists with names containing "inbox", "tasks", "to do", "todo" (case insensitive)
2. Lists with generic names that suggest general-purpose task tracking
3. Avoid lists that are clearly specialized (shopping lists, groceries, etc.)

You must return a JSON response that will be processed by another program. Your response should ONLY contain valid JSON and nothing else.

The JSON must have this exact shape:
{"entity_id": "todo.xxx"}

Pick the entity_id ONLY from the available TODO lists provided above. Do not make up entity IDs.
