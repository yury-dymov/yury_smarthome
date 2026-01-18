DOMAIN = "yury_smarthome"
CLASSIFICATION_MODEL = "classification_model"
YURY_LLM_API_ID = "yury-llm-api-id"

ALLOWED_SERVICE_CALL_ARGUMENTS = [
    "rgb_color",
    "brightness",
    "temperature",
    "humidity",
    "fan_mode",
    "hvac_mode",
    "preset_mode",
    "item",
    "duration",
]

SERVICE_TOOL_NAME = "Yury Service Tool"
SERVICE_TOOL_ALLOWED_SERVICES = [
    "turn_on",
    "turn_off",
    "toggle",
    "press",
    "increase_speed",
    "decrease_speed",
    "open_cover",
    "close_cover",
    "stop_cover",
    "lock",
    "unlock",
    "start",
    "stop",
    "return_to_base",
    "pause",
    "cancel",
    "add_item",
    "set_temperature",
    "set_humidity",
    "set_fan_mode",
    "set_hvac_mode",
    "set_preset_mode",
]
SERVICE_TOOL_ALLOWED_DOMAINS = [
    "light",
    "switch",
    "button",
    "fan",
    "cover",
    "lock",
    "media_player",
    "climate",
    "vacuum",
    "todo",
    "timer",
    "script",
]

CONF_CHAT_MODEL = "conf_chat_model"
CONF_TTS_ENGINE = "conf_tts_engine"
SUBENTRY_TYPE_TTS = "tts"
LLM_RETRY_COUNT = 3
