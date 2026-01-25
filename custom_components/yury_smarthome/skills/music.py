from .abstract_skill import AbstractSkill
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent, entity_registry, area_registry, device_registry
from homeassistant.components.conversation import ConversationInput
from homeassistant.components import conversation
from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from custom_components.yury_smarthome.entity import LocalLLMEntity
from custom_components.yury_smarthome.prompt_cache import PromptCache
from custom_components.yury_smarthome.qpl import QPLFlow
from custom_components.yury_smarthome.maybe import maybe
from dataclasses import dataclass
from jinja2 import Template
import json
import logging
import os
import traceback


_LOGGER = logging.getLogger(__name__)


@dataclass
class MusicAction:
    action: str  # "play", "pause", "stop", "next", "previous", "volume", "mute", "unmute", "play_media"
    entity_id: str
    previous_volume: float | None = None
    previous_mute: bool | None = None
    media_query: str | None = None


class Music(AbstractSkill):
    last_actions: list[MusicAction]

    def __init__(
        self,
        hass: HomeAssistant,
        client: LocalLLMEntity,
        prompt_cache: PromptCache,
    ):
        super().__init__(hass, client, prompt_cache)
        self.last_actions = []

    def name(self) -> str:
        return "Control Music Devices"

    async def process_user_request(
        self,
        request: ConversationInput,
        response: intent.IntentResponse,
        qpl_flow: QPLFlow,
    ):
        self.last_actions = []
        prompt = await self._build_prompt(request, qpl_flow)
        qpl_flow.mark_subspan_begin("sending_message_to_llm")
        llm_response = await self.client.send_message(prompt)
        point = qpl_flow.mark_subspan_end("sending_message_to_llm")
        llm_response = llm_response.replace("```json", "")
        llm_response = llm_response.replace("```", "")
        maybe(point).annotate("llm_response", llm_response)

        try:
            json_data = json.loads(llm_response)
            commands = json_data if isinstance(json_data, list) else [json_data]

            messages = []
            for cmd in commands:
                action = cmd.get("action")
                entity_id = cmd.get("entity_id")

                if action is None or entity_id is None:
                    messages.append("Missing action or player")
                    continue

                result = None
                if action == "play":
                    result = await self._play(entity_id, qpl_flow)
                elif action == "pause":
                    result = await self._pause(entity_id, qpl_flow)
                elif action == "stop":
                    result = await self._stop(entity_id, qpl_flow)
                elif action == "next":
                    result = await self._next_track(entity_id, qpl_flow)
                elif action == "previous":
                    result = await self._previous_track(entity_id, qpl_flow)
                elif action == "volume_set":
                    volume = cmd.get("volume")
                    result = await self._set_volume(entity_id, volume, qpl_flow)
                elif action == "volume_up":
                    amount = cmd.get("amount", 10)
                    result = await self._adjust_volume(entity_id, amount, qpl_flow)
                elif action == "volume_down":
                    amount = cmd.get("amount", 10)
                    result = await self._adjust_volume(entity_id, -amount, qpl_flow)
                elif action == "mute":
                    result = await self._mute(entity_id, True, qpl_flow)
                elif action == "unmute":
                    result = await self._mute(entity_id, False, qpl_flow)
                elif action == "play_media":
                    media_query = cmd.get("query")
                    media_type = cmd.get("media_type")
                    artist = cmd.get("artist")
                    album = cmd.get("album")
                    result = await self._play_media(
                        entity_id, media_query, media_type, artist, album, qpl_flow
                    )
                elif action == "queue_add_next":
                    media_query = cmd.get("query")
                    media_type = cmd.get("media_type")
                    artist = cmd.get("artist")
                    album = cmd.get("album")
                    result = await self._queue_add(
                        entity_id, media_query, media_type, artist, album, "next", qpl_flow
                    )
                elif action == "queue_add":
                    media_query = cmd.get("query")
                    media_type = cmd.get("media_type")
                    artist = cmd.get("artist")
                    album = cmd.get("album")
                    result = await self._queue_add(
                        entity_id, media_query, media_type, artist, album, "add", qpl_flow
                    )
                elif action == "queue_clear":
                    result = await self._queue_clear(entity_id, qpl_flow)
                else:
                    messages.append(f"Unknown action: {action}")
                    continue

                if result:
                    messages.append(result)

            if messages:
                response.async_set_speech(". ".join(messages))
            else:
                response.async_set_speech("No music actions performed")

        except json.JSONDecodeError as err:
            qpl_flow.mark_failed(err.msg)
            response.async_set_speech("Failed to understand music request")
        except Exception:
            qpl_flow.mark_failed(traceback.format_exc())
            response.async_set_speech("Failed to control music")

    async def _play(self, entity_id: str, qpl_flow: QPLFlow) -> str:
        point = qpl_flow.mark_subspan_begin("play")
        maybe(point).annotate("entity_id", entity_id)

        try:
            await self.hass.services.async_call(
                "media_player", "media_play", {"entity_id": entity_id}, blocking=True
            )
            self.last_actions.append(MusicAction("play", entity_id))
            return "Playing"
        except Exception:
            _LOGGER.warning(f"Failed to play: {traceback.format_exc()}")
            return "Failed to play"
        finally:
            qpl_flow.mark_subspan_end("play")

    async def _pause(self, entity_id: str, qpl_flow: QPLFlow) -> str:
        point = qpl_flow.mark_subspan_begin("pause")
        maybe(point).annotate("entity_id", entity_id)

        try:
            await self.hass.services.async_call(
                "media_player", "media_pause", {"entity_id": entity_id}, blocking=True
            )
            self.last_actions.append(MusicAction("pause", entity_id))
            return "Paused"
        except Exception:
            _LOGGER.warning(f"Failed to pause: {traceback.format_exc()}")
            return "Failed to pause"
        finally:
            qpl_flow.mark_subspan_end("pause")

    async def _stop(self, entity_id: str, qpl_flow: QPLFlow) -> str:
        point = qpl_flow.mark_subspan_begin("stop")
        maybe(point).annotate("entity_id", entity_id)

        try:
            await self.hass.services.async_call(
                "media_player", "media_stop", {"entity_id": entity_id}, blocking=True
            )
            self.last_actions.append(MusicAction("stop", entity_id))
            return "Stopped"
        except Exception:
            _LOGGER.warning(f"Failed to stop: {traceback.format_exc()}")
            return "Failed to stop"
        finally:
            qpl_flow.mark_subspan_end("stop")

    async def _next_track(self, entity_id: str, qpl_flow: QPLFlow) -> str:
        point = qpl_flow.mark_subspan_begin("next_track")
        maybe(point).annotate("entity_id", entity_id)

        try:
            await self.hass.services.async_call(
                "media_player",
                "media_next_track",
                {"entity_id": entity_id},
                blocking=True,
            )
            self.last_actions.append(MusicAction("next", entity_id))
            return "Skipped to next track"
        except Exception:
            _LOGGER.warning(f"Failed to skip track: {traceback.format_exc()}")
            return "Failed to skip track"
        finally:
            qpl_flow.mark_subspan_end("next_track")

    async def _previous_track(self, entity_id: str, qpl_flow: QPLFlow) -> str:
        point = qpl_flow.mark_subspan_begin("previous_track")
        maybe(point).annotate("entity_id", entity_id)

        try:
            await self.hass.services.async_call(
                "media_player",
                "media_previous_track",
                {"entity_id": entity_id},
                blocking=True,
            )
            self.last_actions.append(MusicAction("previous", entity_id))
            return "Went to previous track"
        except Exception:
            _LOGGER.warning(f"Failed to go to previous track: {traceback.format_exc()}")
            return "Failed to go to previous track"
        finally:
            qpl_flow.mark_subspan_end("previous_track")

    async def _set_volume(
        self, entity_id: str, volume: float | None, qpl_flow: QPLFlow
    ) -> str:
        point = qpl_flow.mark_subspan_begin("set_volume")
        maybe(point).annotate("entity_id", entity_id)
        maybe(point).annotate("volume", volume)

        try:
            if volume is None:
                return "No volume level specified"

            # Clamp volume to 0-100 range
            volume = max(0, min(100, volume))

            # Get current volume for undo
            state = self.hass.states.get(entity_id)
            previous_volume = None
            if state and "volume_level" in state.attributes:
                previous_volume = state.attributes["volume_level"] * 100

            await self.hass.services.async_call(
                "media_player",
                "volume_set",
                {"entity_id": entity_id, "volume_level": volume / 100},
                blocking=True,
            )
            self.last_actions.append(
                MusicAction("volume", entity_id, previous_volume=previous_volume)
            )
            return f"Volume set to {int(volume)}%"
        except Exception:
            _LOGGER.warning(f"Failed to set volume: {traceback.format_exc()}")
            return "Failed to set volume"
        finally:
            qpl_flow.mark_subspan_end("set_volume")

    async def _adjust_volume(
        self, entity_id: str, amount: float, qpl_flow: QPLFlow
    ) -> str:
        point = qpl_flow.mark_subspan_begin("adjust_volume")
        maybe(point).annotate("entity_id", entity_id)
        maybe(point).annotate("amount", amount)

        try:
            # Get current volume
            state = self.hass.states.get(entity_id)
            if state is None or "volume_level" not in state.attributes:
                return "Cannot determine current volume"

            current_volume = state.attributes["volume_level"] * 100
            new_volume = max(0, min(100, current_volume + amount))

            await self.hass.services.async_call(
                "media_player",
                "volume_set",
                {"entity_id": entity_id, "volume_level": new_volume / 100},
                blocking=True,
            )
            self.last_actions.append(
                MusicAction("volume", entity_id, previous_volume=current_volume)
            )
            direction = "up" if amount > 0 else "down"
            return f"Volume {direction} to {int(new_volume)}%"
        except Exception:
            _LOGGER.warning(f"Failed to adjust volume: {traceback.format_exc()}")
            return "Failed to adjust volume"
        finally:
            qpl_flow.mark_subspan_end("adjust_volume")

    async def _mute(self, entity_id: str, mute: bool, qpl_flow: QPLFlow) -> str:
        action_name = "mute" if mute else "unmute"
        point = qpl_flow.mark_subspan_begin(action_name)
        maybe(point).annotate("entity_id", entity_id)

        try:
            # Get current mute state for undo
            state = self.hass.states.get(entity_id)
            previous_mute = None
            if state and "is_volume_muted" in state.attributes:
                previous_mute = state.attributes["is_volume_muted"]

            await self.hass.services.async_call(
                "media_player",
                "volume_mute",
                {"entity_id": entity_id, "is_volume_muted": mute},
                blocking=True,
            )
            self.last_actions.append(
                MusicAction(action_name, entity_id, previous_mute=previous_mute)
            )
            return "Muted" if mute else "Unmuted"
        except Exception:
            _LOGGER.warning(f"Failed to {action_name}: {traceback.format_exc()}")
            return f"Failed to {action_name}"
        finally:
            qpl_flow.mark_subspan_end(action_name)

    async def _play_media(
        self,
        entity_id: str,
        query: str | None,
        media_type: str | None,
        artist: str | None,
        album: str | None,
        qpl_flow: QPLFlow,
    ) -> str:
        point = qpl_flow.mark_subspan_begin("play_media")
        maybe(point).annotate("entity_id", entity_id)
        maybe(point).annotate("query", query)
        maybe(point).annotate("media_type", media_type)
        maybe(point).annotate("artist", artist)
        maybe(point).annotate("album", album)

        try:
            if query is None:
                return "What would you like to play?"

            # First try to search in local library via Music Assistant
            config_entry = await self._get_music_assistant_config_entry()

            if config_entry:
                # Try library search first
                library_result = await self._search_library(
                    config_entry, query, media_type, artist, album, qpl_flow
                )

                if library_result:
                    return await self._play_found_media(
                        entity_id, library_result, qpl_flow
                    )

                # Try global search (includes streaming services)
                search_result = await self._search_global(
                    config_entry, query, media_type, artist, album, qpl_flow
                )

                if search_result:
                    return await self._play_found_media(entity_id, search_result, qpl_flow)

            # Fallback: try direct play_media service
            service_data = {
                "entity_id": entity_id,
                "media_id": query,
            }
            if media_type:
                service_data["media_type"] = media_type
            if artist:
                service_data["artist"] = artist
            if album:
                service_data["album"] = album

            # Try Music Assistant play_media first
            if config_entry:
                await self.hass.services.async_call(
                    "music_assistant",
                    "play_media",
                    service_data,
                    blocking=True,
                )
            else:
                # Fallback to standard media_player
                await self.hass.services.async_call(
                    "media_player",
                    "play_media",
                    {
                        "entity_id": entity_id,
                        "media_content_id": query,
                        "media_content_type": media_type or "music",
                    },
                    blocking=True,
                )

            self.last_actions.append(
                MusicAction("play_media", entity_id, media_query=query)
            )
            return f"Playing {query}"
        except Exception:
            _LOGGER.warning(f"Failed to play media: {traceback.format_exc()}")
            return f"Could not find or play '{query}'"
        finally:
            qpl_flow.mark_subspan_end("play_media")

    async def _queue_add(
        self,
        entity_id: str,
        query: str | None,
        media_type: str | None,
        artist: str | None,
        album: str | None,
        enqueue_mode: str,  # "next" or "add"
        qpl_flow: QPLFlow,
    ) -> str:
        """Add media to queue (next or end)."""
        action_name = "queue_add_next" if enqueue_mode == "next" else "queue_add"
        point = qpl_flow.mark_subspan_begin(action_name)
        maybe(point).annotate("entity_id", entity_id)
        maybe(point).annotate("query", query)
        maybe(point).annotate("enqueue_mode", enqueue_mode)

        try:
            if query is None:
                return "What would you like to add to the queue?"

            config_entry = await self._get_music_assistant_config_entry()

            if config_entry:
                # Search for the media first
                library_result = await self._search_library(
                    config_entry, query, media_type, artist, album, qpl_flow
                )
                if not library_result:
                    library_result = await self._search_global(
                        config_entry, query, media_type, artist, album, qpl_flow
                    )

                if library_result:
                    item = library_result["item"]
                    found_type = library_result["type"]
                    media_id = item.get("uri") or item.get("name")

                    await self.hass.services.async_call(
                        "music_assistant",
                        "play_media",
                        {
                            "entity_id": entity_id,
                            "media_id": media_id,
                            "media_type": found_type,
                            "enqueue": enqueue_mode,
                        },
                        blocking=True,
                    )

                    name = item.get("name", media_id)
                    position = "next" if enqueue_mode == "next" else "to queue"
                    self.last_actions.append(
                        MusicAction(action_name, entity_id, media_query=media_id)
                    )
                    return f"Added {name} {position}"

                # Fallback: try direct with query
                await self.hass.services.async_call(
                    "music_assistant",
                    "play_media",
                    {
                        "entity_id": entity_id,
                        "media_id": query,
                        "enqueue": enqueue_mode,
                    },
                    blocking=True,
                )
                position = "next" if enqueue_mode == "next" else "to queue"
                self.last_actions.append(
                    MusicAction(action_name, entity_id, media_query=query)
                )
                return f"Added {query} {position}"
            else:
                return "Music Assistant not available for queue management"
        except Exception:
            _LOGGER.warning(f"Failed to add to queue: {traceback.format_exc()}")
            return f"Could not add '{query}' to queue"
        finally:
            qpl_flow.mark_subspan_end(action_name)

    async def _queue_clear(self, entity_id: str, qpl_flow: QPLFlow) -> str:
        """Clear the playback queue."""
        point = qpl_flow.mark_subspan_begin("queue_clear")
        maybe(point).annotate("entity_id", entity_id)

        try:
            # Try media_player.clear_playlist first
            try:
                await self.hass.services.async_call(
                    "media_player",
                    "clear_playlist",
                    {"entity_id": entity_id},
                    blocking=True,
                )
                self.last_actions.append(MusicAction("queue_clear", entity_id))
                return "Queue cleared"
            except Exception:
                pass

            # Fallback: stop playback
            await self.hass.services.async_call(
                "media_player",
                "media_stop",
                {"entity_id": entity_id},
                blocking=True,
            )
            self.last_actions.append(MusicAction("queue_clear", entity_id))
            return "Queue cleared"
        except Exception:
            _LOGGER.warning(f"Failed to clear queue: {traceback.format_exc()}")
            return "Failed to clear queue"
        finally:
            qpl_flow.mark_subspan_end("queue_clear")

    async def _get_music_assistant_config_entry(self) -> str | None:
        """Get the Music Assistant config entry ID if available."""
        for entry in self.hass.config_entries.async_entries("music_assistant"):
            return entry.entry_id
        return None

    async def _search_library(
        self,
        config_entry_id: str,
        query: str,
        media_type: str | None,
        artist: str | None,
        album: str | None,
        qpl_flow: QPLFlow,
    ) -> dict | None:
        """Search in local Music Assistant library."""
        point = qpl_flow.mark_subspan_begin("search_library")

        try:
            service_data = {
                "config_entry_id": config_entry_id,
                "name": query,
                "library_only": True,
                "limit": 5,
            }
            if media_type:
                service_data["media_type"] = media_type
            if artist:
                service_data["artist"] = artist
            if album:
                service_data["album"] = album

            result = await self.hass.services.async_call(
                "music_assistant",
                "search",
                service_data,
                blocking=True,
                return_response=True,
            )

            maybe(point).annotate("result", str(result)[:500] if result else "None")

            if result and self._has_results(result):
                return self._pick_best_result(result, media_type)

            return None
        except Exception:
            _LOGGER.debug(f"Library search failed: {traceback.format_exc()}")
            return None
        finally:
            qpl_flow.mark_subspan_end("search_library")

    async def _search_global(
        self,
        config_entry_id: str,
        query: str,
        media_type: str | None,
        artist: str | None,
        album: str | None,
        qpl_flow: QPLFlow,
    ) -> dict | None:
        """Search globally in Music Assistant (includes streaming services)."""
        point = qpl_flow.mark_subspan_begin("search_global")

        try:
            service_data = {
                "config_entry_id": config_entry_id,
                "name": query,
                "limit": 5,
            }
            if media_type:
                service_data["media_type"] = media_type
            if artist:
                service_data["artist"] = artist
            if album:
                service_data["album"] = album

            result = await self.hass.services.async_call(
                "music_assistant",
                "search",
                service_data,
                blocking=True,
                return_response=True,
            )

            maybe(point).annotate("result", str(result)[:500] if result else "None")

            if result and self._has_results(result):
                return self._pick_best_result(result, media_type)

            return None
        except Exception:
            _LOGGER.debug(f"Global search failed: {traceback.format_exc()}")
            return None
        finally:
            qpl_flow.mark_subspan_end("search_global")

    def _has_results(self, result: dict) -> bool:
        """Check if search result contains any items."""
        if not result:
            return False
        for key in ["tracks", "albums", "artists", "playlists", "radio"]:
            if key in result and result[key]:
                return True
        return False

    def _pick_best_result(self, result: dict, preferred_type: str | None) -> dict:
        """Pick the best result from search, prioritizing by type."""
        # Priority order based on preference or default
        if preferred_type == "artist":
            order = ["artists", "albums", "tracks", "playlists", "radio"]
        elif preferred_type == "album":
            order = ["albums", "tracks", "artists", "playlists", "radio"]
        elif preferred_type == "playlist":
            order = ["playlists", "tracks", "albums", "artists", "radio"]
        elif preferred_type == "radio":
            order = ["radio", "tracks", "playlists", "albums", "artists"]
        else:
            # Default: tracks first
            order = ["tracks", "albums", "artists", "playlists", "radio"]

        for category in order:
            if category in result and result[category]:
                items = result[category]
                if items:
                    return {"type": category.rstrip("s"), "item": items[0]}

        return None

    async def _play_found_media(
        self, entity_id: str, found: dict, qpl_flow: QPLFlow
    ) -> str:
        """Play the found media item."""
        point = qpl_flow.mark_subspan_begin("play_found_media")

        try:
            item = found["item"]
            media_type = found["type"]

            maybe(point).annotate("media_type", media_type)
            maybe(point).annotate("item_name", item.get("name", "unknown"))

            # Build URI or use name
            media_id = item.get("uri") or item.get("name")

            await self.hass.services.async_call(
                "music_assistant",
                "play_media",
                {
                    "entity_id": entity_id,
                    "media_id": media_id,
                    "media_type": media_type,
                },
                blocking=True,
            )

            self.last_actions.append(
                MusicAction("play_media", entity_id, media_query=media_id)
            )

            name = item.get("name", media_id)
            artist_name = item.get("artist", {}).get("name") if "artist" in item else None

            if artist_name:
                return f"Playing {name} by {artist_name}"
            return f"Playing {name}"
        except Exception:
            _LOGGER.warning(f"Failed to play found media: {traceback.format_exc()}")
            return "Failed to play media"
        finally:
            qpl_flow.mark_subspan_end("play_found_media")

    # --- Undo helper methods (don't track actions) ---

    async def _play_for_undo(self, entity_id: str, qpl_flow: QPLFlow):
        """Play without tracking for undo operations."""
        point = qpl_flow.mark_subspan_begin("play_for_undo")
        maybe(point).annotate("entity_id", entity_id)
        try:
            await self.hass.services.async_call(
                "media_player", "media_play", {"entity_id": entity_id}, blocking=True
            )
        except Exception:
            _LOGGER.warning(f"Failed to play for undo: {traceback.format_exc()}")
        finally:
            qpl_flow.mark_subspan_end("play_for_undo")

    async def _pause_for_undo(self, entity_id: str, qpl_flow: QPLFlow):
        """Pause without tracking for undo operations."""
        point = qpl_flow.mark_subspan_begin("pause_for_undo")
        maybe(point).annotate("entity_id", entity_id)
        try:
            await self.hass.services.async_call(
                "media_player", "media_pause", {"entity_id": entity_id}, blocking=True
            )
        except Exception:
            _LOGGER.warning(f"Failed to pause for undo: {traceback.format_exc()}")
        finally:
            qpl_flow.mark_subspan_end("pause_for_undo")

    async def _stop_for_undo(self, entity_id: str, qpl_flow: QPLFlow):
        """Stop without tracking for undo operations."""
        point = qpl_flow.mark_subspan_begin("stop_for_undo")
        maybe(point).annotate("entity_id", entity_id)
        try:
            await self.hass.services.async_call(
                "media_player", "media_stop", {"entity_id": entity_id}, blocking=True
            )
        except Exception:
            _LOGGER.warning(f"Failed to stop for undo: {traceback.format_exc()}")
        finally:
            qpl_flow.mark_subspan_end("stop_for_undo")

    async def _set_volume_for_undo(self, entity_id: str, volume: float, qpl_flow: QPLFlow):
        """Set volume without tracking for undo operations."""
        point = qpl_flow.mark_subspan_begin("set_volume_for_undo")
        maybe(point).annotate("entity_id", entity_id)
        maybe(point).annotate("volume", volume)
        try:
            await self.hass.services.async_call(
                "media_player",
                "volume_set",
                {"entity_id": entity_id, "volume_level": volume / 100},
                blocking=True,
            )
        except Exception:
            _LOGGER.warning(f"Failed to set volume for undo: {traceback.format_exc()}")
        finally:
            qpl_flow.mark_subspan_end("set_volume_for_undo")

    async def _mute_for_undo(self, entity_id: str, mute: bool, qpl_flow: QPLFlow):
        """Mute/unmute without tracking for undo operations."""
        action_name = "mute_for_undo" if mute else "unmute_for_undo"
        point = qpl_flow.mark_subspan_begin(action_name)
        maybe(point).annotate("entity_id", entity_id)
        try:
            await self.hass.services.async_call(
                "media_player",
                "volume_mute",
                {"entity_id": entity_id, "is_volume_muted": mute},
                blocking=True,
            )
        except Exception:
            _LOGGER.warning(f"Failed to {action_name}: {traceback.format_exc()}")
        finally:
            qpl_flow.mark_subspan_end(action_name)

    async def undo(self, response: intent.IntentResponse, qpl_flow: QPLFlow):
        point = qpl_flow.mark_subspan_begin("music_undo")

        try:
            if not self.last_actions:
                maybe(point).annotate("no action to undo")
                response.async_set_speech("No music action to undo")
                return

            # Save actions and clear BEFORE iterating to prevent loop
            # (helper methods add to last_actions, which would cause infinite iteration)
            actions_to_undo = self.last_actions
            self.last_actions = []

            messages = []
            for action in actions_to_undo:
                maybe(point).annotate("undoing_action", action.action)

                if action.action == "play":
                    await self._pause_for_undo(action.entity_id, qpl_flow)
                    messages.append("Paused")
                elif action.action == "pause":
                    await self._play_for_undo(action.entity_id, qpl_flow)
                    messages.append("Resumed")
                elif action.action == "volume" and action.previous_volume is not None:
                    await self._set_volume_for_undo(action.entity_id, action.previous_volume, qpl_flow)
                    messages.append(f"Volume restored to {int(action.previous_volume)}%")
                elif action.action in ("mute", "unmute") and action.previous_mute is not None:
                    await self._mute_for_undo(action.entity_id, action.previous_mute, qpl_flow)
                    messages.append("Mute state restored")
                elif action.action == "play_media":
                    await self._stop_for_undo(action.entity_id, qpl_flow)
                    messages.append("Stopped playback")
                else:
                    messages.append(f"Cannot undo {action.action}")

            if messages:
                response.async_set_speech(". ".join(messages))
            else:
                response.async_set_speech("Done")
        finally:
            qpl_flow.mark_subspan_end("music_undo")

    async def _build_prompt(
        self, request: ConversationInput, qpl_flow: QPLFlow
    ) -> str:
        qpl_flow.mark_subspan_begin("build_prompt")

        try:
            qpl_flow.mark_subspan_begin("querying_players_from_ha")

            players = []
            er = entity_registry.async_get(self.hass)
            dr = device_registry.async_get(self.hass)
            ar = area_registry.async_get(self.hass)
            user_location = None

            for state in self.hass.states.async_all():
                if not state.entity_id.startswith("media_player."):
                    continue

                if not async_should_expose(self.hass, conversation.DOMAIN, state.entity_id):
                    continue

                entry = {
                    "entity_id": state.entity_id,
                    "friendly_name": state.name,
                    "state": state.state,
                }

                # Add volume info if available
                if "volume_level" in state.attributes:
                    entry["volume"] = int(state.attributes["volume_level"] * 100)
                if "is_volume_muted" in state.attributes:
                    entry["muted"] = state.attributes["is_volume_muted"]

                # Add currently playing info if available
                if state.state == "playing":
                    if "media_title" in state.attributes:
                        entry["now_playing"] = state.attributes["media_title"]
                    if "media_artist" in state.attributes:
                        entry["artist"] = state.attributes["media_artist"]

                # Get area info
                entity = er.async_get(state.entity_id)
                if entity:
                    device = None
                    if entity.device_id:
                        device = dr.async_get(entity.device_id)

                    area_id = entity.area_id
                    if device and device.area_id:
                        area_id = device.area_id

                    if area_id:
                        area = ar.async_get_area(area_id)
                        if area:
                            entry["area"] = area.name
                            if device and device.id == request.device_id:
                                user_location = area.name

                players.append(entry)

            point = qpl_flow.mark_subspan_end("querying_players_from_ha")
            player_list = json.dumps(players)
            maybe(point).annotate("player_list", player_list)

            qpl_flow.mark_subspan_begin("render_prompt")
            prompt_key = os.path.join(os.path.dirname(__file__), "music.md")
            prompt_template = await self.prompt_cache.get(prompt_key, request.conversation_id)
            template = Template(prompt_template, trim_blocks=True)

            output = template.render(
                player_list=player_list,
                user_prompt=request.text,
                user_location=user_location,
            )
            point = qpl_flow.mark_subspan_end("render_prompt")
            maybe(point).annotate("prompt", output)
            return output
        finally:
            qpl_flow.mark_subspan_end("build_prompt")
