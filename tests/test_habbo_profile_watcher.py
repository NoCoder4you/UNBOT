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

    class EmbedStub:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.title = kwargs.get("title")
            self.description = kwargs.get("description")
            self.fields = []

        def set_thumbnail(self, **kwargs):
            self.thumbnail = kwargs

        def set_footer(self, **kwargs):
            self.footer = kwargs

        def add_field(self, **kwargs):
            self.fields.append(kwargs)

    discord_stub.Embed = EmbedStub
    discord_stub.TextChannel = object
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
    commands_stub.Context = object
    commands_stub.command = lambda *args, **kwargs: (lambda func: func)
    commands_stub.is_owner = lambda *args, **kwargs: (lambda func: func)
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


class HabboManualJsonUpdateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_watcher_module()
        cls.watch_cls = cls.module.HabboWatch

    def make_watch(self):
        watch = self.watch_cls.__new__(self.watch_cls)
        watch.last_online_times = {}
        watch.logoff_times = {}
        watch.offline_records = {}
        watch._state = {"alpha": {"was_online": False}}
        watch.save_last_online_times = lambda: None
        watch.save_logoff_times = lambda: None
        watch.save_offline_records = lambda: None
        return watch

    def test_manual_offline_update_writes_active_json_markers(self):
        watch = self.make_watch()
        message = watch.apply_manual_json_update("Alpha", "offline", "2026-06-17 12:30:00Z", "ooa")

        self.assertIn("Saved Alpha as offline", message)
        self.assertEqual(watch.logoff_times["alpha"], "2026-06-17T12:30:00+00:00")
        self.assertEqual(watch.offline_records["alpha"]["policy"], "OOA")
        self.assertEqual(watch.offline_records["alpha"]["current_offline_since"], "2026-06-17T12:30:00+00:00")
        self.assertNotIn("alpha", watch._state)

    def test_manual_online_update_closes_existing_offline_window(self):
        watch = self.make_watch()
        watch.logoff_times["alpha"] = "2026-06-17T10:00:00+00:00"
        watch.offline_records["alpha"] = {
            "display_name": "Alpha",
            "policy": "MOD",
            "last_seen_online_at": None,
            "current_offline_since": "2026-06-17T10:00:00+00:00",
            "history": [],
        }

        message = watch.apply_manual_json_update("Alpha", "online", "2026-06-17T12:00:00+00:00", "MOD")

        self.assertIn("Saved Alpha as online", message)
        self.assertEqual(watch.last_online_times["alpha"], "2026-06-17T12:00:00+00:00")
        self.assertNotIn("alpha", watch.logoff_times)
        self.assertIsNone(watch.offline_records["alpha"]["current_offline_since"])
        self.assertEqual(watch.offline_records["alpha"]["history"][0]["duration_seconds"], 7200)

    def test_manual_update_rejects_unknown_status(self):
        watch = self.make_watch()
        with self.assertRaises(ValueError):
            watch.apply_manual_json_update("Alpha", "away", None, "MOD")


class FakeAlertDestination:
    def __init__(self):
        self.sent_embeds = []

    async def send(self, embed=None):
        self.sent_embeds.append(embed)


class FakeAlertBot:
    def __init__(self, cached_channel=None, fetched_channel=None, dm_user=None):
        self.cached_channel = cached_channel
        self.fetched_channel = fetched_channel
        self.dm_user = dm_user or FakeAlertDestination()
        self.requested_channel_ids = []

    def get_channel(self, channel_id):
        self.requested_channel_ids.append(("get", channel_id))
        if isinstance(self.cached_channel, dict):
            return self.cached_channel.get(channel_id)
        return self.cached_channel

    async def fetch_channel(self, channel_id):
        self.requested_channel_ids.append(("fetch", channel_id))
        return self.fetched_channel

    async def fetch_user(self, user_id):
        return self.dm_user


class HabboAlertRoutingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_watcher_module()
        cls.watch_cls = cls.module.HabboWatch

    def make_watch(self, bot):
        watch = self.watch_cls.__new__(self.watch_cls)
        watch.bot = bot
        watch.alert_channel_ids = {"MOD": [], "OOA": []}
        watch.save_alert_channel_ids = lambda: None
        return watch

    def test_alert_channel_ids_for_policy_uses_separate_mod_and_ooa_channels(self):
        watch = self.make_watch(FakeAlertBot())
        watch.alert_channel_ids = {"MOD": [111, 112], "OOA": [222]}

        self.assertEqual(watch.alert_channel_ids_for_policy("MOD"), [111, 112])
        self.assertEqual(watch.alert_channel_ids_for_policy("ooa"), [222])

    def test_alert_channel_ids_for_policy_ignores_invalid_channel_ids(self):
        watch = self.make_watch(FakeAlertBot())
        watch.alert_channel_ids = {"MOD": ["not-a-channel"], "OOA": []}

        self.assertEqual(watch.alert_channel_ids_for_policy("MOD"), [])

    def test_notify_user_sends_to_configured_policy_channel(self):
        import asyncio

        channel = FakeAlertDestination()
        bot = FakeAlertBot(cached_channel=channel)
        watch = self.make_watch(bot)
        watch.alert_channel_ids["MOD"] = [333]

        asyncio.run(watch.notify_user("embed-payload", "MOD"))

        self.assertEqual(channel.sent_embeds, ["embed-payload"])
        self.assertEqual(bot.dm_user.sent_embeds, [])
        self.assertEqual(bot.requested_channel_ids, [("get", 333)])

    def test_notify_user_falls_back_to_dm_when_policy_channel_unset(self):
        import asyncio

        bot = FakeAlertBot()
        watch = self.make_watch(bot)
        watch.alert_channel_ids["OOA"] = []

        asyncio.run(watch.notify_user("embed-payload", "OOA"))

        self.assertEqual(bot.dm_user.sent_embeds, ["embed-payload"])
        self.assertEqual(bot.requested_channel_ids, [])

    def test_notify_user_sends_to_all_configured_policy_channels(self):
        import asyncio

        first_channel = FakeAlertDestination()
        second_channel = FakeAlertDestination()
        bot = FakeAlertBot(cached_channel={333: first_channel, 334: second_channel})
        watch = self.make_watch(bot)
        watch.alert_channel_ids["MOD"] = [333, 334]

        asyncio.run(watch.notify_user("embed-payload", "MOD"))

        self.assertEqual(first_channel.sent_embeds, ["embed-payload"])
        self.assertEqual(second_channel.sent_embeds, ["embed-payload"])
        self.assertEqual(bot.dm_user.sent_embeds, [])
        self.assertEqual(bot.requested_channel_ids, [("get", 333), ("get", 334)])

    def test_configure_alert_channels_accepts_multiple_mentions_and_saves_policy(self):
        saved = []
        watch = self.make_watch(FakeAlertBot())
        watch.save_alert_channel_ids = lambda: saved.append(dict(watch.alert_channel_ids))

        channel_ids = watch.configure_alert_channels("ooa", "<#444>, <#445> <#444>")

        self.assertEqual(channel_ids, [444, 445])
        self.assertEqual(watch.alert_channel_ids["OOA"], [444, 445])
        self.assertEqual(saved, [{"MOD": [], "OOA": [444, 445]}])


class HabboPeriodicNotificationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_watcher_module()
        cls.watch_cls = cls.module.HabboWatch

    def make_watch(self, members_by_group, users_by_name):
        watch = self.watch_cls.__new__(self.watch_cls)
        watch.bot = types.SimpleNamespace(user=types.SimpleNamespace(name="TestBot"))
        watch._state = {}
        watch.last_online_times = {}
        watch.logoff_times = {}
        watch.offline_records = {}
        watch.notifications = []
        watch.saved = []

        async def fetch_group_members(group_id):
            return members_by_group.get(group_id, [])

        async def fetch_habbo_user(username):
            return users_by_name.get(username.lower())

        async def notify_user(embed, policy_name=None):
            watch.notifications.append((embed.title, policy_name))

        watch.fetch_group_members = fetch_group_members
        watch.fetch_habbo_user = fetch_habbo_user
        watch.notify_user = notify_user
        watch.save_last_online_times = lambda: watch.saved.append("last_online")
        watch.save_logoff_times = lambda: watch.saved.append("logoff")
        watch.save_offline_records = lambda: watch.saved.append("offline_records")
        return watch

    def run_periodic_once(self, watch):
        import asyncio

        asyncio.run(self.watch_cls.periodic_check.func(watch))

    def test_periodic_check_sends_nothing_when_statuses_do_not_change(self):
        users = {
            "alpha": {"name": "Alpha", "online": False, "profileVisible": True},
            "bravo": {"name": "Bravo", "online": True, "profileVisible": True},
        }
        watch = self.make_watch(
            {self.module.MOD_GROUP_ID: ["Alpha", "Bravo"], self.module.OOA_GROUP_ID: []},
            users,
        )

        self.run_periodic_once(watch)
        self.run_periodic_once(watch)

        self.assertEqual(watch.notifications, [])

    def test_periodic_check_checks_every_unique_member_once(self):
        checked = []
        users = {
            "alpha": {"name": "Alpha", "online": True, "profileVisible": True},
            "bravo": {"name": "Bravo", "online": False, "profileVisible": True},
            "charlie": {"name": "Charlie", "online": True, "profileVisible": True},
        }
        watch = self.make_watch(
            {self.module.MOD_GROUP_ID: ["Alpha", "Bravo"], self.module.OOA_GROUP_ID: ["Bravo", "Charlie"]},
            users,
        )

        async def fetch_habbo_user(username):
            checked.append(username)
            return users[username.lower()]

        watch.fetch_habbo_user = fetch_habbo_user
        self.run_periodic_once(watch)

        self.assertEqual(set(checked), {"alpha", "bravo", "charlie"})
        self.assertEqual(len(checked), 3)
        self.assertEqual(watch._state["bravo"]["was_online"], False)

    def test_periodic_check_notifies_once_when_user_goes_offline(self):
        users = {"alpha": {"name": "Alpha", "online": True, "profileVisible": True}}
        watch = self.make_watch({self.module.MOD_GROUP_ID: ["Alpha"], self.module.OOA_GROUP_ID: []}, users)

        self.run_periodic_once(watch)
        users["alpha"] = {"name": "Alpha", "online": False, "profileVisible": True}
        self.run_periodic_once(watch)
        self.run_periodic_once(watch)

        self.assertEqual(watch.notifications, [("Recent Activity", "MOD")])

    def test_periodic_check_notifies_when_baseline_offline_user_comes_online(self):
        users = {"alpha": {"name": "Alpha", "online": False, "profileVisible": True}}
        watch = self.make_watch({self.module.MOD_GROUP_ID: ["Alpha"], self.module.OOA_GROUP_ID: []}, users)

        self.run_periodic_once(watch)
        users["alpha"] = {"name": "Alpha", "online": True, "profileVisible": True}
        self.run_periodic_once(watch)
        self.run_periodic_once(watch)

        self.assertEqual(watch.notifications, [("Back Online", "MOD")])


if __name__ == "__main__":
    unittest.main()
