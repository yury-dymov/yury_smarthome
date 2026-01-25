from .abstract_skill import AbstractSkill
import json
import os
import logging
import re
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dateutil.relativedelta import relativedelta
from jinja2 import Template
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.components.conversation import ConversationInput
from homeassistant.components.calendar import CalendarEntity
from custom_components.yury_smarthome.entity import LocalLLMEntity
from custom_components.yury_smarthome.prompt_cache import PromptCache
from custom_components.yury_smarthome.qpl import QPL, QPLFlow
from custom_components.yury_smarthome.maybe import maybe
import traceback

_LOGGER = logging.getLogger(__name__)

# Keywords to match notification targets
NOTIFICATION_TARGET_KEYWORDS = ["yury_dymov", "delorean"]

# Keywords to match preferred calendar
CALENDAR_KEYWORDS = ["yury", "local"]

# Hashtag prefix for reminder notifications
REMINDER_HASHTAG_PREFIX = "#remind:"


@dataclass
class CreatedReminder:
    calendar_id: str
    uid: str
    summary: str  # Clean summary without hashtag


class Reminders(AbstractSkill):
    created_reminders: list[CreatedReminder]
    last_calendar_id: str | None
    inbox_tasks_skill: "AbstractSkill | None"
    qpl_provider: QPL
    _listener_registered: bool = False

    def __init__(
        self,
        hass: HomeAssistant,
        client: LocalLLMEntity,
        prompt_cache: PromptCache,
        qpl_provider: QPL,
    ):
        super().__init__(hass, client, prompt_cache)
        self.created_reminders = []
        self.last_calendar_id = None
        self.inbox_tasks_skill = None
        self.qpl_provider = qpl_provider
        self._register_calendar_listener()

    def _register_calendar_listener(self):
        """Register periodic check for reminder notifications."""
        if Reminders._listener_registered:
            return

        # Track which events we've already notified to avoid duplicates
        notified_events: set[str] = set()

        async def _check_upcoming_reminders(*args):
            """Check calendars for upcoming reminders with our hashtag."""
            now = self._get_current_time()
            check_window_start = now - timedelta(minutes=1)
            check_window_end = now + timedelta(minutes=2)

            # Find calendars matching our keywords
            for state in self.hass.states.async_all():
                if not state.entity_id.startswith("calendar."):
                    continue

                entity_lower = state.entity_id.lower()
                name_lower = state.name.lower() if state.name else ""

                # Only check calendars matching our keywords
                matches_keyword = any(
                    kw.lower() in entity_lower or kw.lower() in name_lower
                    for kw in CALENDAR_KEYWORDS
                )
                if not matches_keyword:
                    continue

                try:
                    calendar_entity = self._get_calendar_entity(state.entity_id)
                    if calendar_entity is None:
                        continue

                    events = await calendar_entity.async_get_events(
                        self.hass, check_window_start, check_window_end
                    )

                    for event in events:
                        # Create unique key for this event instance
                        event_key = f"{state.entity_id}:{event.uid}:{event.start}"

                        # Skip if already notified
                        if event_key in notified_events:
                            continue

                        # Check if event has our hashtag in description
                        description = event.description or ""
                        targets = self._decode_reminder_hashtag(description)

                        if targets:
                            summary = event.summary or "Reminder"
                            _LOGGER.info(f"Reminder due: {summary}, targets: {targets}")
                            await self._send_reminder_notification(summary, targets)
                            notified_events.add(event_key)

                            # Clean up old entries (keep last 100)
                            if len(notified_events) > 100:
                                # Remove oldest entries
                                to_remove = list(notified_events)[:50]
                                for key in to_remove:
                                    notified_events.discard(key)

                except Exception as e:
                    _LOGGER.debug(f"Error checking calendar {state.entity_id}: {e}")

        # Check every minute
        from homeassistant.helpers.event import async_track_time_interval

        async_track_time_interval(self.hass, _check_upcoming_reminders, timedelta(minutes=1))
        Reminders._listener_registered = True
        _LOGGER.debug("Reminder check scheduler registered")

    def _encode_reminder_hashtag(self, targets: list[str]) -> str:
        """Encode notification targets as a hashtag suffix.

        Example: ["notify.mobile_app_yury_dymov", "notify.delorean"]
                 -> "#remind:mobile_app_yury_dymov,delorean"
        """
        # Strip "notify." prefix from targets
        clean_targets = [t.replace("notify.", "") for t in targets]
        return f"{REMINDER_HASHTAG_PREFIX}{','.join(clean_targets)}"

    def _decode_reminder_hashtag(self, description: str) -> list[str] | None:
        """Decode hashtag from description and return targets list.

        Returns None if no hashtag found.
        Returns ["notify.target1", ...] if hashtag found.
        """
        if not description or REMINDER_HASHTAG_PREFIX not in description:
            return None

        # Find the hashtag
        match = re.search(rf"{re.escape(REMINDER_HASHTAG_PREFIX)}([^\s]+)", description)
        if not match:
            return None

        targets_str = match.group(1)

        # Parse targets
        targets = [f"notify.{t.strip()}" for t in targets_str.split(",") if t.strip()]

        return targets if targets else None

    def _find_notification_targets(self) -> list[str]:
        """Find notification service targets matching our keywords."""
        targets = []

        # Get all available services
        services = self.hass.services.async_services()
        notify_services = services.get("notify", {})

        for service_name in notify_services:
            service_lower = service_name.lower()
            for keyword in NOTIFICATION_TARGET_KEYWORDS:
                if keyword.lower() in service_lower:
                    targets.append(f"notify.{service_name}")
                    break

        if targets:
            _LOGGER.debug(f"Found notification targets: {targets}")
        else:
            _LOGGER.warning(
                f"No notification targets found matching keywords: {NOTIFICATION_TARGET_KEYWORDS}"
            )

        return targets

    async def _send_reminder_notification(self, summary: str, targets: list[str]):
        """Send notification for a triggered reminder."""
        qpl_flow = self.qpl_provider.create_flow("reminder_notification")
        point = qpl_flow.mark_subspan_begin("send_reminder_notification")
        maybe(point).annotate("summary", summary)
        maybe(point).annotate("targets", str(targets))

        message = f"Reminder: {summary}"
        did_sent_at_least_once = False

        for target in targets:
            try:
                point = qpl_flow.mark_subspan_begin("send_to_target")
                # Extract service name (notify.xxx -> xxx)
                service_name = target.replace("notify.", "")
                maybe(point).annotate("service_name", service_name)
                maybe(point).annotate("message", message)
                await self.hass.services.async_call(
                    "notify",
                    service_name,
                    {
                        "message": message,
                        "title": "Reminder",
                    },
                    blocking=False,
                )
                did_sent_at_least_once = True
                qpl_flow.mark_subspan_end("send_to_target")
                _LOGGER.info(f"Reminder notification sent to {target}: {message}")
            except Exception as e:
                point = qpl_flow.mark_subspan_end("send_to_target")
                err = f"Failed to send notification to {target}: {e}"
                maybe(point).annotate("error", err)
                _LOGGER.warning(err)

        qpl_flow.mark_subspan_end("send_reminder_notification")
        if did_sent_at_least_once:
            qpl_flow.mark_success()
        else:
            qpl_flow.mark_failed("failed to notify")

    def set_inbox_tasks_skill(self, skill: "AbstractSkill"):
        """Set reference to inbox_tasks skill for delegation."""
        self.inbox_tasks_skill = skill

    def name(self) -> str:
        return "Reminders"

    async def process_user_request(
        self,
        request: ConversationInput,
        response: intent.IntentResponse,
        qpl_flow: QPLFlow,
    ):
        self.created_reminders = []
        self.last_calendar_id = None

        try:
            # Step 1: Select the calendar (prefer local calendar)
            calendar_id = await self._select_calendar(qpl_flow)
            if calendar_id is None:
                err = "No calendar was found for reminders"
                qpl_flow.mark_failed(err)
                response.async_set_speech(err)
                return

            self.last_calendar_id = calendar_id

            # Step 2: Get existing reminders for context
            existing_reminders = await self._get_existing_reminders(
                calendar_id, qpl_flow
            )

            # Step 3: Parse the user's request
            action_prompt = await self._build_action_prompt(
                request, existing_reminders, qpl_flow
            )
            point = qpl_flow.mark_subspan_begin("sending_action_prompt_to_llm")
            maybe(point).annotate("prompt", action_prompt)
            llm_response = await self.client.send_message(action_prompt)
            point = qpl_flow.mark_subspan_end("sending_action_prompt_to_llm")
            llm_response = llm_response.replace("```json", "")
            llm_response = llm_response.replace("```", "")
            maybe(point).annotate("llm_response", llm_response)

            json_data = json.loads(llm_response)
            action = json_data.get("action")

            if action == "delegate_to_todo":
                await self._delegate_to_inbox_tasks(
                    json_data.get("task", ""), request, response, qpl_flow
                )
                return

            if action == "no_match":
                match_summary = json_data.get("match_summary", "")
                err = f"Could not find a reminder matching '{match_summary}'"
                qpl_flow.mark_canceled(err)
                response.async_set_speech(err)
                return

            if action == "create":
                await self._create_reminder(
                    calendar_id, json_data, response, qpl_flow
                )
            elif action == "delete":
                await self._delete_reminder(
                    calendar_id, json_data, existing_reminders, response, qpl_flow
                )
            else:
                err = "Unknown action"
                qpl_flow.mark_failed(err)
                response.async_set_speech(err)

        except json.JSONDecodeError as e:
            qpl_flow.mark_failed(e.msg)
            response.async_set_speech("Failed to process the request")
        except Exception:
            qpl_flow.mark_failed(traceback.format_exc())
            response.async_set_speech("Failed")

    async def _delegate_to_inbox_tasks(
        self,
        task: str,
        request: ConversationInput,
        response: intent.IntentResponse,
        qpl_flow: QPLFlow,
    ):
        """Delegate to inbox_tasks skill when no time is specified."""
        point = qpl_flow.mark_subspan_begin("delegate_to_inbox_tasks")
        maybe(point).annotate("task", task)

        if self.inbox_tasks_skill is None:
            err = "Cannot create a task - inbox tasks skill not available"
            qpl_flow.mark_failed(err)
            response.async_set_speech(err)
            qpl_flow.mark_subspan_end("delegate_to_inbox_tasks")
            return

        modified_request = ConversationInput(
            text=f"Add {task}",
            context=request.context,
            conversation_id=request.conversation_id,
            device_id=request.device_id,
            satellite_id=request.satellite_id,
            language=request.language,
            agent_id=request.agent_id,
        )

        await self.inbox_tasks_skill.process_user_request(
            modified_request, response, qpl_flow
        )
        qpl_flow.mark_subspan_end("delegate_to_inbox_tasks")

    async def _create_reminder(
        self,
        calendar_id: str,
        data: dict,
        response: intent.IntentResponse,
        qpl_flow: QPLFlow,
    ):
        """Create a new reminder event."""
        point = qpl_flow.mark_subspan_begin("create_reminder")

        summary = data.get("summary", "Reminder")
        time_spec = data.get("time_spec")
        recurrence = data.get("recurrence")

        if not time_spec:
            err = "No time was specified for the reminder"
            qpl_flow.mark_failed(err)
            response.async_set_speech(err)
            qpl_flow.mark_subspan_end("create_reminder")
            return

        maybe(point).annotate("summary", summary)
        maybe(point).annotate("time_spec", json.dumps(time_spec))
        maybe(point).annotate("calendar_id", calendar_id)

        # Parse the time specification
        start_dt = self._parse_time_spec(time_spec)
        if start_dt is None:
            err = "Could not parse the time specification"
            qpl_flow.mark_failed(err)
            response.async_set_speech(err)
            qpl_flow.mark_subspan_end("create_reminder")
            return

        maybe(point).annotate("parsed_datetime", start_dt.isoformat())

        # End time is 1 hour after start
        end_dt = start_dt + timedelta(hours=1)

        # Get the calendar entity directly
        calendar_entity = self._get_calendar_entity(calendar_id)
        if calendar_entity is None:
            err = f"Could not find calendar entity: {calendar_id}"
            qpl_flow.mark_failed(err)
            response.async_set_speech(err)
            qpl_flow.mark_subspan_end("create_reminder")
            return

        # Find notification targets and create hashtag for description
        targets = self._find_notification_targets()
        description = self._encode_reminder_hashtag(targets) if targets else ""
        maybe(point).annotate("description", description)

        # Build kwargs for async_create_event
        event_data = {
            "summary": summary,
            "dtstart": start_dt,
            "dtend": end_dt,
        }

        if description:
            event_data["description"] = description

        if recurrence:
            rrule = self._build_rrule(recurrence, start_dt)
            if rrule:
                event_data["rrule"] = rrule
                maybe(point).annotate("rrule", rrule)

        try:
            # Generate UID deterministically from event properties
            uid = self._generate_uid(summary, start_dt, end_dt)
            maybe(point).annotate("generated_uid", uid)

            # Include UID in event data so we can delete it later
            event_data["uid"] = uid

            await calendar_entity.async_create_event(**event_data)

            # Track for undo (store clean summary for user display)
            self.created_reminders.append(
                CreatedReminder(calendar_id=calendar_id, uid=uid, summary=summary)
            )

            _LOGGER.debug(f"Created reminder: {summary} at {start_dt}")

            qpl_flow.mark_subspan_end("create_reminder")

            time_str = self._format_datetime_friendly(start_dt)
            if recurrence:
                freq = recurrence.get("frequency", "")
                interval = recurrence.get("interval", 1)
                if interval == 1:
                    answer = f"Reminder set: {summary}, {freq} starting {time_str}"
                else:
                    answer = f"Reminder set: {summary}, every {interval} {freq}s starting {time_str}"
            else:
                answer = f"Reminder set: {summary} for {time_str}"

            response.async_set_speech(answer)

        except Exception:
            qpl_flow.mark_failed(traceback.format_exc())
            response.async_set_speech("Failed to create reminder")
            qpl_flow.mark_subspan_end("create_reminder")

    async def _delete_reminder(
        self,
        calendar_id: str,
        data: dict,
        existing_reminders: list[dict],
        response: intent.IntentResponse,
        qpl_flow: QPLFlow,
    ):
        """Delete an existing reminder."""
        point = qpl_flow.mark_subspan_begin("delete_reminder")

        match_summary = data.get("match_summary", "").lower()
        maybe(point).annotate("match_summary", match_summary)

        best_match = None
        for reminder in existing_reminders:
            # Compare against clean summary (without hashtag)
            summary = reminder.get("summary", "").lower()
            if match_summary in summary or summary in match_summary:
                best_match = reminder
                break

        if not best_match:
            err = f"Could not find a reminder matching '{match_summary}'"
            qpl_flow.mark_canceled(err)
            response.async_set_speech(err)
            qpl_flow.mark_subspan_end("delete_reminder")
            return

        uid = best_match.get("uid")
        # Get clean summary for user display
        summary = best_match.get("summary", "")

        if not uid:
            err = "Cannot delete this reminder - no UID found"
            qpl_flow.mark_failed(err)
            response.async_set_speech(err)
            qpl_flow.mark_subspan_end("delete_reminder")
            return

        maybe(point).annotate("uid", uid)
        maybe(point).annotate("summary", summary)

        # Get the calendar entity directly
        calendar_entity = self._get_calendar_entity(calendar_id)
        if calendar_entity is None:
            err = f"Could not find calendar entity: {calendar_id}"
            qpl_flow.mark_failed(err)
            response.async_set_speech(err)
            qpl_flow.mark_subspan_end("delete_reminder")
            return

        try:
            await calendar_entity.async_delete_event(uid)

            qpl_flow.mark_subspan_end("delete_reminder")
            response.async_set_speech(f"Reminder '{summary}' deleted")

        except Exception:
            qpl_flow.mark_failed(traceback.format_exc())
            response.async_set_speech("Failed to delete reminder")
            qpl_flow.mark_subspan_end("delete_reminder")

    def _generate_uid(
        self,
        summary: str,
        start_dt: datetime,
        end_dt: datetime,
    ) -> str:
        """Generate a deterministic UID from event properties."""
        # Create a unique string from the event properties
        uid_source = f"{summary}|{start_dt.isoformat()}|{end_dt.isoformat()}"
        # Hash it to create a consistent UID
        uid_hash = hashlib.sha256(uid_source.encode()).hexdigest()[:32]
        return f"{uid_hash}@yury-smarthome"

    def _build_rrule(self, recurrence: dict, start_dt: datetime) -> str | None:
        """Build an iCalendar RRULE string from recurrence data."""
        frequency = recurrence.get("frequency")
        if not frequency:
            return None

        freq_map = {
            "daily": "DAILY",
            "weekly": "WEEKLY",
            "monthly": "MONTHLY",
            "yearly": "YEARLY",
        }

        freq = freq_map.get(frequency.lower())
        if not freq:
            return None

        parts = [f"FREQ={freq}"]

        interval = recurrence.get("interval", 1)
        if interval > 1:
            parts.append(f"INTERVAL={interval}")

        count = recurrence.get("count")
        if count:
            parts.append(f"COUNT={count}")

        until = recurrence.get("until")
        if until:
            # UNTIL must match DTSTART type - if DTSTART is datetime, UNTIL must be datetime too
            # Parse the date and use the same time as start_dt
            try:
                until_date = datetime.strptime(until, "%Y-%m-%d")
                until_dt = until_date.replace(
                    hour=start_dt.hour,
                    minute=start_dt.minute,
                    second=start_dt.second,
                    tzinfo=start_dt.tzinfo,
                )
                # Format as iCalendar datetime (YYYYMMDDTHHMMSS)
                until_str = until_dt.strftime("%Y%m%dT%H%M%S")
                parts.append(f"UNTIL={until_str}")
            except ValueError:
                # Fallback to just the date if parsing fails
                until_clean = until.replace("-", "")
                parts.append(f"UNTIL={until_clean}")

        byday = recurrence.get("byday")
        if byday:
            parts.append(f"BYDAY={','.join(byday)}")

        bymonthday = recurrence.get("bymonthday")
        if bymonthday:
            parts.append(f"BYMONTHDAY={bymonthday}")

        return ";".join(parts)

    def _format_datetime_friendly(self, dt: datetime) -> str:
        """Format datetime to a friendly string."""
        now = self._get_current_time()

        if dt.date() == now.date():
            return f"today at {dt.strftime('%I:%M %p')}"
        elif dt.date() == (now + timedelta(days=1)).date():
            return f"tomorrow at {dt.strftime('%I:%M %p')}"
        elif (dt.date() - now.date()).days < 7:
            return f"{dt.strftime('%A')} at {dt.strftime('%I:%M %p')}"
        else:
            return dt.strftime("%B %d at %I:%M %p")

    def _get_timezone(self) -> ZoneInfo:
        """Get the Home Assistant configured timezone."""
        tz_str = self.hass.config.time_zone
        return ZoneInfo(tz_str)

    def _get_current_time(self) -> datetime:
        """Get current time in the configured timezone."""
        tz = self._get_timezone()
        return datetime.now(tz)

    def _get_calendar_entity(self, entity_id: str) -> CalendarEntity | None:
        """Get the CalendarEntity instance for the given entity_id."""
        # Try the standard EntityComponent path
        entity_component = self.hass.data.get("calendar")
        if entity_component is not None and hasattr(entity_component, "get_entity"):
            entity = entity_component.get_entity(entity_id)
            if entity:
                return entity

        # Try via entity_platform
        try:
            from homeassistant.helpers.entity_platform import async_get_platforms

            platforms = async_get_platforms(self.hass, "calendar")
            for platform in platforms:
                if entity_id in platform.entities:
                    return platform.entities[entity_id]
        except Exception as e:
            _LOGGER.debug(f"Could not get calendar entity via platforms: {e}")

        _LOGGER.warning(f"Could not find calendar entity: {entity_id}")
        return None

    def _parse_time_spec(self, time_spec: dict) -> datetime | None:
        """Parse the time specification from LLM and return a datetime."""
        if not time_spec:
            return None

        spec_type = time_spec.get("type")
        value = time_spec.get("value")

        if not spec_type or not value:
            return None

        now = self._get_current_time()

        if spec_type == "relative":
            return self._parse_relative_time(now, value)
        elif spec_type == "absolute":
            return self._parse_absolute_time(now, value)

        return None

    def _parse_relative_time(self, now: datetime, value: dict) -> datetime:
        """Parse relative time specification (e.g., 'in 2 hours')."""
        delta = timedelta()

        if "minutes" in value:
            delta += timedelta(minutes=value["minutes"])
        if "hours" in value:
            delta += timedelta(hours=value["hours"])
        if "days" in value:
            delta += timedelta(days=value["days"])
        if "weeks" in value:
            delta += timedelta(weeks=value["weeks"])
        if "months" in value:
            # Use relativedelta for months
            return now + relativedelta(months=value["months"])

        return now + delta

    def _parse_absolute_time(self, now: datetime, value: dict) -> datetime | None:
        """Parse absolute time specification (e.g., 'tomorrow at 3pm')."""
        day_spec = value.get("day", "today")
        time_spec = value.get("time", "09:00")

        # Parse the time
        try:
            hour, minute = map(int, time_spec.split(":"))
        except (ValueError, AttributeError):
            hour, minute = 9, 0

        # Parse the day
        target_date = self._parse_day_spec(now, day_spec)
        if target_date is None:
            return None

        # Combine date and time
        result = target_date.replace(hour=hour, minute=minute, second=0, microsecond=0)

        # If the time has already passed today, move to tomorrow
        if day_spec == "today" and result <= now:
            result += timedelta(days=1)

        return result

    def _parse_day_spec(self, now: datetime, day_spec: str) -> datetime | None:
        """Parse day specification and return the target date."""
        day_spec_lower = day_spec.lower().strip()

        if day_spec_lower == "today":
            return now

        if day_spec_lower == "tomorrow":
            return now + timedelta(days=1)

        if day_spec_lower == "next_week":
            return now + timedelta(weeks=1)

        # Handle next_monday, next_tuesday, etc.
        weekday_map = {
            "next_monday": 0,
            "next_tuesday": 1,
            "next_wednesday": 2,
            "next_thursday": 3,
            "next_friday": 4,
            "next_saturday": 5,
            "next_sunday": 6,
        }

        if day_spec_lower in weekday_map:
            target_weekday = weekday_map[day_spec_lower]
            current_weekday = now.weekday()
            days_ahead = target_weekday - current_weekday
            if days_ahead <= 0:  # Target day already happened this week
                days_ahead += 7
            return now + timedelta(days=days_ahead)

        # Handle specific date YYYY-MM-DD
        if re.match(r"^\d{4}-\d{2}-\d{2}$", day_spec):
            try:
                return datetime.strptime(day_spec, "%Y-%m-%d").replace(
                    tzinfo=now.tzinfo
                )
            except ValueError:
                return None

        # Handle day of month (e.g., "15")
        if re.match(r"^\d{1,2}$", day_spec):
            try:
                target_day = int(day_spec)
                # Find next occurrence of this day
                target = now.replace(day=target_day)
                if target <= now:
                    # Move to next month
                    target = target + relativedelta(months=1)
                return target
            except ValueError:
                return None

        return None

    async def _select_calendar(self, qpl_flow: QPLFlow) -> str | None:
        """Select the calendar for reminders, preferring calendars matching keywords."""
        qpl_flow.mark_subspan_begin("select_calendar")

        # Query all calendar entities
        qpl_flow.mark_subspan_begin("querying_calendar_entities")
        calendars = []
        for state in self.hass.states.async_all():
            if not state.entity_id.startswith("calendar."):
                continue
            calendars.append(
                {
                    "entity_id": state.entity_id,
                    "friendly_name": state.name,
                }
            )
        point = qpl_flow.mark_subspan_end("querying_calendar_entities")
        calendars_json = json.dumps(calendars)
        maybe(point).annotate("calendars", calendars_json)

        if not calendars:
            qpl_flow.mark_subspan_end("select_calendar")
            return None

        if len(calendars) == 1:
            qpl_flow.mark_subspan_end("select_calendar")
            return calendars[0]["entity_id"]

        # Look for a calendar matching our keywords
        for keyword in CALENDAR_KEYWORDS:
            for cal in calendars:
                entity_lower = cal["entity_id"].lower()
                name_lower = cal["friendly_name"].lower()
                if keyword.lower() in entity_lower or keyword.lower() in name_lower:
                    point = qpl_flow.mark_subspan_end("select_calendar")
                    maybe(point).annotate("selected_calendar_id", cal["entity_id"])
                    maybe(point).annotate("matched_keyword", keyword)
                    return cal["entity_id"]

        # Build and send prompt to select calendar
        qpl_flow.mark_subspan_begin("render_select_calendar_prompt")
        prompt_key = os.path.join(
            os.path.dirname(__file__), "reminders_select_calendar.md"
        )
        prompt_template = await self.prompt_cache.get(prompt_key)
        template = Template(prompt_template, trim_blocks=True)
        prompt = template.render(calendars=calendars_json)
        point = qpl_flow.mark_subspan_end("render_select_calendar_prompt")
        maybe(point).annotate("prompt", prompt)

        qpl_flow.mark_subspan_begin("sending_select_calendar_to_llm")
        llm_response = await self.client.send_message(prompt)
        point = qpl_flow.mark_subspan_end("sending_select_calendar_to_llm")
        llm_response = llm_response.replace("```json", "")
        llm_response = llm_response.replace("```", "")
        maybe(point).annotate("llm_response", llm_response)

        try:
            json_data = json.loads(llm_response)
            calendar_id = json_data.get("entity_id")
            point = qpl_flow.mark_subspan_end("select_calendar")
            maybe(point).annotate("selected_calendar_id", calendar_id)
            return calendar_id
        except json.JSONDecodeError:
            qpl_flow.mark_subspan_end("select_calendar")
            return None

    async def _get_existing_reminders(
        self, calendar_id: str, qpl_flow: QPLFlow
    ) -> list[dict]:
        """Get existing reminders from the calendar."""
        point = qpl_flow.mark_subspan_begin("get_existing_reminders")
        maybe(point).annotate("calendar_id", calendar_id)

        reminders = []
        try:
            # Get the calendar entity directly
            calendar_entity = self._get_calendar_entity(calendar_id)
            if calendar_entity is None:
                qpl_flow.mark_subspan_end("get_existing_reminders")
                return []

            now = self._get_current_time()
            end = now + timedelta(days=30)

            events = await calendar_entity.async_get_events(self.hass, now, end)

            for event in events:
                reminders.append(
                    {
                        "summary": event.summary or "",
                        "start": event.start,
                        "uid": event.uid,
                    }
                )

            point = qpl_flow.mark_subspan_end("get_existing_reminders")
            maybe(point).annotate("reminder_count", len(reminders))
            return reminders

        except Exception:
            qpl_flow.mark_subspan_end("get_existing_reminders")
            return []

    async def _build_action_prompt(
        self,
        request: ConversationInput,
        existing_reminders: list[dict],
        qpl_flow: QPLFlow,
    ) -> str:
        """Build prompt for parsing the reminder request."""
        qpl_flow.mark_subspan_begin("build_action_prompt")

        prompt_key = os.path.join(os.path.dirname(__file__), "reminders.md")
        prompt_template = await self.prompt_cache.get(prompt_key, request.conversation_id)
        template = Template(prompt_template, trim_blocks=True)

        # Use clean summaries (without hashtags) for LLM
        reminder_summaries = [r.get("summary", "") for r in existing_reminders]
        reminders_json = json.dumps(reminder_summaries)

        output = template.render(
            existing_reminders=reminders_json,
            user_prompt=request.text,
        )
        point = qpl_flow.mark_subspan_end("build_action_prompt")
        maybe(point).annotate("prompt", output)
        maybe(point).annotate("existing_reminders", reminders_json)
        return output

    async def undo(self, response: intent.IntentResponse, qpl_flow: QPLFlow):
        point = qpl_flow.mark_subspan_begin("reminders_undo")

        if not self.created_reminders:
            maybe(point).annotate("status", "no reminders to undo")
            response.async_set_speech("Nothing to undo")
            qpl_flow.mark_subspan_end("reminders_undo")
            return

        deleted_summaries = []

        for reminder in self.created_reminders:
            point = qpl_flow.mark_subspan_begin("undo_single_reminder")
            maybe(point).annotate("uid", reminder.uid)
            maybe(point).annotate("summary", reminder.summary)

            try:
                # Get the calendar entity directly
                calendar_entity = self._get_calendar_entity(reminder.calendar_id)
                if calendar_entity is not None:
                    _LOGGER.debug(f"Deleting event with uid: {reminder.uid}")
                    await calendar_entity.async_delete_event(reminder.uid)
                    deleted_summaries.append(reminder.summary)
                else:
                    _LOGGER.warning(f"Could not get calendar entity for undo: {reminder.calendar_id}")

            except Exception as e:
                _LOGGER.error(f"Failed to undo reminder: {e}")

            qpl_flow.mark_subspan_end("undo_single_reminder")

        self.created_reminders = []
        qpl_flow.mark_subspan_end("reminders_undo")

        if deleted_summaries:
            if len(deleted_summaries) == 1:
                response.async_set_speech(
                    f"Reminder '{deleted_summaries[0]}' was removed"
                )
            else:
                response.async_set_speech(
                    f"Reminders removed: {', '.join(deleted_summaries)}"
                )
        else:
            response.async_set_speech("Could not undo reminders")
