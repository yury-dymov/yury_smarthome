import aiofiles
from .conversation_history import ConversationHistoryCache


class PromptCache:
    cache: dict[str, str] = {}
    conversation_history: ConversationHistoryCache | None = None

    def __init__(self, conversation_history: ConversationHistoryCache | None = None):
        self.cache = {}
        self.conversation_history = conversation_history

    def set_conversation_history(self, history: ConversationHistoryCache):
        """Set the conversation history cache instance."""
        self.conversation_history = history

    async def get(self, key: str, conversation_id: str | None = None) -> str:
        """Get a prompt template, optionally appending conversation history.

        Args:
            key: The file path to the prompt template
            conversation_id: If provided, conversation history will be appended

        Returns:
            The prompt template, optionally with conversation history appended
        """
        cached_version = self.cache.get(key)
        if cached_version is None:
            async with aiofiles.open(key) as file:
                data = await file.read()
                self.cache[key] = data
                cached_version = data

        # Append conversation history if available and conversation_id provided
        if conversation_id and self.conversation_history:
            history = self.conversation_history.get_history(conversation_id)
            if history:
                return cached_version + "\n\n" + history

        return cached_version
