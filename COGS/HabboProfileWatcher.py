import aiohttp
from datetime import datetime, timezone
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
        self.periodic_check.start()

    async def cog_unload(self):
        self.periodic_check.cancel()
        await self.session.close()

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

    def evaluate_user(self, user_json: dict, requested_username: str, threshold_days: int):
        name = user_json.get("name") or requested_username
        online = user_json.get("online", user_json.get("isOnline"))
        profile_visible = user_json.get("profileVisible", user_json.get("isProfileVisible"))
        if profile_visible is None:
            profile_visible = bool(user_json.get("memberSince") or user_json.get("lastAccessTime"))

        last_access_raw = user_json.get("lastAccessTime") or user_json.get("lastAccess") or user_json.get("lastLoggedIn")
        member_since_raw = user_json.get("memberSince")
        last_access_dt = self.parse_iso(last_access_raw)
        member_since_dt = self.parse_iso(member_since_raw)
        days_offline = self.days_since(last_access_dt)

        # Full-body avatar (direction changed to 3)
        figure = user_json.get("figureString") or user_json.get("figure")
        if figure:
            avatar_url = f"https://www.habbo.com/habbo-imaging/avatarimage?figure={figure}&size=l&direction=3&head_direction=3"
        else:
            avatar_url = f"https://www.habbo.com/habbo-imaging/avatarimage?user={name}&size=l&direction=3&head_direction=3"

        unique_id = user_json.get("uniqueId")

        lines = []
        if unique_id:
            # Link using username rather than uniqueId
            lines.append(f"## Habbo: [{name}](https://www.habbo.com/profile/{name})")
        else:
            lines.append(f"## Habbo: {name}")

        if not profile_visible:
            title = "Profile Hidden"
            warn = True
        else:
            last_seen_str = last_access_dt.strftime("%Y-%m-%d %H:%M UTC") if last_access_dt else "Unknown"
            lines.append(f"## Last Seen: {last_seen_str}")
            if online is True:
                warn = False
                title = "Online"
            else:
                if online is False and days_offline is not None:
                    threshold = float(threshold_days)
                    if days_offline >= threshold:
                        warn = True
                        title = "Offline Warning"
                    elif threshold == 3 and days_offline >= 2.5:
                        warn = True
                        title = "Offline Warning (Approaching 3 Days)"
                    else:
                        warn = False
                        title = "Recent Activity"
                else:
                    warn = False
                    title = "Recent Activity"

        if member_since_dt:
            lines.append(f"## Member Since: {member_since_dt.strftime('%Y-%m-%d')}")

        warn_titles = ("Offline Warning", "Offline Warning (Approaching 3 Days)", "Profile Hidden")
        embed = discord.Embed(
            title=title,
            description="\n".join(lines),
            colour=discord.Colour.red() if title in warn_titles else discord.Colour.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.set_thumbnail(url=avatar_url)
        embed.set_footer(text=f"{self.bot.user.name} - Siren - Noah")
        # return online flag and last_access_dt for transition logic
        return warn, embed, bool(online), last_access_dt, name, avatar_url

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
        threshold_map: dict[str, float] = {}

        for group_id, threshold_days in GROUPS:
            members = await self.fetch_group_members(group_id)
            if not members:
                continue
            for username in members:
                key = username.lower()
                # choose the least strict threshold if user appears in multiple groups
                # (e.g., MODs at 3 days should override OOA 1-day rules)
                if key in threshold_map:
                    threshold_map[key] = max(threshold_map[key], float(threshold_days))
                else:
                    threshold_map[key] = float(threshold_days)

        # Now check each unique user once
        for username_lc, threshold in threshold_map.items():
            user_json = await self.fetch_habbo_user(username_lc)
            if not user_json:
                # clear state if they disappear from API
                self._state.pop(username_lc, None)
                continue

            warn, embed, is_online, last_access_dt, name, avatar_url = self.evaluate_user(user_json, username_lc, threshold)

            # previous state
            st = self._state.get(username_lc, {"was_warn": False, "offline_since": None})

            # Send warning if needed
            if warn:
                # set offline_since if first time entering warn state
                if not st["was_warn"]:
                    st["offline_since"] = last_access_dt
                st["was_warn"] = True
                await self.notify_user(embed)
            else:
                # If previously warned and now online -> send "Back Online"
                if st["was_warn"] and is_online:
                    back_embed = self.make_back_online_embed(name, avatar_url, st.get("offline_since"))
                    await self.notify_user(back_embed)
                    # reset state
                    st["was_warn"] = False
                    st["offline_since"] = None
                else:
                    # if no warn and not online, just clear warn flag
                    st["was_warn"] = False

            self._state[username_lc] = st

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

        warn, embed, is_online, last_access_dt, name, avatar_url = self.evaluate_user(user_json, username, 3)
        if warn:
            await self.notify_user(embed)
        else:
            # optional: if they are online now and previously warned in memory, send back-online here too
            st = self._state.get(username.lower(), {"was_warn": False, "offline_since": None})
            if st["was_warn"] and is_online:
                back_embed = self.make_back_online_embed(name, avatar_url, st.get("offline_since"))
                await self.notify_user(back_embed)
                self._state[username.lower()] = {"was_warn": False, "offline_since": None}

        await interaction.followup.send("Check Complete", ephemeral=True)

async def setup(bot: commands.Bot):
    await bot.add_cog(HabboWatch(bot))
