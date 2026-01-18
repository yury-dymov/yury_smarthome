from typing import Any, Optional, List, Dict, Literal
from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.const import MATCH_ALL
from homeassistant.helpers import llm, device_registry as dr, entity
from dataclasses import dataclass
from .const import DOMAIN, CONF_CHAT_MODEL
from abc import abstractmethod

type LocalLLMConfigEntry = ConfigEntry[LocalLLMClient]


class LocalLLMClient:
    """Base Local LLM conversation agent."""

    hass: HomeAssistant

    def __init__(self, hass: HomeAssistant, client_options: dict[str, Any]) -> None:
        self.hass = hass

    async def send_message(self, model: str, message: str) -> str | None:
        raise NotImplementedError()

    @staticmethod
    def get_name(client_options: dict[str, Any]):
        raise NotImplementedError()

    @staticmethod
    async def async_validate_connection(
        hass: HomeAssistant, user_input: Dict[str, Any]
    ) -> str | None:
        """Validate connection to the backend. Implemented by sub-classes"""
        return None

    def _load_model(self, entity_options: dict[str, Any]) -> None:
        """Load the model on the backend. Implemented by sub-classes"""
        pass

    def _update_options(self, entity_options: dict[str, Any]) -> None:
        """Update options on the backend. Implemented by sub-classes"""
        pass


@dataclass(kw_only=True)
class TextGenerationResult:
    response: Optional[str] = None
    stop_reason: Optional[str] = None
    tool_calls: Optional[List[llm.ToolInput]] = None
    response_streamed: bool = False
    raise_error: bool = False
    error_msg: Optional[str] = None


class LocalLLMEntity(entity.Entity):
    """Base LLM Entity"""

    hass: HomeAssistant
    client: LocalLLMClient
    entry_id: str

    _attr_has_entity_name = True

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        subentry: ConfigSubentry,
        client: LocalLLMClient,
    ) -> None:
        """Initialize the agent."""
        self._attr_name = subentry.title
        self._attr_unique_id = subentry.subentry_id
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, subentry.subentry_id)},
            name=subentry.title,
            model=subentry.data.get(CONF_CHAT_MODEL),
            entry_type=dr.DeviceEntryType.SERVICE,
        )

        self.hass = hass
        self.entry_id = entry.entry_id
        self.subentry_id = subentry.subentry_id
        self.client = client

        # create update handler
        self.async_on_remove(entry.add_update_listener(self._async_update_options))

    async def _async_update_options(
        self, hass: HomeAssistant, config_entry: LocalLLMConfigEntry
    ):
        for subentry in config_entry.subentries.values():
            # handle subentry updates, but only invoke for this entity
            if subentry.subentry_id == self.subentry_id:
                await hass.async_add_executor_job(
                    self.client._update_options, self.runtime_options
                )

    @property
    def entry(self) -> ConfigEntry:
        try:
            return self.hass.data[DOMAIN][self.entry_id]
        except KeyError as ex:
            raise Exception("Attempted to use self.entry during startup.") from ex

    @property
    def subentry(self) -> ConfigSubentry:
        try:
            return self.entry.subentries[self.subentry_id]
        except KeyError as ex:
            raise Exception("Attempted to use self.subentry during startup.") from ex

    @property
    def runtime_options(self) -> dict[str, Any]:
        """Return the runtime options for this entity."""
        return {**self.entry.data, **self.subentry.data}

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Return a list of supported languages."""
        return MATCH_ALL

    @abstractmethod
    async def send_message(self, prompt: str) -> str:
        """Send a message."""
