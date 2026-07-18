import aiohttp
from datetime import datetime, timezone
import os
import json
import logging
from pathlib import Path
import discord
from discord import app_commands
from discord.ext import commands, tasks

NOTIFY_USER_ID = 298121351871594497  # DM recipient

# Optional Discord channel destinations for policy-specific watcher alerts.
# Leave either value unset/blank to keep that policy falling back to the DM recipient above.
MOD_ALERT_CHANNEL_ID = os.getenv("HABBO_MOD_ALERT_CHANNEL_ID", "").strip()
OOA_ALERT_CHANNEL_ID = os.getenv("HABBO_OOA_ALERT_CHANNEL_ID", "").strip()

# Group IDs supplied by the operator.
MOD_GROUP_ID = "g-hhus-eb463e25366b3796072507bc69cbfee4"
OOA_GROUP_ID = "g-hhus-1685c3902d4ce5c8a4fcefa160fedaa2"

LOGGER = logging.getLogger(__name__)

# Notification milestones by policy.
# Tuple shape: (trigger_days_offline, embed_title, alert_key)
MOD_MILESTONES = (
    (71 / 24, "Offline Warning (2 Days 23 Hours)", "offline_mod_2d_23h"),
    (3.0, "Offline Warning (3 Days)", "offline_mod_3d"),
)
OOA_MILESTONES = (
    (23 / 24, "OOA Offline Warning (23 Hours)", "offline_ooa_23h"),
    (1.0, "OOA Offline Warning (1 Day)", "offline_ooa_24h"),
)

POLICIES = {
    "MOD": {"allowed_days": 3.0, "milestones": MOD_MILESTONES},
    "OOA": {"allowed_days": 1.0, "milestones": OOA_MILESTONES},
}

class HabboWatch(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        self._state: dict[str, dict] = {}
        self._last_error_notifications: dict[str, datetime] = {}
        # Resolve JSON storage from the bot root (..../UNBOT/JSON) even though this cog lives in COGS/.
        bot_root = Path(__file__).resolve().parent.parent
        self.last_online_file = bot_root / "JSON" / "habbo_last_online.json"
        self.logoff_file = bot_root / "JSON" / "habbo_logoff_times.json"
        self.offline_records_file = bot_root / "JSON" / "habbo_offline_records.json"
        self.alert_channels_file = bot_root / "JSON" / "habbo_alert_channels.json"
        self.last_online_times = self.load_last_online_times()
        self.logoff_times = self.load_logoff_times()
        self.offline_records = self.load_offline_records()
        self.alert_channel_ids = self.load_alert_channel_ids()
        self.periodic_check.start()

    async def cog_unload(self):
        self.periodic_check.cancel()
        await self.session.close()

    @staticmethod
    def ensure_json_file(file_path: Path):
        """Create a JSON storage file with an empty object when it is missing.

        The bot stores watcher state in files that are intentionally ignored by
        git. This guard makes startup and later saves safe on fresh installs or
        after an operator deletes one of the JSON files while the bot is offline.
        """
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if not file_path.exists():
            file_path.write_text("{}", encoding="utf-8")

    def load_last_online_times(self) -> dict[str, str]:
        """Load persisted last-online timestamps, creating JSON storage when missing."""
        try:
            # Always create missing storage before reading so fresh installs work immediately.
            self.ensure_json_file(self.last_online_file)
            data = json.loads(self.last_online_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # Keep only simple string timestamp values.
                return {str(k).lower(): str(v) for k, v in data.items() if isinstance(v, str)}
        except Exception:
            pass
        return {}

    def save_last_online_times(self):
        """Persist last-online timestamps to disk after state-changing events."""
        try:
            self.ensure_json_file(self.last_online_file)
            payload = json.dumps(self.last_online_times, indent=2, sort_keys=True)
            self.last_online_file.write_text(payload, encoding="utf-8")
        except Exception:
            pass

    def load_logoff_times(self) -> dict[str, str]:
        """Load persisted active->offline transition timestamps from JSON storage."""
        try:
            # Create the logoff file so each tracked transition is durable and auditable.
            self.ensure_json_file(self.logoff_file)
            data = json.loads(self.logoff_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k).lower(): str(v) for k, v in data.items() if isinstance(v, str)}
        except Exception:
            pass
        return {}

    def save_logoff_times(self):
        """Persist active->offline transition timestamps to disk."""
        try:
            self.ensure_json_file(self.logoff_file)
            payload = json.dumps(self.logoff_times, indent=2, sort_keys=True)
            self.logoff_file.write_text(payload, encoding="utf-8")
        except Exception:
            pass

    def load_offline_records(self) -> dict[str, dict]:
        """Load the full offline audit log used by Discord reporting commands.

        The active logoff file is intentionally tiny because it is used for alert
        restoration. This file keeps the operator-facing audit trail: the current
        offline window, the latest observed online timestamp, and completed
        offline windows for each Habbo user.
        """
        try:
            self.ensure_json_file(self.offline_records_file)
            data = json.loads(self.offline_records_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {}

            records: dict[str, dict] = {}
            for username, record in data.items():
                if not isinstance(record, dict):
                    continue

                # Normalize the shape so older/manual edits do not break commands.
                history = record.get("history", [])
                if not isinstance(history, list):
                    history = []

                records[str(username).lower()] = {
                    "display_name": str(record.get("display_name") or username),
                    "policy": str(record.get("policy") or "Unknown"),
                    "last_seen_online_at": record.get("last_seen_online_at") if isinstance(record.get("last_seen_online_at"), str) else None,
                    "current_offline_since": record.get("current_offline_since") if isinstance(record.get("current_offline_since"), str) else None,
                    "history": [entry for entry in history if isinstance(entry, dict)],
                }
            return records
        except Exception:
            pass
        return {}

    def save_offline_records(self):
        """Persist the full offline audit log for slash-command reporting."""
        try:
            self.ensure_json_file(self.offline_records_file)
            payload = json.dumps(self.offline_records, indent=2, sort_keys=True)
            self.offline_records_file.write_text(payload, encoding="utf-8")
        except Exception:
            pass

    def load_alert_channel_ids(self) -> dict[str, list[int]]:
        defaults = {
            "MOD": self.parse_discord_ids(MOD_ALERT_CHANNEL_ID),
            "OOA": self.parse_discord_ids(OOA_ALERT_CHANNEL_ID),
        }
        try:
            self.ensure_json_file(self.alert_channels_file)
            data = json.loads(self.alert_channels_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for policy_name in POLICIES:
                    configured_ids = self.parse_discord_ids(data.get(policy_name.lower()) or data.get(policy_name))
                    if configured_ids:
                        defaults[policy_name] = configured_ids
        except Exception:
            pass
        return defaults

    def save_alert_channel_ids(self):
        """Persist the channel routing changed by setmod/setooa text commands."""
        try:
            self.ensure_json_file(self.alert_channels_file)
            payload = {
                policy_name.lower(): channel_ids
                for policy_name, channel_ids in self.alert_channel_ids.items()
                if channel_ids
            }
            self.alert_channels_file.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        except Exception:
            pass

    def get_or_create_offline_record(self, username_lc: str, display_name: str, policy_name: str) -> dict:
        """Return a stable JSON-backed record bucket for one Habbo user."""
        record = self.offline_records.setdefault(
            username_lc,
            {
                "display_name": display_name,
                "policy": policy_name,
                "last_seen_online_at": None,
                "current_offline_since": None,
                "history": [],
            },
        )
        record["display_name"] = display_name
        record["policy"] = policy_name
        record.setdefault("history", [])
        return record

    def record_online_observation(self, username_lc: str, display_name: str, policy_name: str, observed_at: datetime):
        """Store the newest time we directly observed a user online."""
        record = self.get_or_create_offline_record(username_lc, display_name, policy_name)
        record["last_seen_online_at"] = observed_at.isoformat()

    def record_offline_start(self, username_lc: str, display_name: str, policy_name: str, offline_since: datetime):
        """Record the start of a currently active offline window in JSON."""
        record = self.get_or_create_offline_record(username_lc, display_name, policy_name)
        record["current_offline_since"] = offline_since.isoformat()

    def record_offline_end(self, username_lc: str, display_name: str, policy_name: str, went_offline_at: datetime, back_online_at: datetime):
        """Archive a completed offline window and clear the active JSON marker."""
        record = self.get_or_create_offline_record(username_lc, display_name, policy_name)
        if went_offline_at.tzinfo is None:
            went_offline_at = went_offline_at.replace(tzinfo=timezone.utc)
        if back_online_at.tzinfo is None:
            back_online_at = back_online_at.replace(tzinfo=timezone.utc)

        duration_seconds = int(max(0, (back_online_at - went_offline_at).total_seconds()))
        record["current_offline_since"] = None
        record["last_seen_online_at"] = back_online_at.isoformat()
        record["history"].append(
            {
                "offline_since": went_offline_at.isoformat(),
                "back_online_at": back_online_at.isoformat(),
                "duration_seconds": duration_seconds,
                "policy": policy_name,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    async def fetch_json(self, url: str, params: dict | None = None) -> dict | list | None:
        try:
            async with self.session.get(url, params=params, timeout=20) as resp:
                if resp.status == 404:
                    return None
                resp.raise_for_status()
                ct = resp.headers.get("content-type", "")
                if "json" in ct:
                    return await resp.json()
                return None
        except Exception as exc:
            LOGGER.warning("Unable to fetch Habbo API JSON from %s with params %s: %s", url, params, exc)
            return None

    @staticmethod
    def extract_group_member_names(data: dict | list | None) -> list[str]:
        """Extract Habbo names from known group-member response shapes.

        Habbo's public group-member endpoint has appeared as both a bare list
        and a paged object. Keeping the extraction in one tested helper avoids
        dropping users when the API wraps members with pagination metadata.
        """
        if isinstance(data, list):
            members = data
        elif isinstance(data, dict):
            members = data.get("members") or data.get("items") or data.get("data") or []
        else:
            members = []

        usernames: list[str] = []
        for member in members:
            if not isinstance(member, dict):
                continue
            name = member.get("name") or member.get("habboName") or member.get("username")
            if name:
                usernames.append(str(name))
        return usernames

    @staticmethod
    def group_members_has_next_page(data: dict | list | None, current_page: int, names_found: int, page_size: int) -> bool:
        """Return whether another group-member page should be requested.

        Some Habbo responses include explicit page counts, while bare-list
        responses only tell us whether the current page was full. Supporting
        both forms prevents large groups from being truncated after page one.
        """
        if isinstance(data, dict):
            total_pages = data.get("totalPages") or data.get("pageCount") or data.get("total_pages")
            if total_pages is not None:
                try:
                    return current_page < int(total_pages)
                except (TypeError, ValueError):
                    return False

            has_more = data.get("hasMore") or data.get("hasNextPage") or data.get("nextPage")
            if has_more is not None:
                return bool(has_more)

        return names_found >= page_size

    async def fetch_group_members(self, group_id: str) -> list[str]:
        """Return a list of Habbo usernames in the given group.

        This pulls members from the configured MOD/OOA groups only; a user's
        total number of joined groups is not used when deciding whom to check.
        """
        usernames: list[str] = []
        page = 1
        page_size = 100
        while True:
            url = f"https://www.habbo.com/api/public/groups/{group_id}/members"
            data = await self.fetch_json(url, params={"pageNumber": page, "pageSize": page_size})
            if not data:
                break
            page_usernames = self.extract_group_member_names(data)
            if not page_usernames:
                break
            usernames.extend(page_usernames)
            if not self.group_members_has_next_page(data, page, len(page_usernames), page_size):
                break
            page += 1
        return sorted(set(usernames))

    async def fetch_habbo_user(self, username: str) -> dict | None:
        # Hotel hardcoded
        url = "https://www.habbo.com/api/public/users"
        params = {"name": username}
        data = await self.fetch_json(url, params=params)
        if data is None:
            LOGGER.warning("Habbo profile lookup returned no public user for %s", username)
        return data if isinstance(data, dict) else None

    async def fetch_habbo_user_forced(self, username: str, attempts: int = 3) -> dict | None:
        """Try a Habbo profile lookup more than once for manual forced checks.

        A blank `/check` is operator-initiated and expected to inspect every
        roster member as thoroughly as possible. Retrying each individual user
        helps avoid treating a short-lived Habbo API hiccup as an unavailable
        profile while still falling back to a diagnostic embed after all
        attempts fail.
        """
        for attempt_number in range(1, attempts + 1):
            user_json = await self.fetch_habbo_user(username)
            if user_json:
                return user_json
            if attempt_number < attempts:
                LOGGER.info(
                    "Retrying Habbo profile lookup for %s after failed attempt %s/%s",
                    username,
                    attempt_number,
                    attempts,
                )
        return None

    async def fetch_user_policy_map(self) -> dict[str, tuple[str, str]]:
        """Fetch watched group members with their exact lookup name and policy.

        Keys stay lower-case for stable de-duplication and saved state, but the
        value preserves the original Habbo casing from the group roster. Passing
        the exact roster name back to Habbo avoids false "profile unavailable"
        results caused by over-normalizing names before lookup. OOA wins when a
        member appears in both groups because its shorter offline policy is more
        urgent.
        """
        mod_members = await self.fetch_group_members(MOD_GROUP_ID)
        ooa_members = await self.fetch_group_members(OOA_GROUP_ID)

        user_policy_map: dict[str, tuple[str, str]] = {}
        for username in mod_members:
            user_policy_map[username.lower()] = (username, "MOD")
        for username in ooa_members:
            user_policy_map[username.lower()] = (username, "OOA")
        return user_policy_map

    @staticmethod
    def parse_iso(ts: str | None):
        if not ts:
            return None
        try:
            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            else:
                ts = ts.replace(" ", "T")
                if ts.endswith("+0000"):
                    ts = ts[:-5] + "+00:00"
            return datetime.fromisoformat(ts)
        except Exception:
            return None

    @staticmethod
    def days_since(dt):
        if not dt:
            return None
        now = datetime.now(timezone.utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (now - dt).total_seconds() / 86400.0

    @classmethod
    def newer_habbo_last_access(cls, user_json: dict, offline_since_dt: datetime | None) -> datetime | None:
        """Return Habbo lastAccessTime only when it corrects a stale bot time.

        Bot-observed JSON remains the source used to start offline tracking. This
        guard only moves an existing offline start forward when Habbo reports
        later activity, preventing false multi-day alerts after missed scans or
        state resets without inventing an offline timer for users the bot never
        saw online.
        """
        if not offline_since_dt:
            return None

        habbo_last_access = cls.parse_iso(user_json.get("lastAccessTime") or user_json.get("last_access_time"))
        if not habbo_last_access:
            return None
        if offline_since_dt.tzinfo is None:
            offline_since_dt = offline_since_dt.replace(tzinfo=timezone.utc)
        if habbo_last_access.tzinfo is None:
            habbo_last_access = habbo_last_access.replace(tzinfo=timezone.utc)
        return habbo_last_access if habbo_last_access > offline_since_dt else None

    @staticmethod
    def format_offline_duration(offline_since_dt: datetime | None) -> str | None:
        """Return a human-readable elapsed duration since the user went offline."""
        if not offline_since_dt:
            return None

        now = datetime.now(timezone.utc)
        if offline_since_dt.tzinfo is None:
            offline_since_dt = offline_since_dt.replace(tzinfo=timezone.utc)

        elapsed_seconds = int(max(0, (now - offline_since_dt).total_seconds()))
        days, remainder = divmod(elapsed_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)

        parts: list[str] = []
        if days:
            parts.append(f"{days} day{'s' if days != 1 else ''}")
        if hours:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes:
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")

        # If a user has just gone offline, keep output explicit instead of empty.
        if not parts:
            parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
        return ", ".join(parts)


    @staticmethod
    def format_duration_seconds(total_seconds: int | None) -> str:
        """Return a human-readable duration from a saved number of seconds."""
        if total_seconds is None:
            return "Unknown"

        total_seconds = max(0, int(total_seconds))
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)

        parts: list[str] = []
        if days:
            parts.append(f"{days} day{'s' if days != 1 else ''}")
        if hours:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes:
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
        if not parts:
            parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")
        return ", ".join(parts)

    @staticmethod
    def split_usernames(usernames: str) -> list[str]:
        """Split comma/space/newline separated Habbo usernames for operator commands."""
        cleaned = usernames.replace(",", " ").replace("\n", " ")
        return [part.strip() for part in cleaned.split(" ") if part.strip()]


    @staticmethod
    def normalize_policy(policy_name: str | None) -> str:
        """Return a supported watcher policy name for manual JSON edits.

        Operators type this value in Discord, so the helper accepts lowercase
        input and safely falls back to MOD instead of writing an unsupported
        policy string into the audit JSON.
        """
        normalized = str(policy_name or "MOD").strip().upper()
        return normalized if normalized in POLICIES else "MOD"

    @staticmethod
    def parse_operator_datetime(timestamp_text: str | None) -> datetime:
        """Parse an operator-provided timestamp or default to the current UTC time.

        Supported input is intentionally simple for Discord messages: ISO-8601
        text (with either a ``T`` or a space), a trailing ``Z``, or a Unix
        timestamp. Naive values are treated as UTC to keep the JSON consistent.
        """
        if not timestamp_text or not str(timestamp_text).strip():
            return datetime.now(timezone.utc)

        value = str(timestamp_text).strip()
        parsed: datetime | None = None
        if value.isdigit():
            parsed = datetime.fromtimestamp(int(value), tz=timezone.utc)
        else:
            if value.endswith("Z"):
                value = value[:-1] + "+00:00"
            value = value.replace(" ", "T")
            parsed = datetime.fromisoformat(value)

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def apply_manual_json_update(
        self,
        username: str,
        status: str,
        timestamp_text: str | None = None,
        policy_name: str | None = None,
    ) -> str:
        """Apply a Discord-requested JSON update and return a response message.

        This is the shared implementation behind the slash command so tests can
        verify the JSON mutation without needing a live Discord connection.
        """
        display_name = username.strip()
        if not display_name:
            raise ValueError("Please provide a Habbo username.")

        status_lc = status.strip().lower()
        if status_lc not in {"online", "offline"}:
            raise ValueError("Status must be either 'online' or 'offline'.")

        policy = self.normalize_policy(policy_name)
        observed_at = self.parse_operator_datetime(timestamp_text)
        username_lc = display_name.lower()

        if status_lc == "online":
            # A manual online entry mirrors what the watcher records after it
            # observes a user online: update last-online JSON and close any
            # active offline window so future alerts start from fresh state.
            previous_offline_since = self.parse_iso(
                self.offline_records.get(username_lc, {}).get("current_offline_since")
            ) or self.parse_iso(self.logoff_times.get(username_lc))
            self.last_online_times[username_lc] = observed_at.isoformat()
            if previous_offline_since:
                self.record_offline_end(username_lc, display_name, policy, previous_offline_since, observed_at)
            else:
                self.record_online_observation(username_lc, display_name, policy, observed_at)
            self.logoff_times.pop(username_lc, None)
            message = f"Saved {display_name} as online at {observed_at.isoformat()} in the Habbo JSON files."
        else:
            # A manual offline entry creates the same durable markers that the
            # watcher uses after an observed online->offline transition.
            self.logoff_times[username_lc] = observed_at.isoformat()
            self.record_offline_start(username_lc, display_name, policy, observed_at)
            message = f"Saved {display_name} as offline since {observed_at.isoformat()} in the Habbo JSON files."

        self.save_last_online_times()
        self.save_logoff_times()
        self.save_offline_records()
        self._state.pop(username_lc, None)
        return message

    def build_offline_times_embed(self, usernames: list[str], include_history: bool) -> discord.Embed:
        """Build a Discord embed summarizing saved offline times for specific users."""
        embed = discord.Embed(
            title="Recorded Habbo Offline Times",
            description="These times come from the bot's JSON audit file and only include users observed by the watcher.",
            colour=discord.Colour.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"{self.bot.user.name}")

        for username in usernames[:20]:
            username_lc = username.lower()
            record = self.offline_records.get(username_lc, {})
            display_name = record.get("display_name") or username
            lines: list[str] = []

            current_since = self.parse_iso(record.get("current_offline_since")) or self.parse_iso(self.logoff_times.get(username_lc))
            if current_since:
                unix_since = int(current_since.timestamp())
                current_duration = self.format_offline_duration(current_since) or "Unknown"
                lines.append(f"**Current Offline Since:** <t:{unix_since}:R>")
                lines.append(f"**Current Offline For:** {current_duration}")
            else:
                lines.append("**Current Offline:** No active recorded offline window")

            last_seen_online = self.parse_iso(record.get("last_seen_online_at"))
            if last_seen_online:
                lines.append(f"**Last Seen Online:** <t:{int(last_seen_online.timestamp())}:R>")

            history = record.get("history", []) if isinstance(record.get("history"), list) else []
            if include_history and history:
                last_entry = history[-1]
                offline_since = self.parse_iso(last_entry.get("offline_since"))
                back_online_at = self.parse_iso(last_entry.get("back_online_at"))
                duration = self.format_duration_seconds(last_entry.get("duration_seconds"))
                if offline_since and back_online_at:
                    lines.append("**Last Completed Offline Window:**")
                    lines.append(f"Started: <t:{int(offline_since.timestamp())}:R>")
                    lines.append(f"Ended: <t:{int(back_online_at.timestamp())}:R>")
                    lines.append(f"Duration: {duration}")

            if not record:
                lines.append("No JSON record found for this user yet.")

            embed.add_field(name=str(display_name), value="\n".join(lines), inline=False)

        if len(usernames) > 20:
            embed.add_field(
                name="Limit Reached",
                value="Only the first 20 usernames are shown to keep the Discord embed readable.",
                inline=False,
            )
        return embed

    @staticmethod
    def resolve_milestone(days_offline: float | None, milestones: tuple[tuple[float, str, str], ...]):
        """Return the highest milestone reached for the current offline duration."""
        if days_offline is None:
            return None, None

        reached_title = None
        reached_key = None
        for threshold_days, title, key in milestones:
            if days_offline >= threshold_days:
                reached_title = title
                reached_key = key
        return reached_title, reached_key

    def evaluate_user(
        self,
        user_json: dict,
        requested_username: str,
        offline_since_dt: datetime | None,
        policy_name: str,
    ):
        """Build an embed from live status + tracked offline transition time.

        Important behavior:
        - Offline duration is based on `offline_since_dt` only (set when we observe online->offline).
        - We intentionally avoid API last-access timestamps for watcher alert timing.
        """
        name = user_json.get("name") or requested_username
        online = user_json.get("online", user_json.get("isOnline")) is True
        profile_visible = user_json.get("profileVisible", user_json.get("isProfileVisible"))
        if profile_visible is None:
            profile_visible = bool(user_json.get("memberSince") or user_json.get("lastAccessTime"))

        # Full-body avatar (direction changed to 3)
        figure = user_json.get("figureString") or user_json.get("figure")
        if figure:
            avatar_url = f"https://www.habbo.com/habbo-imaging/avatarimage?figure={figure}&size=l&direction=3&head_direction=3"
        else:
            avatar_url = f"https://www.habbo.com/habbo-imaging/avatarimage?user={name}&size=l&direction=3&head_direction=3"

        unique_id = user_json.get("uniqueId")
        lines = []
        if unique_id:
            lines.append(f"## Habbo: [{name}](https://www.habbo.com/profile/{name})")
        else:
            lines.append(f"## Habbo: {name}")

        if not profile_visible:
            title = "Profile Hidden"
            alert_key = "profile_hidden"
        else:
            if online:
                title = "Online"
                alert_key = None
                lines.append("## Status: Online")
            elif offline_since_dt:
                days_offline = self.days_since(offline_since_dt)
                last_seen_relative = f"<t:{int(offline_since_dt.timestamp())}:R>"
                offline_duration = self.format_offline_duration(offline_since_dt)
                lines.append(f"## Last Seen Online: {last_seen_relative}")
                if offline_duration:
                    # Include the exact elapsed time for quick triage in alerts.
                    lines.append(f"## Offline For: {offline_duration}")
                allowed_days = POLICIES[policy_name]["allowed_days"]
                lines.append(f"## Group Policy: {policy_name}")
                lines.append(f"## Allowed Offline Window: {allowed_days:.0f} day(s)")

                # Alerts are sent at specific checkpoints requested by policy.
                # We return only the highest reached checkpoint and rely on sent_alerts
                # deduplication in periodic_check to avoid duplicate notifications.
                milestone_title, milestone_key = self.resolve_milestone(
                    days_offline,
                    POLICIES[policy_name]["milestones"],
                )
                if milestone_title and milestone_key:
                    title = milestone_title
                    alert_key = milestone_key
                else:
                    title = "Recent Activity"
                    alert_key = None
            else:
                # User is offline, but we never observed a live online->offline transition yet.
                # Per requirements, do not start tracking from API last-access values.
                title = "Offline (Awaiting Online Observation)"
                alert_key = None
                lines.append("## Status: Offline (tracking starts after they are seen online first)")

        warn_titles = (
            "Offline Warning (2 Days 23 Hours)",
            "Offline Warning (3 Days)",
            "OOA Offline Warning (23 Hours)",
            "OOA Offline Warning (1 Day)",
            "Profile Hidden",
        )
        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            colour=discord.Colour.red() if title in warn_titles else discord.Colour.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=avatar_url)
        embed.set_footer(text=f"{self.bot.user.name}")
        return embed, online, alert_key, name, avatar_url

    def make_back_online_embed(self, name: str, avatar_url: str, went_offline_at: datetime | None):
        lines = [f"## Habbo: [{name}](https://www.habbo.com/profile/{name})"]
        if went_offline_at:
            unix_then = int(went_offline_at.timestamp())
            unix_now = int(datetime.now(timezone.utc).timestamp())
            offline_duration = self.format_offline_duration(went_offline_at)
            # Show when they were last seen (offline start) and how long until now
            lines.append(f"## Was Offline Since: <t:{unix_then}:R>")
            lines.append(f"## Back Online: <t:{unix_now}:R>")
            if offline_duration:
                lines.append(f"## Total Time Offline: {offline_duration}")
        else:
            lines.append(" ")

        embed = discord.Embed(
            title="Back Online",
            description="\n".join(lines),
            colour=discord.Colour.green(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=avatar_url)
        embed.set_footer(text=f"{self.bot.user.name}")
        return embed

    @staticmethod
    def parse_discord_id(raw_id: str | int | None) -> int | None:
        """Return one Discord snowflake integer from configuration text when valid."""
        if raw_id is None:
            return None
        try:
            value = str(raw_id).strip()
            # Accept plain IDs and Discord channel mention text such as <#123>.
            if value.startswith("<#") and value.endswith(">"):
                value = value[2:-1]
            return int(value) if value else None
        except (TypeError, ValueError):
            return None

    @classmethod
    def parse_discord_ids(cls, raw_ids) -> list[int]:
        """Return unique Discord channel IDs from strings, lists, or channel-like objects.

        JSON may contain either the old single-ID shape or the newer list shape,
        while text commands pass Discord channel objects. Supporting all three
        keeps older configs working and makes multi-channel updates concise.
        """
        if raw_ids is None:
            return []

        if isinstance(raw_ids, (list, tuple, set)):
            candidates = raw_ids
        elif hasattr(raw_ids, "id"):
            candidates = [getattr(raw_ids, "id")]
        elif isinstance(raw_ids, str):
            candidates = raw_ids.replace(",", " ").split()
        else:
            candidates = [raw_ids]

        channel_ids: list[int] = []
        for candidate in candidates:
            channel_id = cls.parse_discord_id(getattr(candidate, "id", candidate))
            if channel_id is not None and channel_id not in channel_ids:
                channel_ids.append(channel_id)
        return channel_ids

    def alert_channel_ids_for_policy(self, policy_name: str | None) -> list[int]:
        """Resolve all optional alert channels for a watcher policy.

        MOD and OOA can be routed independently by the setmod/setooa text
        commands. Missing or invalid channel IDs intentionally return an empty
        list so notifications continue to use the long-standing DM fallback
        instead of being dropped.
        """
        policy = self.normalize_policy(policy_name)
        configured_channels = getattr(self, "alert_channel_ids", {})
        return self.parse_discord_ids(configured_channels.get(policy))

    def configure_alert_channels(self, policy_name: str, raw_channels) -> list[int]:
        """Save one or more alert channels for a policy and return their IDs.

        ``raw_channels`` may include Discord channel objects, plain snowflakes,
        channel mention text, or legacy single-ID values. The command replaces
        the policy's channel list so operators can intentionally remove old
        destinations by running setmod/setooa with the desired final list.
        """
        channel_ids = self.parse_discord_ids(raw_channels)
        if not channel_ids:
            raise ValueError("Please provide at least one valid Discord channel or run the command in the target channel.")

        policy = self.normalize_policy(policy_name)
        self.alert_channel_ids[policy] = channel_ids
        self.save_alert_channel_ids()
        return channel_ids

    def make_error_embed(self, message: str) -> discord.Embed:
        """Build an operator-facing error embed for Habbo watcher failures."""
        embed = discord.Embed(
            title="Habbo Watcher Error",
            description=message,
            colour=discord.Colour.red(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"{self.bot.user.name}")
        return embed

    async def message_error_to_owner(self, message: str, dedupe_key: str | None = None, cooldown_seconds: int = 3600):
        """DM watcher errors to the operator while throttling repeated noise.

        The first occurrence is always sent. Repeated errors with the same key
        are suppressed for the cooldown window so a one-minute API loop cannot
        spam the owner during a Habbo outage or bad channel configuration.
        """
        now = datetime.now(timezone.utc)
        error_key = dedupe_key or message
        last_sent_at = getattr(self, "_last_error_notifications", {}).get(error_key)
        if last_sent_at and (now - last_sent_at).total_seconds() < cooldown_seconds:
            return

        self._last_error_notifications = getattr(self, "_last_error_notifications", {})
        self._last_error_notifications[error_key] = now
        try:
            user = await self.bot.fetch_user(NOTIFY_USER_ID)
            await user.send(embed=self.make_error_embed(message))
        except Exception as exc:
            LOGGER.warning("Unable to DM Habbo watcher error to %s: %s", NOTIFY_USER_ID, exc)

    async def notify_user(self, embed: discord.Embed, policy_name: str | None = None):
        """Send an alert to every configured policy channel, otherwise DM Noah."""
        channel_ids = self.alert_channel_ids_for_policy(policy_name)
        sent_to_channel = False
        for channel_id in channel_ids:
            try:
                channel = self.bot.get_channel(channel_id) if hasattr(self.bot, "get_channel") else None
                if channel is None:
                    channel = await self.bot.fetch_channel(channel_id)
                await channel.send(embed=embed)
                sent_to_channel = True
            except Exception as exc:
                LOGGER.warning("Unable to send Habbo %s alert to channel %s: %s", policy_name, channel_id, exc)
                await self.message_error_to_owner(
                    f"Unable to send Habbo {policy_name or 'general'} alert to channel {channel_id}: {exc}",
                    dedupe_key=f"channel-send:{policy_name}:{channel_id}",
                )

        if sent_to_channel:
            return

        try:
            user = await self.bot.fetch_user(NOTIFY_USER_ID)
            await user.send(embed=embed)
        except Exception as exc:
            LOGGER.warning("Unable to DM Habbo alert to %s: %s", NOTIFY_USER_ID, exc)

    # Poll Habbo every minute so online/offline changes and policy milestones are detected promptly.
    @tasks.loop(minutes=1)
    async def periodic_check(self):
        user_policy_map = await self.fetch_user_policy_map()

        # Check each unique user once.
        for username_lc, (lookup_username, policy_name) in user_policy_map.items():
            user_json = await self.fetch_habbo_user_forced(lookup_username)
            if not user_json:
                self._state.pop(username_lc, None)
                await self.message_error_to_owner(
                    f"Habbo profile lookup failed for watched user {lookup_username}; no status embed could be built from the API response.",
                    dedupe_key=f"profile-lookup:{username_lc}",
                )
                continue

            st = self._state.get(
                username_lc,
                {"was_online": None, "offline_since": None, "sent_alerts": set()},
            )

            # Normalize alert history to a set in case older state shape exists.
            sent_alerts = st.get("sent_alerts")
            if not isinstance(sent_alerts, set):
                sent_alerts = set(sent_alerts or [])
                st["sent_alerts"] = sent_alerts

            previous_online = st.get("was_online")
            is_online = user_json.get("online", user_json.get("isOnline")) is True
            display_name = user_json.get("name") or username_lc
            state_changed = False

            if previous_online is None and (not is_online) and st.get("offline_since") is None:
                restored_active_offline_since = (
                    self.parse_iso(self.offline_records.get(username_lc, {}).get("current_offline_since"))
                    or self.parse_iso(self.logoff_times.get(username_lc))
                )
                restored_last_online = self.parse_iso(self.last_online_times.get(username_lc))
                restored_offline_since = restored_active_offline_since or restored_last_online
                if restored_offline_since:
                    st["offline_since"] = restored_offline_since
                    st["sent_alerts"] = set(st.get("sent_alerts") or [])
                    if restored_last_online and not restored_active_offline_since:
                        # After a reset, the last-online JSON is enough to
                        # resume counting from the last time this bot saw the
                        # user online and to recreate the active offline record.
                        self.logoff_times[username_lc] = restored_last_online.isoformat()
                        self.record_offline_start(username_lc, display_name, policy_name, restored_last_online)
                        state_changed = True
                # Do not use Habbo lastAccessTime for offline counting: policy
                # timers are based only on this bot's JSON-backed observations
                # of when the user was last online.

            # Establish a silent baseline the first time we see each user. The
            # watcher must check every group member every cycle, but it should
            # only notify after a later online-status change is observed.
            initial_observation = previous_online is None

            # Transition flags are the only paths that send Discord messages.
            # If a member is still online or still offline, the loop updates no
            # alert state and intentionally does nothing noisy.
            went_online = previous_online is False and is_online
            went_offline = previous_online is True and (not is_online)
            went_offline_at = st.get("offline_since")

            # Persist every online observation silently. This JSON-backed
            # timestamp is the authoritative "last online" value after restarts
            # and is preferred over Habbo lastAccessTime for offline policy timers.
            if is_online:
                observed_at = datetime.now(timezone.utc)
                self.last_online_times[username_lc] = observed_at.isoformat()
                self.record_online_observation(username_lc, display_name, policy_name, observed_at)
                state_changed = True

            if went_offline:
                # Start offline tracking from the last observed online timestamp stored on disk.
                # If that value is missing/corrupt, fall back to now to keep tracking functional.
                persisted_last_online = self.parse_iso(self.last_online_times.get(username_lc))
                st["offline_since"] = persisted_last_online or datetime.now(timezone.utc)
                st["sent_alerts"] = set()

                # Persist an explicit logoff timestamp for the active->offline transition.
                transition_at = datetime.now(timezone.utc)
                self.logoff_times[username_lc] = transition_at.isoformat()
                self.record_offline_start(username_lc, display_name, policy_name, st["offline_since"])
                state_changed = True
            elif went_online:
                # Returning online ends the current offline tracking window.
                back_online_at = datetime.now(timezone.utc)
                if went_offline_at:
                    self.record_offline_end(username_lc, display_name, policy_name, went_offline_at, back_online_at)
                else:
                    self.record_online_observation(username_lc, display_name, policy_name, back_online_at)

                st["offline_since"] = None
                st["sent_alerts"] = set()

                # Clear last logoff marker once they are active again.
                self.logoff_times.pop(username_lc, None)
                state_changed = True

            corrected_offline_since = self.newer_habbo_last_access(user_json, st.get("offline_since"))
            if (not is_online) and corrected_offline_since:
                # If Habbo shows a newer last access than our saved bot time,
                # move the active offline window forward to avoid false alerts.
                st["offline_since"] = corrected_offline_since
                self.logoff_times[username_lc] = corrected_offline_since.isoformat()
                self.record_offline_start(username_lc, display_name, policy_name, corrected_offline_since)
                st["sent_alerts"] = set()
                sent_alerts = st["sent_alerts"]
                state_changed = True

            embed, _, alert_key, name, avatar_url = self.evaluate_user(
                user_json,
                username_lc,
                st.get("offline_since"),
                policy_name,
            )

            # To reduce spam, do not send a generic "just went offline" message.
            # The watcher quietly counts offline time and only posts configured
            # policy milestones, plus the recovery message when they return.

            # While someone remains offline, count from their saved offline start
            # and flag each policy milestone exactly once. Online users do not
            # produce recurring status messages.
            if (not is_online) and alert_key and alert_key not in sent_alerts:
                await self.notify_user(embed, policy_name)
                sent_alerts.add(alert_key)
                state_changed = True

            # Send one recovery message for every observed offline->online change,
            # even if the bot only has a baseline offline state and no saved
            # offline-start timestamp yet.
            if went_online:
                back_embed = self.make_back_online_embed(name, avatar_url, went_offline_at)
                await self.notify_user(back_embed, policy_name)

            st["was_online"] = is_online
            self._state[username_lc] = st

            if state_changed:
                self.save_last_online_times()
                self.save_logoff_times()
                self.save_offline_records()


    async def force_upload_all_embeds(self) -> tuple[int, int, list[str]]:
        """Check every watched Habbo and resend their current status embed.

        This is intentionally noisier than the periodic watcher: operators use
        the manual command when they want a fresh full upload after changing
        channels, restarting the bot, or correcting state. The method still
        updates the in-memory baseline so the next automatic loop compares
        against the newest status it just observed.
        """
        user_policy_map = await self.fetch_user_policy_map()
        sent_count = 0
        unavailable_usernames: list[str] = []

        for username_lc, (lookup_username, policy_name) in user_policy_map.items():
            user_json = await self.fetch_habbo_user_forced(lookup_username)
            if not user_json:
                # Do not skip watched members when Habbo does not return a
                # public profile. A fallback embed gives operators one message
                # per roster entry while making the lookup problem visible, and
                # the owner DM makes the API/profile error impossible to miss.
                await self.notify_user(self.make_unfetchable_profile_embed(lookup_username), policy_name)
                await self.message_error_to_owner(
                    f"Habbo profile lookup failed for watched user {lookup_username} during forced embed upload; posted a fallback embed instead."
                )
                sent_count += 1
                unavailable_usernames.append(lookup_username)
                continue

            st = self._state.get(
                username_lc,
                {"was_online": None, "offline_since": None, "sent_alerts": set()},
            )
            sent_alerts = st.get("sent_alerts")
            if not isinstance(sent_alerts, set):
                st["sent_alerts"] = set(sent_alerts or [])

            is_online = user_json.get("online", user_json.get("isOnline")) is True
            display_name = user_json.get("name") or lookup_username
            state_changed = False

            if is_online:
                observed_at = datetime.now(timezone.utc)
                previous_offline_since = (
                    st.get("offline_since")
                    or self.parse_iso(self.offline_records.get(username_lc, {}).get("current_offline_since"))
                    or self.parse_iso(self.logoff_times.get(username_lc))
                )
                if previous_offline_since:
                    # A manual /check should close stale offline windows when it
                    # sees the user online, keeping JSON consistent after resets.
                    self.record_offline_end(username_lc, display_name, policy_name, previous_offline_since, observed_at)
                    self.logoff_times.pop(username_lc, None)
                else:
                    self.record_online_observation(username_lc, display_name, policy_name, observed_at)

                self.last_online_times[username_lc] = observed_at.isoformat()
                st["offline_since"] = None
                st["sent_alerts"] = set()
                state_changed = True
            elif st.get("offline_since") is None:
                # Restore persisted active offline windows, or after a reset use
                # the bot-observed last-online JSON as the offline counter start.
                restored_active_offline_since = (
                    self.parse_iso(self.offline_records.get(username_lc, {}).get("current_offline_since"))
                    or self.parse_iso(self.logoff_times.get(username_lc))
                )
                restored_last_online = self.parse_iso(self.last_online_times.get(username_lc))
                st["offline_since"] = restored_active_offline_since or restored_last_online
                if st.get("offline_since"):
                    current_record_since = self.parse_iso(
                        self.offline_records.get(username_lc, {}).get("current_offline_since")
                    )
                    if not restored_active_offline_since:
                        self.logoff_times[username_lc] = st["offline_since"].isoformat()
                    if not current_record_since:
                        self.record_offline_start(username_lc, display_name, policy_name, st["offline_since"])
                    state_changed = True

            corrected_offline_since = self.newer_habbo_last_access(user_json, st.get("offline_since"))
            if (not is_online) and corrected_offline_since:
                # Manual /check should also correct stale JSON before posting the embed.
                st["offline_since"] = corrected_offline_since
                self.logoff_times[username_lc] = corrected_offline_since.isoformat()
                self.record_offline_start(username_lc, display_name, policy_name, corrected_offline_since)
                state_changed = True

            embed, _is_online, _alert_key, _name, _avatar_url = self.evaluate_user(
                user_json,
                username_lc,
                st.get("offline_since"),
                policy_name,
            )
            await self.notify_user(embed, policy_name)
            sent_count += 1

            st["was_online"] = is_online
            self._state[username_lc] = st

            if state_changed:
                self.save_last_online_times()
                self.save_logoff_times()
                self.save_offline_records()

        return sent_count, len(unavailable_usernames), unavailable_usernames

    def make_unfetchable_profile_embed(self, username: str) -> discord.Embed:
        """Build a fallback embed when Habbo profile details cannot be fetched."""
        embed = discord.Embed(
            title="Profile Unavailable",
            description=(
                f"## Habbo: {username}\n"
                "## Last Seen: Unknown\n"
                "## Details: Habbo did not return a public profile for this watched group member. "
                "They may have been renamed, deleted, hidden from the public API, or the API request may have failed."
            ),
            colour=discord.Colour.red(),
        )
        embed.set_footer(text=f"{self.bot.user.name}")
        return embed

    @staticmethod
    def format_force_check_summary(sent_count: int, unavailable_usernames: list[str]) -> str:
        """Build the ephemeral /check result, including fallback-profile diagnostics."""
        unavailable_count = len(unavailable_usernames)
        message = (
            f"Check Complete: uploaded {sent_count} embed(s)"
            f" and used fallback profile embeds for {unavailable_count} member(s)."
        )
        if unavailable_usernames:
            shown_names = ", ".join(unavailable_usernames[:10])
            remaining_count = unavailable_count - 10
            suffix = f" (+{remaining_count} more)" if remaining_count > 0 else ""
            message += f" Fallbacks: {shown_names}{suffix}."
        return message

    @periodic_check.before_loop
    async def before_periodic(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="check", description="Check Habbo status; leave username blank to refresh every watched member.")
    @app_commands.describe(username="Optional single username; leave blank to check everyone and upload all embeds again")
    async def habbo_check(self, interaction: discord.Interaction, username: str | None = None):
        await interaction.response.defer(thinking=True, ephemeral=True)

        if not username:
            # Operators asked for the manual full refresh to live in /check.
            # Leaving username blank intentionally sends every watched member's
            # current embed again, unlike the quiet automatic periodic watcher.
            sent_count, _unavailable_count, unavailable_usernames = await self.force_upload_all_embeds()
            await interaction.followup.send(
                self.format_force_check_summary(sent_count, unavailable_usernames),
                ephemeral=True,
            )
            return

        user_json = await self.fetch_habbo_user(username)
        if not user_json:
            embed = discord.Embed(
                title="Profile Not Found",
                description=f"## Habbo: {username}\n## Last Seen: Unknown\n## Details: No public profile found or invalid username.",
                colour=discord.Colour.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(text=f"{self.bot.user.name}")
            await self.notify_user(embed)
            await interaction.followup.send("Check Complete", ephemeral=True)
            return

        # Slash command checks are ad-hoc lookups without guaranteed group membership.
        # We default to MOD policy for neutral display; no offline milestone fires here
        # because this command intentionally passes offline_since_dt=None.
        embed, _is_online, _alert_key, _name, _avatar_url = self.evaluate_user(user_json, username, None, "MOD")
        # A direct /check request should always post the current profile embed,
        # not only warning embeds, so operators can verify every requested user.
        await self.notify_user(embed)

        await interaction.followup.send("Check Complete", ephemeral=True)

    # Slash-only reporting command so staff can use Discord autocomplete/ephemeral responses.
    @app_commands.command(name="offlinetimes", description="Show recorded offline times for specific Habbo users.")
    @app_commands.describe(
        usernames="Habbo usernames separated by commas or spaces",
        include_history="Show each user's latest completed offline window too",
    )
    async def offline_times(self, interaction: discord.Interaction, usernames: str, include_history: bool = True):
        """Slash command for operators to view JSON-recorded offline times in Discord."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        requested_usernames = self.split_usernames(usernames)

        if not requested_usernames:
            await interaction.followup.send("Please provide at least one Habbo username.", ephemeral=True)
            return

        embed = self.build_offline_times_embed(requested_usernames, include_history)
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="habbojson", description="Manually save a Habbo online/offline entry into the watcher JSON files.")
    @app_commands.describe(
        username="Habbo username to update",
        status="Use 'online' or 'offline'",
        timestamp="Optional ISO time or Unix timestamp; defaults to now in UTC",
        policy="Optional policy name: MOD or OOA",
    )
    async def habbo_json_update(
        self,
        interaction: discord.Interaction,
        username: str,
        status: str,
        timestamp: str | None = None,
        policy: str = "MOD",
    ):
        """Slash command that lets staff edit watcher JSON through Discord."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            message = self.apply_manual_json_update(username, status, timestamp, policy)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:
            LOGGER.warning("Manual Habbo JSON update failed for %s: %s", username, exc)
            await interaction.followup.send("I could not save that JSON update. Check the timestamp format and try again.", ephemeral=True)
            return

        await interaction.followup.send(message, ephemeral=True)

    async def _set_policy_alert_channels(self, ctx: commands.Context, policy_name: str, channels: tuple[discord.TextChannel, ...]):
        """Shared implementation for text commands that route policy alerts."""
        target_channels = channels or (ctx.channel,)
        try:
            channel_ids = self.configure_alert_channels(policy_name, target_channels)
        except ValueError as exc:
            await ctx.send(str(exc), delete_after=10)
            return

        mentions = ", ".join(f"<#{channel_id}>" for channel_id in channel_ids)
        await ctx.send(f"{policy_name} Habbo alerts will now be sent to: {mentions}.", delete_after=10)

    @commands.command(name="setmod")
    @commands.is_owner()
    async def set_mod_alert_channel(self, ctx: commands.Context, *channels: discord.TextChannel):
        """Set one or more MOD alert channels; defaults to the current channel."""
        await self._set_policy_alert_channels(ctx, "MOD", channels)

    @commands.command(name="setooa")
    @commands.is_owner()
    async def set_ooa_alert_channel(self, ctx: commands.Context, *channels: discord.TextChannel):
        """Set one or more OOA alert channels; defaults to the current channel."""
        await self._set_policy_alert_channels(ctx, "OOA", channels)


async def setup(bot: commands.Bot):
    await bot.add_cog(HabboWatch(bot))
