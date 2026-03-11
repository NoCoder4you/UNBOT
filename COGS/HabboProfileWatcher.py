import aiohttp
from datetime import datetime, timezone
import json
from pathlib import Path
import discord
from discord import app_commands
from discord.ext import commands, tasks

NOTIFY_USER_ID = 298121351871594497  # DM recipient

# Groups to watch: (GROUP_ID, THRESHOLD_DAYS)
GROUPS = [
    ("g-hhus-eb463e25366b3796072507bc69cbfee4", 3),
    ("g-hhus-1685c3902d4ce5c8a4fcefa160fedaa2", 1),
]

class HabboWatch(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = aiohttp.ClientSession()
        self._state: dict[str, dict] = {}
        # Resolve JSON storage from the bot root (..../UNBOT/JSON) even though this cog lives in COGS/.
        bot_root = Path(__file__).resolve().parent.parent
        self.last_online_file = bot_root / "JSON" / "habbo_last_online.json"
        self.logoff_file = bot_root / "JSON" / "habbo_logoff_times.json"
        self.last_online_times = self.load_last_online_times()
        self.logoff_times = self.load_logoff_times()
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

    def evaluate_user(
        self,
        user_json: dict,
        requested_username: str,
        offline_since_dt: datetime | None,
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
                lines.append(f"## Last Seen Online: {last_seen_str}")

                # Milestones requested by user: 2.0d, 2.5d, 3.0d
                # We use these as one-shot alert keys so each is notified only once.
                if days_offline is not None and days_offline >= 3.0:
                    title = "Offline Warning"
                    alert_key = "offline_3.0"
                elif days_offline is not None and days_offline >= 2.5:
                    title = "Offline Warning (Approaching 3 Days)"
                    alert_key = "offline_2.5"
                elif days_offline is not None and days_offline >= 2.0:
                    title = "Offline Notice (2 Days)"
                    alert_key = "offline_2.0"
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
            # Show when they were last seen (offline start) and how long until now
            lines.append(f"## Was Offline Since: <t:{unix_then}:F>")
            lines.append(f"## Back Online: <t:{unix_now}:F>")
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

    @tasks.loop(minutes=10)
    async def periodic_check(self):
        # Build a username -> threshold map, de-duping across groups
        # (threshold currently unused because alerts are fixed at 2/2.5/3 days).
        threshold_map: dict[str, float] = {}

        for group_id, threshold_days in GROUPS:
            members = await self.fetch_group_members(group_id)
            if not members:
                continue
            for username in members:
                key = username.lower()
                if key in threshold_map:
                    threshold_map[key] = max(threshold_map[key], float(threshold_days))
                else:
                    threshold_map[key] = float(threshold_days)

        # Check each unique user once.
        for username_lc in threshold_map:
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

            # Transition flags are used to reset tracking only when state changes,
            # preventing repeated alerts while status is unchanged.
            went_online = previous_online is False and is_online
            went_offline = previous_online is True and (not is_online)
            went_offline_at = st.get("offline_since")
            state_changed = False

            # Track only observed online->offline transitions.
            if is_online:
                # Continuously refresh last-online timestamp while online so it is durable across restarts.
                self.last_online_times[username_lc] = datetime.now(timezone.utc).isoformat()
                state_changed = True

            if went_offline:
                # Start offline tracking from the last observed online timestamp stored on disk.
                # If that value is missing/corrupt, fall back to now to keep tracking functional.
                persisted_last_online = self.parse_iso(self.last_online_times.get(username_lc))
                st["offline_since"] = persisted_last_online or datetime.now(timezone.utc)
                st["sent_alerts"] = set()

                # Persist an explicit logoff timestamp for the active->offline transition.
                self.logoff_times[username_lc] = datetime.now(timezone.utc).isoformat()
                state_changed = True
            elif went_online:
                # Returning online ends the current offline tracking window.
                st["offline_since"] = None
                st["sent_alerts"] = set()

                # Clear last logoff marker once they are active again.
                self.logoff_times.pop(username_lc, None)
                state_changed = True

            embed, _, alert_key, name, avatar_url = self.evaluate_user(
                user_json,
                username_lc,
                st.get("offline_since"),
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

        embed, _is_online, alert_key, _name, _avatar_url = self.evaluate_user(user_json, username, None)
        if alert_key:
            await self.notify_user(embed)

        await interaction.followup.send("Check Complete", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(HabboWatch(bot))
