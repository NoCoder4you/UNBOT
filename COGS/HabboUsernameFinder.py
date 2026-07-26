"""Discord command for checking Habbo username availability and close variants."""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote

import aiohttp
import discord
from discord.ext import commands


LOGGER = logging.getLogger(__name__)
HABBO_API_ROOT = "https://www.habbo.com/api/public/users"
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{2,15}$")
MAX_CLOSE_MATCHES = 10


class HabboUsernameFinder(commands.Cog):
    """Check a requested Habbo name and a small set of similar names."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))

    async def cog_unload(self):
        await self.session.close()

    @staticmethod
    def normalize_username(username: str) -> str:
        """Strip a name and reject values that cannot be Habbo usernames."""
        normalized = username.strip()
        if not USERNAME_PATTERN.fullmatch(normalized):
            raise ValueError(
                "Habbo usernames must be 2–15 characters and use only letters, "
                "numbers, periods, underscores, or hyphens."
            )
        return normalized

    @staticmethod
    def close_matches(username: str, limit: int = MAX_CLOSE_MATCHES) -> list[str]:
        """Create deterministic, valid alternatives that remain recognizably close.

        Suggestions favor small suffix and separator edits, followed by common
        letter-to-number substitutions. A case-insensitive set prevents Habbo's
        case-insensitive names from being checked more than once.
        """
        candidates: list[str] = []
        seen = {username.casefold()}

        def add(candidate: str) -> None:
            if (
                len(candidates) < limit
                and USERNAME_PATTERN.fullmatch(candidate)
                and candidate.casefold() not in seen
            ):
                seen.add(candidate.casefold())
                candidates.append(candidate)

        for suffix in ("1", "2", "3", "_", "-", "."):
            # Make space for the edit when the requested name is already at the limit.
            add(username[: 15 - len(suffix)] + suffix)
        for prefix in ("x", "i"):
            add(prefix + username[:14])
        for old, new in (("a", "4"), ("e", "3"), ("i", "1"), ("o", "0"), ("s", "5")):
            index = username.lower().find(old)
            if index >= 0:
                add(username[:index] + new + username[index + 1 :])
        return candidates

    async def check_username(self, username: str) -> str:
        """Return ``available``, ``taken``, or ``unknown`` for one Habbo name.

        Habbo returns 404 when no user owns a name. Other failures are kept as
        unknown so temporary API trouble is never presented as availability.
        """
        url = f"{HABBO_API_ROOT}?name={quote(username, safe='')}"
        try:
            async with self.session.get(url) as response:
                if response.status == 404:
                    return "available"
                if response.status == 200:
                    return "taken"
                LOGGER.warning("Habbo username lookup returned HTTP %s for %s", response.status, username)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            LOGGER.warning("Habbo username lookup failed for %s: %s", username, exc)
        return "unknown"

    @staticmethod
    def build_results_embed(username: str, results: list[tuple[str, str]]) -> discord.Embed:
        """Format exact and close-match results in a compact Discord embed."""
        symbols = {"available": "✅", "taken": "❌", "unknown": "⚠️"}
        exact_status = results[0][1]
        embed = discord.Embed(
            title=f"Habbo username: {username}",
            description=f"{symbols[exact_status]} **{exact_status.title()}** at the time of this check.",
            colour=discord.Colour.green() if exact_status == "available" else discord.Colour.blurple(),
        )
        alternatives = [
            f"{symbols[status]} `{candidate}` — {status.title()}"
            for candidate, status in results[1:]
        ]
        embed.add_field(
            name="Close matches",
            value="\n".join(alternatives) or "No valid close matches could be generated.",
            inline=False,
        )
        embed.set_footer(text="Availability can change at any time; confirm on Habbo before choosing a name.")
        return embed

    @commands.hybrid_command(name="usernamefinder", description="Check a Habbo username and close matches.")
    async def username_finder(self, ctx: commands.Context, username: str):
        """Check the exact requested username plus ten nearby alternatives."""
        try:
            normalized = self.normalize_username(username)
        except ValueError as exc:
            await ctx.send(str(exc), ephemeral=True)
            return

        await ctx.defer(ephemeral=True)
        names = [normalized, *self.close_matches(normalized)]
        # Run the small batch together so the interaction does not time out.
        statuses = await asyncio.gather(*(self.check_username(name) for name in names))
        await ctx.send(
            embed=self.build_results_embed(normalized, list(zip(names, statuses))),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(HabboUsernameFinder(bot))
