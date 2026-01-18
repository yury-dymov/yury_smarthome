from typing import Final

import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import llm
from homeassistant.util.json import JsonObjectType

from .const import (
    ALLOWED_SERVICE_CALL_ARGUMENTS,
    DOMAIN,
    SERVICE_TOOL_ALLOWED_DOMAINS,
    SERVICE_TOOL_ALLOWED_SERVICES,
    SERVICE_TOOL_NAME,
    YURY_LLM_API_ID,
)
from .entity import LocalLLMConfigEntry
from .ollama import OllamaAPIClient


async def async_setup_entry(hass: HomeAssistant, entry: LocalLLMConfigEntry) -> bool:
    # make sure the API is registered
    if not any([x.id == YURY_LLM_API_ID for x in llm.async_get_apis(hass)]):
        llm.async_register_api(hass, YuryLLMAPI(hass))

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry

    def create_client():
        client_options = {**dict(entry.data), **dict(entry.options)}
        return OllamaAPIClient(hass, client_options)

    entry.runtime_data = await hass.async_add_executor_job(create_client)
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.CONVERSATION])
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True

async def _async_update_listener(
    hass: HomeAssistant, entry: LocalLLMConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: LocalLLMConfigEntry) -> bool:
    """Unload the integration."""
    hass.data[DOMAIN].pop(entry.entry_id)
    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: LocalLLMConfigEntry):
    """Migrate old entry."""
    return True


class YuryLLMAPI(llm.API):
    def __init__(self, hass: HomeAssistant) -> None:
        """Init the class."""
        super().__init__(
            hass=hass,
            id=YURY_LLM_API_ID,
            name="Home Assistant Services",
        )

    async def async_get_api_instance(
        self, llm_context: llm.LLMContext
    ) -> llm.APIInstance:
        """Return the instance of the API."""
        return llm.APIInstance(
            api=self,
            api_prompt="Call services in Home Assistant by passing the service name and the device to control. Designed for Home-LLM Models (v1-v3)",
            llm_context=llm_context,
            tools=[YuryServiceTool()],
        )


class YuryServiceTool(llm.Tool):
    name: Final[str] = SERVICE_TOOL_NAME
    description: Final[str] = "Executes a Home Assistant service"

    # Optional. A voluptuous schema of the input parameters.
    parameters = vol.Schema(
        {
            vol.Required("service"): str,
            vol.Required("target_device"): str,
            vol.Optional("rgb_color"): str,
            vol.Optional("brightness"): float,
            vol.Optional("temperature"): float,
            vol.Optional("humidity"): float,
            vol.Optional("fan_mode"): str,
            vol.Optional("hvac_mode"): str,
            vol.Optional("preset_mode"): str,
            vol.Optional("duration"): str,
            vol.Optional("item"): str,
        }
    )

    ALLOWED_SERVICES: Final[list[str]] = SERVICE_TOOL_ALLOWED_SERVICES
    ALLOWED_DOMAINS: Final[list[str]] = SERVICE_TOOL_ALLOWED_DOMAINS

    async def async_call(
        self,
        hass: HomeAssistant,
        tool_input: llm.ToolInput,
        llm_context: llm.LLMContext,
    ) -> JsonObjectType:
        """Call the tool."""
        try:
            domain, service = tuple(tool_input.tool_args["service"].split("."))
        except ValueError:
            return {"result": "unknown service"}

        target_device = tool_input.tool_args["target_device"]

        if domain not in self.ALLOWED_DOMAINS or service not in self.ALLOWED_SERVICES:
            return {"result": "unknown service"}

        if domain == "script" and service not in [
            "reload",
            "turn_on",
            "turn_off",
            "toggle",
        ]:
            return {"result": "unknown service"}

        service_data = {ATTR_ENTITY_ID: target_device}
        for attr in ALLOWED_SERVICE_CALL_ARGUMENTS:
            if attr in tool_input.tool_args.keys():
                service_data[attr] = tool_input.tool_args[attr]
        try:
            await hass.services.async_call(
                domain,
                service,
                service_data=service_data,
                blocking=True,
            )
        except Exception:
            return {"result": "failed"}

        return {"result": "success"}
