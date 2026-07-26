"""Tests for Habbo username validation, suggestions, and API interpretation."""

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest


def load_module():
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientSession = object
    aiohttp.ClientTimeout = lambda **kwargs: kwargs
    aiohttp.ClientError = type("ClientError", (Exception,), {})

    class Embed:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.fields = []

        def add_field(self, **kwargs):
            self.fields.append(kwargs)

        def set_footer(self, **kwargs):
            self.footer = kwargs

    discord = types.ModuleType("discord")
    discord.Embed = Embed
    discord.Colour = types.SimpleNamespace(green=lambda: "green", blurple=lambda: "blurple")
    commands = types.ModuleType("discord.ext.commands")
    commands.Cog = object
    commands.Bot = object
    commands.Context = object
    commands.BucketType = types.SimpleNamespace(user="user")
    commands.hybrid_command = lambda *args, **kwargs: lambda function: function
    commands.cooldown = lambda *args, **kwargs: lambda function: function
    ext = types.ModuleType("discord.ext")
    ext.commands = commands
    sys.modules.update({"aiohttp": aiohttp, "discord": discord, "discord.ext": ext, "discord.ext.commands": commands})

    path = Path(__file__).resolve().parents[1] / "COGS" / "HabboUsernameFinder.py"
    spec = importlib.util.spec_from_file_location("habbo_username_finder_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Response:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None


class Session:
    def __init__(self, status):
        self.status = status
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return Response(self.status)


class HabboUsernameFinderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()
        cls.finder = cls.module.HabboUsernameFinder

    def test_normalize_username_accepts_valid_name(self):
        self.assertEqual(self.finder.normalize_username("  Name-1  "), "Name-1")

    def test_normalize_username_rejects_invalid_names(self):
        for name in ("x", "has space", "name!", "a" * 16):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.finder.normalize_username(name)

    def test_close_matches_are_unique_valid_and_bounded(self):
        matches = self.finder.close_matches("Example")
        self.assertLessEqual(len(matches), self.module.MAX_CLOSE_MATCHES)
        self.assertEqual(len(matches), len({name.casefold() for name in matches}))
        self.assertNotIn("example", {name.casefold() for name in matches})
        self.assertTrue(all(self.module.USERNAME_PATTERN.fullmatch(name) for name in matches))
        self.assertIn("Example1", matches)
        self.assertIn("3xample", matches)

    def test_check_username_maps_http_status_without_guessing_on_errors(self):
        for status, expected in ((200, "taken"), (404, "available"), (500, "unknown")):
            with self.subTest(status=status):
                finder = self.finder.__new__(self.finder)
                finder.session = Session(status)
                self.assertEqual(asyncio.run(finder.check_username("Name-1")), expected)
                self.assertEqual(finder.session.urls, [self.module.HABBO_API_ROOT + "?name=Name-1"])

    def test_build_results_embed_labels_exact_and_close_results(self):
        embed = self.finder.build_results_embed(
            "Example", [("Example", "taken"), ("Example1", "available"), ("Example2", "unknown")]
        )
        self.assertIn("Taken", embed.kwargs["description"])
        self.assertIn("`Example1` — Available", embed.fields[0]["value"])
        self.assertIn("`Example2` — Unknown", embed.fields[0]["value"])


if __name__ == "__main__":
    unittest.main()
