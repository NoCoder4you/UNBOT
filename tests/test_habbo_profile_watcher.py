import importlib.util
import sys
import types
import unittest
from pathlib import Path


def load_watcher_module():
    """Load the cog with lightweight discord stubs so pure helpers can be tested."""
    aiohttp_stub = types.ModuleType("aiohttp")
    aiohttp_stub.ClientSession = object
    sys.modules["aiohttp"] = aiohttp_stub

    discord_stub = types.ModuleType("discord")
    discord_stub.Embed = object
    discord_stub.Colour = types.SimpleNamespace(
        blurple=lambda: "blurple",
        red=lambda: "red",
        green=lambda: "green",
    )
    discord_stub.Interaction = object
    app_commands_stub = types.ModuleType("discord.app_commands")
    app_commands_stub.command = lambda *args, **kwargs: (lambda func: func)
    app_commands_stub.describe = lambda *args, **kwargs: (lambda func: func)

    ext_stub = types.ModuleType("discord.ext")
    commands_stub = types.ModuleType("discord.ext.commands")
    commands_stub.Cog = object
    commands_stub.Bot = object
    tasks_stub = types.ModuleType("discord.ext.tasks")

    class LoopStub:
        def __init__(self, func):
            self.func = func

        def __get__(self, instance, owner):
            return self

        def start(self):
            pass

        def cancel(self):
            pass

        def before_loop(self, func):
            return func

    tasks_stub.loop = lambda *args, **kwargs: (lambda func: LoopStub(func))

    sys.modules.update(
        {
            "discord": discord_stub,
            "discord.app_commands": app_commands_stub,
            "discord.ext": ext_stub,
            "discord.ext.commands": commands_stub,
            "discord.ext.tasks": tasks_stub,
        }
    )
    discord_stub.app_commands = app_commands_stub
    ext_stub.commands = commands_stub
    ext_stub.tasks = tasks_stub

    module_path = Path(__file__).resolve().parents[1] / "COGS" / "HabboProfileWatcher.py"
    spec = importlib.util.spec_from_file_location("habbo_profile_watcher_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HabboGroupMemberHelpersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_watcher_module()
        cls.watch = cls.module.HabboWatch

    def test_extract_group_member_names_supports_known_shapes(self):
        bare_list = [{"name": "Alpha"}, {"habboName": "Bravo"}, {"username": "Charlie"}, {"id": 1}]
        wrapped = {"members": [{"name": "Delta"}]}
        self.assertEqual(self.watch.extract_group_member_names(bare_list), ["Alpha", "Bravo", "Charlie"])
        self.assertEqual(self.watch.extract_group_member_names(wrapped), ["Delta"])

    def test_group_members_has_next_page_uses_metadata_or_full_page(self):
        self.assertTrue(self.watch.group_members_has_next_page({"totalPages": 3}, 2, 40, 100))
        self.assertFalse(self.watch.group_members_has_next_page({"totalPages": 3}, 3, 40, 100))
        self.assertTrue(self.watch.group_members_has_next_page({"hasMore": True}, 1, 5, 100))
        self.assertTrue(self.watch.group_members_has_next_page([{}] * 100, 1, 100, 100))
        self.assertFalse(self.watch.group_members_has_next_page([{}] * 99, 1, 99, 100))


if __name__ == "__main__":
    unittest.main()
