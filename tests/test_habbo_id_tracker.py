"""Focused unit tests for Habbo ID validation and change detection."""

import importlib.util
from pathlib import Path
import sys
import types
import unittest


def load_tracker_module():
    """Load the cog with small dependency stubs for its pure helper tests."""
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientSession = object
    aiohttp.ClientTimeout = lambda **kwargs: kwargs
    aiohttp.ClientError = Exception

    discord = types.ModuleType("discord")
    discord.TextChannel = object
    discord.AllowedMentions = object
    discord.HTTPException = type("HTTPException", (Exception,), {})
    discord.NotFound = type("NotFound", (Exception,), {})
    discord.Forbidden = type("Forbidden", (Exception,), {})

    commands = types.ModuleType("discord.ext.commands")
    commands.Cog = object
    commands.Bot = object
    commands.Context = object
    commands.hybrid_command = lambda *args, **kwargs: (lambda function: function)
    commands.is_owner = lambda *args, **kwargs: (lambda function: function)

    class LoopStub:
        def __init__(self, function):
            self.function = function

        def start(self):
            pass

        def cancel(self):
            pass

        def before_loop(self, function):
            return function

    tasks = types.ModuleType("discord.ext.tasks")
    tasks.loop = lambda *args, **kwargs: (lambda function: LoopStub(function))
    ext = types.ModuleType("discord.ext")
    ext.commands = commands
    ext.tasks = tasks
    sys.modules.update({"aiohttp": aiohttp, "discord": discord, "discord.ext": ext,
                        "discord.ext.commands": commands, "discord.ext.tasks": tasks})

    path = Path(__file__).resolve().parents[1] / "COGS" / "HabboIdTracker.py"
    spec = importlib.util.spec_from_file_location("habbo_id_tracker_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HabboIdTracker = load_tracker_module().HabboIdTracker


class HabboIdTrackerHelpersTest(unittest.TestCase):
    def test_normalize_habbo_id_accepts_supplied_id(self):
        self.assertEqual(
            HabboIdTracker.normalize_habbo_id(" HHUS-452093bfeba8168bb70ea408bea12112 "),
            "hhus-452093bfeba8168bb70ea408bea12112",
        )

    def test_normalize_habbo_id_rejects_names_and_urls(self):
        for invalid in ("Noah", "hhus-short", "https://www.habbo.com/profile/test"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                HabboIdTracker.normalize_habbo_id(invalid)

    def test_profile_snapshot_tracks_public_scalar_properties(self):
        profile = {
            "uniqueId": "hhus-example",
            "name": "Before",
            "motto": "Hello",
            "profileVisible": True,
            "online": False,
            "newApiProperty": 42,
            "badges": [{"code": "TEST"}],
        }

        self.assertEqual(
            HabboIdTracker.profile_snapshot(profile),
            {
                "name": "Before",
                "motto": "Hello",
                "profileVisible": True,
                "online": False,
                "newApiProperty": 42,
            },
        )

    def test_compare_snapshots_reports_name_motto_visibility_and_other_changes(self):
        old = {"name": "Before", "motto": "Old", "profileVisible": True, "level": 1}
        new = {"name": "After", "motto": "New", "profileVisible": False, "level": 2}

        changes = HabboIdTracker.compare_snapshots(old, new)

        self.assertEqual(changes["name"], {"old": "Before", "new": "After"})
        self.assertEqual(changes["motto"], {"old": "Old", "new": "New"})
        self.assertEqual(changes["profileVisible"], {"old": True, "new": False})
        self.assertEqual(changes["level"], {"old": 1, "new": 2})

    def test_unchanged_snapshots_have_no_differences(self):
        snapshot = {"name": "Same", "motto": "Still the same"}
        self.assertEqual(HabboIdTracker.compare_snapshots(snapshot, snapshot.copy()), {})


if __name__ == "__main__":
    unittest.main()
