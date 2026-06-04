import aiohttp
from datetime import datetime, timezone
import json
from pathlib import Path
import discord
from discord import app_commands
from discord.ext import commands, tasks

NOTIFY_USER_ID = 298121351871594497  # DM recipient

# Group IDs supplied by the operator.
MOD_GROUP_ID = "g-hhus-eb463e25366b3796072507bc69cbfee4"
OOA_GROUP_ID = "g-hhus-1685c3902d4ce5c8a4fcefa160fedaa2"

# Notification milestones by policy.
# Tuple shape: (trigger_days_offline, embed_title, alert_key)
MOD_MILESTONES = (
    (2.0, "Offline Notice (2 Days)", "offline_mod_2d"),
    (2.5, "Offline Warning (Approaching 3 Days)", "offline_mod_2_5d"),
    (3.0, "Offline Warning", "offline_mod_3d"),
)
OOA_MILESTONES = (
    (16 / 24, "Approaching 16 Hours", "offline_ooa_16h"),
    (23 / 24, "23 Hours Offline", "offline_ooa_23h"),
    (1.0, "Offline Warning", "offline_ooa_24h"),
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
        # Resolve JSON storage from the bot root (..../UNBOT/JSON) even though this cog lives in COGS/.
        bot_root = Path(__file__).resolve().parent.parent
        self.last_online_file = bot_root / "JSON" / "habbo_last_online.json"
        self.logoff_file = bot_root / "JSON" / "habbo_logoff_times.json"
        self.offline_records_file = bot_root / "JSON" / "habbo_offline_records.json"
        self.last_online_times = self.load_last_online_times()
        self.logoff_times = self.load_logoff_times()
        self.offline_records = self.load_offline_records()
        self.periodic_check.start()

    async def cog_unload(self):
        self.periodic_check.cancel()
        await self.session.close()

    def load_last_online_times(self) -> dict[str, str]:
        """Load persisted last-online timestamps, creating JSON storage when missing."""
        try:
            self.last_online_file.parent.mkdir(parents=True, exist_ok=True)
            if not self.last_online_file.exists():
                # Create the JSON file the first time so operators can inspect/edit if needed.
                self.last_online_file.write_text("{}", encoding="utf-8")
                return {}

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
            self.last_online_file.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self.last_online_times, indent=2, sort_keys=True)
            self.last_online_file.write_text(payload, encoding="utf-8")
        except Exception:
            pass

    def load_logoff_times(self) -> dict[str, str]:
        """Load persisted active->offline transition timestamps from JSON storage."""
        try:
            self.logoff_file.parent.mkdir(parents=True, exist_ok=True)
            if not self.logoff_file.exists():
                # Create the logoff file so each tracked transition is durable and auditable.
                self.logoff_file.write_text("{}", encoding="utf-8")
                return {}

            data = json.loads(self.logoff_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k).lower(): str(v) for k, v in data.items() if isinstance(v, str)}
        except Exception:
            pass
        return {}

    def save_logoff_times(self):
        """Persist active->offline transition timestamps to disk."""
        try:
            self.logoff_file.parent.mkdir(parents=True, exist_ok=True)
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
            self.offline_records_file.parent.mkdir(parents=True, exist_ok=True)
            if not self.offline_records_file.exists():
                self.offline_records_file.write_text("{}", encoding="utf-8")
                return {}

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
            self.offline_records_file.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self.offline_records, indent=2, sort_keys=True)
            self.offline_records_file.write_text(payload, encoding="utf-8")
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
        except Exception:
            return None

    async def fetch_group_members(self, group_id: str) -> list[str]:
        """Return a list of Habbo usernames in the given group (handles basic pagination)."""
        usernames: list[str] = []
        page = 1
        page_size = 100
        while True:
            url = f"https://www.habbo.com/api/public/groups/{group_id}/members"
            data = await self.fetch_json(url, params={"pageNumber": page, "pageSize": page_size})
            if not data:
                break
            members = data if isinstance(data, list) else data.get("members", [])
            if not members:
                break
            for m in members:
                name = m.get("name") or m.get("habboName") or m.get("username")
                if name:
                    usernames.append(name)
            total_pages = (data.get("totalPages") if isinstance(data, dict) else None) or 1
            if page >= total_pages:
                break
            page += 1
        return sorted(set(usernames))

    async def fetch_habbo_user(self, username: str) -> dict | None:
        # Hotel hardcoded
        url = "https://www.habbo.com/api/public/users"
        params = {"name": username}
        data = await self.fetch_json(url, params=params)
        return data if isinstance(data, dict) else None

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

    def build_offline_times_embed(self, usernames: list[str], include_history: bool) -> discord.Embed:
        """Build a Discord embed summarizing saved offline times for specific users."""
        embed = discord.Embed(
            title="Recorded Habbo Offline Times",
            description="These times come from the bot's JSON audit file and only include users observed by the watcher.",
            colour=discord.Colour.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_footer(text=f"{self.bot.user.name} - Siren - Noah")

        for username in usernames[:20]:
            username_lc = username.lower()
            record = self.offline_records.get(username_lc, {})
            display_name = record.get("display_name") or username
            lines: list[str] = []

            current_since = self.parse_iso(record.get("current_offline_since")) or self.parse_iso(self.logoff_times.get(username_lc))
            if current_since:
                unix_since = int(current_since.timestamp())
                current_duration = self.format_offline_duration(current_since) or "Unknown"
                lines.append(f"**Current Offline Since:** <t:{unix_since}:F>")
                lines.append(f"**Current Offline For:** {current_duration}")
            else:
                lines.append("**Current Offline:** No active recorded offline window")

            last_seen_online = self.parse_iso(record.get("last_seen_online_at"))
            if last_seen_online:
                lines.append(f"**Last Seen Online:** <t:{int(last_seen_online.timestamp())}:F>")

            history = record.get("history", []) if isinstance(record.get("history"), list) else []
            if include_history and history:
                last_entry = history[-1]
                offline_since = self.parse_iso(last_entry.get("offline_since"))
                back_online_at = self.parse_iso(last_entry.get("back_online_at"))
                duration = self.format_duration_seconds(last_entry.get("duration_seconds"))
                if offline_since and back_online_at:
                    lines.append("**Last Completed Offline Window:**")
                    lines.append(f"Started: <t:{int(offline_since.timestamp())}:F>")
                    lines.append(f"Ended: <t:{int(back_online_at.timestamp())}:F>")
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
                last_seen_str = offline_since_dt.strftime("%Y-%m-%d %H:%M UTC")
                offline_duration = self.format_offline_duration(offline_since_dt)
                lines.append(f"## Last Seen Online: {last_seen_str}")
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
            "Offline Notice (2 Days)",
            "Offline Warning (Approaching 3 Days)",
            "Approaching 16 Hours",
            "23 Hours Offline",
            "Offline Warning",
            "Profile Hidden",
        )
        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            colour=discord.Colour.red() if title in warn_titles else discord.Colour.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=avatar_url)
        embed.set_footer(text=f"{self.bot.user.name} - Siren - Noah")
        return embed, online, alert_key, name, avatar_url

    def make_back_online_embed(self, name: str, avatar_url: str, went_offline_at: datetime | None):
        lines = [f"## Habbo: [{name}](https://www.habbo.com/profile/{name})"]
        if went_offline_at:
            unix_then = int(went_offline_at.timestamp())
            unix_now = int(datetime.now(timezone.utc).timestamp())
            offline_duration = self.format_offline_duration(went_offline_at)
            # Show when they were last seen (offline start) and how long until now
            lines.append(f"## Was Offline Since: <t:{unix_then}:F>")
            lines.append(f"## Back Online: <t:{unix_now}:F>")
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
        embed.set_footer(text=f"{self.bot.user.name} - Siren - Noah")
        return embed

    async def notify_user(self, embed: discord.Embed):
        try:
            user = await self.bot.fetch_user(NOTIFY_USER_ID)
            await user.send(embed=embed)
        except Exception:
            pass

    # Poll every 2.5 minutes so policy milestones are detected closer to real-time.
    @tasks.loop(minutes=2.5)
    async def periodic_check(self):
        # Collect memberships independently so we can apply explicit precedence.
        # Requirement: all OOA users are also MOD, but OOA policy must win for OOA users.
        mod_members = {u.lower() for u in await self.fetch_group_members(MOD_GROUP_ID)}
        ooa_members = {u.lower() for u in await self.fetch_group_members(OOA_GROUP_ID)}

        # Build username -> policy map once.
        user_policy_map: dict[str, str] = {}
        for username_lc in mod_members:
            user_policy_map[username_lc] = "MOD"
        for username_lc in ooa_members:
            # OOA assignment intentionally overwrites MOD assignment.
            user_policy_map[username_lc] = "OOA"

        # Check each unique user once.
        for username_lc, policy_name in user_policy_map.items():
            user_json = await self.fetch_habbo_user(username_lc)
            if not user_json:
                self._state.pop(username_lc, None)
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


            if previous_online is None and (not is_online) and st.get("offline_since") is None:
                restored_offline_since = (
                    self.parse_iso(self.offline_records.get(username_lc, {}).get("current_offline_since"))
                    or self.parse_iso(self.logoff_times.get(username_lc))
                )
                if restored_offline_since:
                    st["offline_since"] = restored_offline_since
                    st["sent_alerts"] = set(st.get("sent_alerts") or [])

            # Transition flags are used to reset tracking only when state changes,
            # preventing repeated alerts while status is unchanged.
            went_online = previous_online is False and is_online
            went_offline = previous_online is True and (not is_online)
            went_offline_at = st.get("offline_since")
            state_changed = False

            # Track only observed online->offline transitions.
            if is_online:
                # Continuously refresh last-online timestamp while online so it is durable across restarts.
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

            embed, _, alert_key, name, avatar_url = self.evaluate_user(
                user_json,
                username_lc,
                st.get("offline_since"),
                policy_name,
            )

            # Send milestone/profile-hidden alerts only once per tracking window.
            if alert_key and alert_key not in st["sent_alerts"]:
                await self.notify_user(embed)
                st["sent_alerts"].add(alert_key)

            # Send one recovery message when user comes back online.
            if went_online and went_offline_at:
                back_embed = self.make_back_online_embed(name, avatar_url, went_offline_at)
                await self.notify_user(back_embed)

            st["was_online"] = is_online
            self._state[username_lc] = st

            if state_changed:
                self.save_last_online_times()
                self.save_logoff_times()
                self.save_offline_records()

    @periodic_check.before_loop
    async def before_periodic(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="check", description="Check a Habbo user's last seen / privacy (www.habbo.com).")
    @app_commands.describe(username="Username")
    async def habbo_check(self, interaction: discord.Interaction, username: str):
        await interaction.response.defer(thinking=True, ephemeral=True)
        user_json = await self.fetch_habbo_user(username)
        if not user_json:
            embed = discord.Embed(
                title="Profile Not Found",
                description=f"## Habbo: {username}\n## Last Seen: Unknown\n## Details: No public profile found or invalid username.",
                colour=discord.Colour.red(),
                timestamp=datetime.now(timezone.utc),
            )
            embed.set_footer(text=f"{self.bot.user.name} - Siren - Noah")
            await self.notify_user(embed)
            await interaction.followup.send("Check Complete", ephemeral=True)
            return

        # Slash command checks are ad-hoc lookups without guaranteed group membership.
        # We default to MOD policy for neutral display; no offline milestone fires here
        # because this command intentionally passes offline_since_dt=None.
        embed, _is_online, alert_key, _name, _avatar_url = self.evaluate_user(user_json, username, None, "MOD")
        if alert_key:
            await self.notify_user(embed)

        await interaction.followup.send("Check Complete", ephemeral=True)

    # Slash-only reporting command so staff can use Discord autocomplete/ephemeral responses.
    @app_commands.command(name="offline_times", description="Show recorded offline times for specific Habbo users.")
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

async def setup(bot: commands.Bot):
    await bot.add_cog(HabboWatch(bot))
