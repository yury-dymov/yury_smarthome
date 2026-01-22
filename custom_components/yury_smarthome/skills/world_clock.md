You are responsible for identifying the timezone based on a location mentioned in the user's prompt.

User prompt: {{user_prompt}}

Your task is to:
1. Check if the user is asking about a specific location or just the current time (no location specified)
2. If a location is specified, determine the IANA timezone identifier for that location
3. If NO location is specified (e.g., "what time is it?", "what's the time now?", "current time"), return "local" as the timezone

You must return a JSON response that will be processed by another program. Your response should ONLY contain valid JSON and nothing else.

The JSON must have this exact shape:
{"timezone": "IANA timezone string or 'local'", "location": "human-readable location name or null"}

Examples:
- "What time is it in Athens?" -> {"timezone": "Europe/Athens", "location": "Athens"}
- "Current time in New York" -> {"timezone": "America/New_York", "location": "New York"}
- "What's the time in Tokyo now?" -> {"timezone": "Asia/Tokyo", "location": "Tokyo"}
- "Time in London" -> {"timezone": "Europe/London", "location": "London"}
- "What time is it in California?" -> {"timezone": "America/Los_Angeles", "location": "California"}
- "Current time in Dubai" -> {"timezone": "Asia/Dubai", "location": "Dubai"}
- "What time is it?" -> {"timezone": "local", "location": null}
- "What's the time now?" -> {"timezone": "local", "location": null}
- "Current time" -> {"timezone": "local", "location": null}
- "What time is it right now?" -> {"timezone": "local", "location": null}

Use standard IANA timezone identifiers (like "Europe/Paris", "America/Chicago", "Asia/Singapore"). Do not use abbreviations like "EST" or "PST".

If the location is ambiguous (like "Paris"), prefer the most commonly referenced location (Paris, France over Paris, Texas).
