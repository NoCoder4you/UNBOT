"""Discord command for checking Habbo username availability and close variants."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import quote

import aiohttp
import discord
from discord.ext import commands


LOGGER = logging.getLogger(__name__)
HABBO_API_ROOT = "https://www.habbo.com/api/public/users"
DATAMUSE_API_ROOT = "https://api.datamuse.com/words"
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{2,15}$")
MAX_CLOSE_MATCHES = 10
MAX_SYNONYMS_PER_WORD = 5


class HabboUsernameFinder(commands.Cog):
    """Check a requested Habbo name and a small set of similar names."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        # This fallback is used only when the profile watcher is not loaded.
        # Ordinarily both cogs use the watcher's existing shared API gate.
        self._api_request_lock = asyncio.Lock()
        self._next_api_request_at = 0.0

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
    def username_words(username: str) -> list[tuple[int, int, str]]:
        """Locate ordinary and CamelCase words while retaining replacement spans."""
        pattern = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])")
        return [(match.start(), match.end(), match.group()) for match in pattern.finditer(username)]

    async def fetch_synonyms(self, word: str) -> list[str]:
        """Look up current synonyms instead of relying on a hard-coded word list."""
        try:
            async with self.session.get(
                DATAMUSE_API_ROOT,
                params={"rel_syn": word.lower(), "max": MAX_SYNONYMS_PER_WORD},
            ) as response:
                if response.status != 200:
                    LOGGER.warning("Synonym lookup returned HTTP %s for %s", response.status, word)
                    return []
                payload = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            LOGGER.warning("Synonym lookup failed for %s: %s", word, exc)
            return []

        # Datamuse returns objects with a `word` property. Multi-word results
        # cannot form a valid Habbo username segment, so they are discarded.
        return [
            item["word"]
            for item in payload
            if isinstance(item, dict)
            and isinstance(item.get("word"), str)
            and re.fullmatch(r"[A-Za-z]+", item["word"])
        ][:MAX_SYNONYMS_PER_WORD]

    async def synonym_matches(self, username: str) -> list[str]:
        """Build valid alternatives from live thesaurus results for every word."""
        words = self.username_words(username)
        synonym_lists = await asyncio.gather(*(self.fetch_synonyms(word) for _, _, word in words))
        matches = []
        for (start, end, original), synonyms in zip(words, synonym_lists):
            for synonym in synonyms:
                replacement = synonym.capitalize() if original[0].isupper() else synonym.lower()
                candidate = username[:start] + replacement + username[end:]
                if USERNAME_PATTERN.fullmatch(candidate):
                    matches.append(candidate)
        return matches

    @staticmethod
    def close_matches(
        username: str,
        synonyms: list[str] | None = None,
        limit: int = MAX_CLOSE_MATCHES,
    ) -> list[str]:
        """Create deterministic, valid alternatives that remain recognizably close.

        Semantic alternatives are preferred, followed by suffix, separator,
        prefix, and letter-to-number edits. A case-insensitive set prevents
        Habbo's case-insensitive names from being checked more than once.
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

        for synonym in synonyms or []:
            add(synonym)
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

    async def find_suggestions(self, username: str) -> list[str]:
        """Look up semantic alternatives, then fill remaining suggestion slots."""
        synonyms = await self.synonym_matches(username)
        return self.close_matches(username, synonyms)

    def _watcher(self):
        """Find the watcher that owns the process-wide Habbo request schedule."""
        get_cog = getattr(getattr(self, "bot", None), "get_cog", None)
        return get_cog("HabboWatch") if get_cog else None

    async def wait_for_api_request_slot(self) -> None:
        """Use the watcher's pacing gate, or an equivalent local fallback."""
        watcher = self._watcher()
        if watcher is not None:
            await watcher.wait_for_api_request_slot()
            return
        async with self._api_request_lock:
            delay = self._next_api_request_at - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_api_request_at = time.monotonic() + 1.0

    def delay_api_requests(self, retry_after: float) -> None:
        """Apply a server-requested cooldown to the shared or fallback gate."""
        watcher = self._watcher()
        if watcher is not None:
            watcher.delay_api_requests(retry_after)
            return
        self._next_api_request_at = max(
            self._next_api_request_at,
            time.monotonic() + max(1.0, retry_after),
        )

    async def check_username(self, username: str) -> str:
        """Return ``available``, ``taken``, or ``unknown`` for one Habbo name.

        Habbo returns 404 when no user owns a name. Other failures are kept as
        unknown so temporary API trouble is never presented as availability.
        """
        url = f"{HABBO_API_ROOT}?name={quote(username, safe='')}"
        try:
            await self.wait_for_api_request_slot()
            async with self.session.get(url) as response:
                if response.status == 404:
                    return "available"
                if response.status == 200:
                    return "taken"
                if response.status == 429:
                    try:
                        retry_after = float(response.headers.get("retry-after", "1"))
                    except (TypeError, ValueError):
                        retry_after = 1.0
                    self.delay_api_requests(retry_after)
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
        names = [normalized, *await self.find_suggestions(normalized)]
        # Keep lookups sequential: each one passes through the same one-request-
        # per-second gate used by watcher scans, preserving the shared API budget.
        statuses = [await self.check_username(name) for name in names]
        await ctx.send(
            embed=self.build_results_embed(normalized, list(zip(names, statuses))),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(HabboUsernameFinder(bot))
