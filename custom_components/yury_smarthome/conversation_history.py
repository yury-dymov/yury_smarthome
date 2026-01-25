from dataclasses import dataclass, field
from datetime import datetime
from typing import List


@dataclass
class ConversationExchange:
    """A single exchange in a conversation (user prompt + assistant response)."""
    timestamp: datetime
    user_prompt: str
    assistant_response: str


@dataclass
class ConversationContext:
    """Holds the conversation history for a single conversation_id."""
    exchanges: List[ConversationExchange] = field(default_factory=list)
    max_exchanges: int = 10  # Keep last N exchanges to avoid token bloat

    def add_exchange(self, user_prompt: str, assistant_response: str):
        """Add a new exchange to the history."""
        exchange = ConversationExchange(
            timestamp=datetime.now(),
            user_prompt=user_prompt,
            assistant_response=assistant_response,
        )
        self.exchanges.append(exchange)
        # Trim to max size
        if len(self.exchanges) > self.max_exchanges:
            self.exchanges = self.exchanges[-self.max_exchanges:]

    def format_for_prompt(self) -> str:
        """Format the conversation history as a string for LLM context."""
        if not self.exchanges:
            return ""

        lines = [
            "## Conversation History (context only - DO NOT execute these, only process the current user prompt above)",
            "The following shows what happened earlier in this conversation for context:",
            ""
        ]
        for i, exchange in enumerate(self.exchanges, 1):
            lines.append(f"[{i}] User said: \"{exchange.user_prompt}\" → Result: \"{exchange.assistant_response}\"")

        return "\n".join(lines)


class ConversationHistoryCache:
    """Caches conversation history per conversation_id."""

    def __init__(self, max_conversations: int = 100, max_exchanges_per_conversation: int = 10):
        self._cache: dict[str, ConversationContext] = {}
        self._max_conversations = max_conversations
        self._max_exchanges = max_exchanges_per_conversation

    def add_exchange(
        self, conversation_id: str | None, user_prompt: str, assistant_response: str
    ):
        """Record a conversation exchange."""
        if conversation_id is None:
            return

        if conversation_id not in self._cache:
            # Check if we need to evict old conversations
            if len(self._cache) >= self._max_conversations:
                # Remove oldest conversation (simple FIFO)
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]

            self._cache[conversation_id] = ConversationContext(
                max_exchanges=self._max_exchanges
            )

        self._cache[conversation_id].add_exchange(user_prompt, assistant_response)

    def get_history(self, conversation_id: str | None) -> str:
        """Get formatted conversation history for a conversation_id."""
        if conversation_id is None:
            return ""

        context = self._cache.get(conversation_id)
        if context is None:
            return ""

        return context.format_for_prompt()

    def clear(self, conversation_id: str | None):
        """Clear history for a specific conversation."""
        if conversation_id and conversation_id in self._cache:
            del self._cache[conversation_id]

    def clear_all(self):
        """Clear all conversation history."""
        self._cache.clear()
