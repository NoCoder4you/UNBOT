"""Track Habbo profiles by unique ID and announce profile changes.

The tracker deliberately uses Habbo's immutable unique ID rather than a name,
so a rename does not cause the bot to lose the profile it is watching. Runtime
data is kept under ``JSON/`` and can safely survive bot restarts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import re
from typing import Any

import aiohttp
import discord
from discord.ext import commands, tasks


LOGGER = logging.getLogger(__name__)
DEFAULT_CHANNEL_ID = 1528811302087032954
DEFAULT_MENTION_USER_ID = 298121351871594497
CHECK_INTERVAL_MINUTES = 5
HABBO_ID_PATTERN = re.compile(r"^[a-z]{2,5}-[a-f0-9]{16,64}$", re.IGNORECASE)

# API properties that are useful to humans and stable enough to compare. New
# simple API properties are also captured by profile_snapshot, making "etc."
# changes visible without requiring a release for every Habbo API addition.
FIELD_LABELS = {
    "name": "Name",
    "motto": "Motto",
    "profileVisible": "Profile visibility",
    "online": "Online status",
    "lastAccessTime": "Last access",
    "memberSince": "Member since",
    "figureString": "Avatar",
    "currentLevel": "Level",
    "currentLevelCompletePercent": "Level progress",
    "totalExperience": "Experience",
    "starGemCount": "Star gems",
}
IGNORED_PROPERTIES = {"uniqueId", "selectedBadges", "groups", "badges"}


class HabboIdTracker(commands.Cog):
    """JSON-backed polling tracker for Habbo unique IDs."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        self._scan_lock = asyncio.Lock()
        root = Path(__file__).resolve().parent.parent / "JSON"
        self.ids_file = root / "habbo_tracked_ids.json"
        self.snapshots_file = root / "habbo_id_snapshots.json"
        self.changes_file = root / "habbo_id_changes.json"
        self.config_file = root / "habbo_id_tracker_config.json"
        self.tracked_ids = self._load_json(self.ids_file, {})
        self.snapshots = self._load_json(self.snapshots_file, {})
        self.changes = self._load_json(self.changes_file, [])
        self.config = self._load_json(
            self.config_file,
            {"channel_id": DEFAULT_CHANNEL_ID, "mention_user_id": DEFAULT_MENTION_USER_ID},
        )
        self.profile_check.start()

    async def cog_unload(self):
        self.profile_check.cancel()
        await self.session.close()

    @staticmethod
    def _load_json(path: Path, default: Any) -> Any:
        """Load JSON, creating a correctly shaped file on a fresh install."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            HabboIdTracker._save_json(path, default)
            return default.copy() if isinstance(default, (dict, list)) else default
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if type(value) is type(default):
                return value
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Could not read Habbo tracker JSON file %s", path)
        return default.copy() if isinstance(default, (dict, list)) else default

    @staticmethod
    def _save_json(path: Path, value: Any) -> None:
        """Atomically replace a JSON file to avoid half-written state files."""
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def normalize_habbo_id(habbo_id: str) -> str:
        """Validate and normalize a Habbo unique ID supplied in Discord."""
        normalized = habbo_id.strip().lower()
        if not HABBO_ID_PATTERN.fullmatch(normalized):
            raise ValueError("That does not look like a Habbo ID (example: `hhus-452093bfeba8168bb70ea408bea12112`).")
        return normalized

    @staticmethod
    def profile_snapshot(profile: dict[str, Any]) -> dict[str, Any]:
        """Return comparable public scalar properties from an API response."""
        snapshot = {}
        for key, value in profile.items():
            if key not in IGNORED_PROPERTIES and (value is None or isinstance(value, (str, int, float, bool))):
                snapshot[key] = value
        return snapshot

    @staticmethod
    def compare_snapshots(old: dict[str, Any], new: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Describe every added, removed, or modified public property."""
        return {
            key: {"old": old.get(key), "new": new.get(key)}
            for key in sorted(old.keys() | new.keys())
            if old.get(key) != new.get(key)
        }

    async def fetch_profile(self, habbo_id: str) -> dict[str, Any] | None:
        """Fetch one US Habbo profile; None means unavailable/non-public."""
        url = f"https://www.habbo.com/api/public/users/{habbo_id}"
        try:
            async with self.session.get(url) as response:
                if response.status == 200:
                    payload = await response.json()
                    return payload if isinstance(payload, dict) else None
                if response.status not in (404, 403):
                    LOGGER.warning("Habbo returned HTTP %s for %s", response.status, habbo_id)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            LOGGER.warning("Habbo lookup failed for %s: %s", habbo_id, exc)
        return None

    @staticmethod
    def _display_value(value: Any) -> str:
        if isinstance(value, bool):
            return "Visible/Yes" if value else "Hidden/No"
        if value is None or value == "":
            return "Not set"
        return str(value)

    def build_change_embed(self, habbo_id: str, profile: dict[str, Any], differences: dict[str, dict[str, Any]]) -> discord.Embed:
        """Build one compact embed containing all changes from a scan."""
        name = profile.get("name") or self.tracked_ids.get(habbo_id, {}).get("name") or "Unknown Habbo"
        embed = discord.Embed(
            title=f"Habbo profile changed: {name}",
            description=f"Unique ID: `{habbo_id}`",
            colour=discord.Colour.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        avatar = profile.get("figureString")
        if avatar:
            embed.set_thumbnail(url=f"https://www.habbo.com/habbo-imaging/avatarimage?figure={avatar}&size=l")
        for key, values in list(differences.items())[:25]:
            label = FIELD_LABELS.get(key, key.replace("_", " ").title())
            old_value = self._display_value(values["old"])
            new_value = self._display_value(values["new"])
            embed.add_field(name=label, value=f"**Before:** {old_value[:450]}\n**Now:** {new_value[:450]}", inline=False)
        embed.set_footer(text=f"{len(differences)} change(s) detected")
        return embed

    async def _notification_channel(self):
        channel_id = int(self.config.get("channel_id", DEFAULT_CHANNEL_ID))
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                LOGGER.exception("Cannot access configured Habbo tracker channel %s", channel_id)
                return None
        return channel

    async def scan_profiles(self) -> int:
        """Scan all IDs, persist snapshots/history, and post changed profiles."""
        notifications = 0
        async with self._scan_lock:
            for habbo_id in list(self.tracked_ids):
                profile = await self.fetch_profile(habbo_id)
                if profile is None:
                    # Network/API failures must not be mistaken for profile privacy changes.
                    continue
                new_snapshot = self.profile_snapshot(profile)
                old_snapshot = self.snapshots.get(habbo_id)
                self.snapshots[habbo_id] = new_snapshot
                self.tracked_ids[habbo_id]["name"] = profile.get("name")
                if old_snapshot is None:
                    continue
                differences = self.compare_snapshots(old_snapshot, new_snapshot)
                if not differences:
                    continue
                detected_at = datetime.now(timezone.utc).isoformat()
                self.changes.append({"habbo_id": habbo_id, "detected_at": detected_at, "changes": differences})
                channel = await self._notification_channel()
                if channel:
                    mention_id = int(self.config.get("mention_user_id", DEFAULT_MENTION_USER_ID))
                    await channel.send(
                        content=f"<@{mention_id}>",
                        embed=self.build_change_embed(habbo_id, profile, differences),
                        allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                    )
                    notifications += 1
            # Bound history size while retaining a useful audit trail on disk.
            self.changes = self.changes[-5000:]
            self._save_json(self.ids_file, self.tracked_ids)
            self._save_json(self.snapshots_file, self.snapshots)
            self._save_json(self.changes_file, self.changes)
        return notifications

    @tasks.loop(minutes=CHECK_INTERVAL_MINUTES)
    async def profile_check(self):
        await self.scan_profiles()

    @profile_check.before_loop
    async def before_profile_check(self):
        await self.bot.wait_until_ready()

    @commands.hybrid_command(name="habboidadd", description="Start tracking a Habbo unique ID.")
    @commands.is_owner()
    async def add_habbo_id(self, ctx: commands.Context, habbo_id: str):
        """Add an ID using either /habboidadd or the text command."""
        try:
            normalized = self.normalize_habbo_id(habbo_id)
        except ValueError as exc:
            await ctx.send(str(exc), ephemeral=True)
            return
        if normalized in self.tracked_ids:
            await ctx.send(f"`{normalized}` is already tracked.", ephemeral=True)
            return
        profile = await self.fetch_profile(normalized)
        if profile is None:
            await ctx.send("I could not find a public Habbo profile with that ID.", ephemeral=True)
            return
        self.tracked_ids[normalized] = {"name": profile.get("name"), "added_at": datetime.now(timezone.utc).isoformat()}
        self.snapshots[normalized] = self.profile_snapshot(profile)
        self._save_json(self.ids_file, self.tracked_ids)
        self._save_json(self.snapshots_file, self.snapshots)
        await ctx.send(f"Now tracking **{profile.get('name', 'Unknown')}** (`{normalized}`).", ephemeral=True)

    @commands.hybrid_command(name="habboidremove", description="Stop tracking a Habbo unique ID.")
    @commands.is_owner()
    async def remove_habbo_id(self, ctx: commands.Context, habbo_id: str):
        """Remove an ID while retaining its historical change log."""
        try:
            normalized = self.normalize_habbo_id(habbo_id)
        except ValueError as exc:
            await ctx.send(str(exc), ephemeral=True)
            return
        if self.tracked_ids.pop(normalized, None) is None:
            await ctx.send(f"`{normalized}` is not tracked.", ephemeral=True)
            return
        self.snapshots.pop(normalized, None)
        self._save_json(self.ids_file, self.tracked_ids)
        self._save_json(self.snapshots_file, self.snapshots)
        await ctx.send(f"Stopped tracking `{normalized}`.", ephemeral=True)

    @commands.hybrid_command(name="habboidlist", description="List every tracked Habbo unique ID.")
    async def list_habbo_ids(self, ctx: commands.Context):
        """List IDs with the latest known name."""
        if not self.tracked_ids:
            await ctx.send("No Habbo IDs are currently tracked.", ephemeral=True)
            return
        lines = [f"• **{item.get('name') or 'Unknown'}** — `{habbo_id}`" for habbo_id, item in self.tracked_ids.items()]
        await ctx.send("\n".join(lines)[:2000], ephemeral=True)

    @commands.hybrid_command(name="habboidchannel", description="Set the channel for Habbo ID change alerts.")
    @commands.is_owner()
    async def set_habbo_channel(self, ctx: commands.Context, channel: discord.TextChannel | None = None):
        """Configure alert routing; without an argument, use the current channel."""
        destination = channel or ctx.channel
        self.config["channel_id"] = destination.id
        self._save_json(self.config_file, self.config)
        await ctx.send(f"Habbo ID changes will be posted in <#{destination.id}>.", ephemeral=True)

    @commands.hybrid_command(name="habboidcheck", description="Check all tracked Habbo IDs now.")
    @commands.is_owner()
    async def check_habbo_ids(self, ctx: commands.Context):
        """Allow operators to run the same scan used by the background task."""
        await ctx.defer(ephemeral=True)
        count = await self.scan_profiles()
        await ctx.send(f"Check complete; posted {count} change notification(s).", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(HabboIdTracker(bot))
