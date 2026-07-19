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


    def test_fetch_user_policy_map_preserves_roster_casing_for_lookup(self):
        import asyncio

        watch = self.watch.__new__(self.watch)

        async def fetch_group_members(group_id):
            if group_id == self.module.MOD_GROUP_ID:
                return ["iLegendaryGOAT"]
            return []

        watch.fetch_group_members = fetch_group_members

        self.assertEqual(
            asyncio.run(watch.fetch_user_policy_map()),
            {"ilegendarygoat": ("iLegendaryGOAT", "MOD")},
        )

    def test_group_members_has_next_page_uses_metadata_or_full_page(self):
        self.assertTrue(self.watch.group_members_has_next_page({"totalPages": 3}, 2, 40, 100))
        self.assertFalse(self.watch.group_members_has_next_page({"totalPages": 3}, 3, 40, 100))
        self.assertTrue(self.watch.group_members_has_next_page({"hasMore": True}, 1, 5, 100))
        self.assertTrue(self.watch.group_members_has_next_page([{}] * 100, 1, 100, 100))
        self.assertFalse(self.watch.group_members_has_next_page([{}] * 99, 1, 99, 100))

    def test_api_request_and_periodic_intervals_are_conservative(self):
        """Guard against accidentally restoring the previous high-frequency polling."""
        self.assertGreaterEqual(self.module.API_REQUEST_INTERVAL_SECONDS, 1.0)
        self.assertGreaterEqual(self.module.PERIODIC_CHECK_INTERVAL_MINUTES, 5)


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


class FakeInteractionResponse:
    def __init__(self):
        self.deferred = []

    async def defer(self, **kwargs):
        self.deferred.append(kwargs)


class FakeInteractionFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, *args, **kwargs):
        self.messages.append((args, kwargs))


class FakeInteraction:
    def __init__(self):
        self.response = FakeInteractionResponse()
        self.followup = FakeInteractionFollowup()


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



    def test_message_error_to_owner_throttles_repeated_error_key(self):
        import asyncio

        dm_user = FakeAlertDestination()
        bot = FakeAlertBot(dm_user=dm_user)
        bot.user = types.SimpleNamespace(name="TestBot")
        watch = self.make_watch(bot)
        watch._last_error_notifications = {}

        asyncio.run(watch.message_error_to_owner("First error", dedupe_key="same-error"))
        asyncio.run(watch.message_error_to_owner("Second error", dedupe_key="same-error"))

        self.assertEqual(len(dm_user.sent_embeds), 1)
        self.assertEqual(dm_user.sent_embeds[0].description, "First error")


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
        watch.errors = []
        watch.saved = []
        # Production retries back off to protect the API. Unit tests use zero
        # delays so failure-path coverage remains fast and deterministic.
        watch.profile_retry_delays = (0, 0)

        async def fetch_group_members(group_id):
            return members_by_group.get(group_id, [])

        async def fetch_habbo_user(username):
            return users_by_name.get(username.lower())

        async def notify_user(embed, policy_name=None):
            watch.notifications.append((embed.title, policy_name))

        async def message_error_to_owner(message, **kwargs):
            watch.errors.append((message, kwargs))

        watch.fetch_group_members = fetch_group_members
        watch.fetch_habbo_user = fetch_habbo_user
        watch.fetch_habbo_user_forced = self.watch_cls.fetch_habbo_user_forced.__get__(watch, self.watch_cls)
        watch.notify_user = notify_user
        watch.message_error_to_owner = message_error_to_owner
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

        self.assertEqual(set(checked), {"Alpha", "Bravo", "Charlie"})
        self.assertEqual(len(checked), 3)
        self.assertEqual(watch._state["bravo"]["was_online"], False)

    def test_periodic_check_stays_quiet_when_user_goes_offline_before_milestone(self):
        users = {"alpha": {"name": "Alpha", "online": True, "profileVisible": True}}
        watch = self.make_watch({self.module.MOD_GROUP_ID: ["Alpha"], self.module.OOA_GROUP_ID: []}, users)

        self.run_periodic_once(watch)
        users["alpha"] = {"name": "Alpha", "online": False, "profileVisible": True}
        self.run_periodic_once(watch)
        self.run_periodic_once(watch)

        self.assertEqual(watch.notifications, [])
        self.assertIn("alpha", watch.logoff_times)

    def test_periodic_check_notifies_when_baseline_offline_user_comes_online(self):
        users = {"alpha": {"name": "Alpha", "online": False, "profileVisible": True}}
        watch = self.make_watch({self.module.MOD_GROUP_ID: ["Alpha"], self.module.OOA_GROUP_ID: []}, users)

        self.run_periodic_once(watch)
        users["alpha"] = {"name": "Alpha", "online": True, "profileVisible": True}
        self.run_periodic_once(watch)
        self.run_periodic_once(watch)

        self.assertEqual(watch.notifications, [("Back Online", "MOD")])


    def test_periodic_check_updates_last_online_json_on_each_online_scan(self):
        users = {"alpha": {"name": "Alpha", "online": True, "profileVisible": True}}
        watch = self.make_watch({self.module.MOD_GROUP_ID: ["Alpha"], self.module.OOA_GROUP_ID: []}, users)

        self.run_periodic_once(watch)
        first_saved_time = watch.last_online_times["alpha"]
        self.run_periodic_once(watch)

        self.assertIn("alpha", watch.last_online_times)
        self.assertGreaterEqual(watch.last_online_times["alpha"], first_saved_time)
        self.assertEqual(watch.offline_records["alpha"]["last_seen_online_at"], watch.last_online_times["alpha"])


    def test_periodic_check_restores_offline_counter_from_saved_last_online_time_after_reset(self):
        from datetime import datetime, timedelta, timezone

        saved_last_online = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        users = {"alpha": {"name": "Alpha", "online": False, "profileVisible": True}}
        watch = self.make_watch({self.module.MOD_GROUP_ID: ["Alpha"], self.module.OOA_GROUP_ID: []}, users)
        watch.last_online_times["alpha"] = saved_last_online

        self.run_periodic_once(watch)

        self.assertEqual(watch.notifications, [("Offline Warning (3 Days)", "MOD")])
        self.assertEqual(watch._state["alpha"]["offline_since"].isoformat(), saved_last_online)
        self.assertEqual(watch.logoff_times["alpha"], saved_last_online)
        self.assertEqual(watch.offline_records["alpha"]["current_offline_since"], saved_last_online)

    def test_periodic_check_ignores_habbo_last_access_without_bot_last_online_time(self):
        from datetime import datetime, timedelta, timezone

        users = {
            "alpha": {
                "name": "Alpha",
                "online": False,
                "profileVisible": True,
                "lastAccessTime": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(),
            }
        }
        watch = self.make_watch({self.module.MOD_GROUP_ID: ["Alpha"], self.module.OOA_GROUP_ID: []}, users)

        self.run_periodic_once(watch)

        self.assertEqual(watch.notifications, [])
        self.assertIn("alpha", watch.logoff_times)
        self.assertEqual(watch.offline_records["alpha"]["current_offline_since"], users["alpha"]["lastAccessTime"])


    def test_periodic_check_retries_profile_lookup_before_reporting_failure(self):
        attempts = []
        watch = self.make_watch({self.module.MOD_GROUP_ID: ["Alpha"], self.module.OOA_GROUP_ID: []}, {})

        async def fetch_habbo_user(username):
            attempts.append(username)
            if len(attempts) == 3:
                return {"name": "Alpha", "online": True, "profileVisible": True}
            return None

        watch.fetch_habbo_user = fetch_habbo_user

        self.run_periodic_once(watch)

        self.assertEqual(attempts, ["Alpha", "Alpha", "Alpha"])
        self.assertEqual(watch.errors, [])
        self.assertTrue(watch._state["alpha"]["was_online"])

    def test_profile_lookup_retries_use_configured_backoff(self):
        import asyncio
        from unittest.mock import AsyncMock, patch

        watch = self.make_watch({self.module.MOD_GROUP_ID: [], self.module.OOA_GROUP_ID: []}, {})
        watch.profile_retry_delays = (1.0, 3.0)
        watch.fetch_habbo_user = AsyncMock(return_value=None)

        with patch.object(self.module.asyncio, "sleep", new=AsyncMock()) as sleep:
            result = asyncio.run(watch.fetch_habbo_user_forced("Missing"))

        self.assertIsNone(result)
        self.assertEqual(watch.fetch_habbo_user.await_count, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1.0, 3.0])

    def test_periodic_check_corrects_stale_offline_counter_from_newer_habbo_activity(self):
        from datetime import datetime, timedelta, timezone

        saved_last_online = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        newer_last_access = (datetime.now(timezone.utc) - timedelta(hours=16)).isoformat()
        users = {
            "alpha": {
                "name": "Alpha",
                "online": False,
                "profileVisible": True,
                "lastAccessTime": newer_last_access,
            }
        }
        watch = self.make_watch({self.module.MOD_GROUP_ID: [], self.module.OOA_GROUP_ID: ["Alpha"]}, users)
        watch.last_online_times["alpha"] = saved_last_online

        self.run_periodic_once(watch)

        self.assertEqual(watch.notifications, [])
        self.assertEqual(watch.logoff_times["alpha"], newer_last_access)
        self.assertEqual(watch.offline_records["alpha"]["current_offline_since"], newer_last_access)

    def test_last_access_slash_reconciles_every_member_and_reports_counts(self):
        import asyncio
        from datetime import datetime, timedelta, timezone

        newer_last_access = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        users = {"alpha": {"name": "Alpha", "online": False, "profileVisible": True, "lastAccessTime": newer_last_access}}
        watch = self.make_watch({self.module.MOD_GROUP_ID: ["Alpha"], self.module.OOA_GROUP_ID: []}, users)
        interaction = FakeInteraction()

        asyncio.run(watch.habbo_last_access_sync(interaction))

        self.assertEqual(watch.notifications, [])
        self.assertEqual(watch.last_online_times["alpha"], newer_last_access)
        self.assertEqual(interaction.followup.messages, [(('Checked 1 watched member(s) and corrected 1 JSON record(s).',), {'ephemeral': True})])

    def test_periodic_check_messages_owner_when_profile_lookup_fails(self):
        watch = self.make_watch({self.module.MOD_GROUP_ID: ["Missing"], self.module.OOA_GROUP_ID: []}, {})

        self.run_periodic_once(watch)

        self.assertEqual(watch.notifications, [])
        self.assertEqual(
            watch.errors,
            [(
                "Habbo profile lookup failed for 1 watched user(s) after retries: Missing. "
                "Habbo may be temporarily unavailable; their last known states were preserved.",
                {"dedupe_key": "periodic-profile-lookups"},
            )],
        )

    def test_periodic_check_batches_lookup_failures_and_preserves_known_state(self):
        watch = self.make_watch(
            {self.module.MOD_GROUP_ID: ["Alpha", "Bravo"], self.module.OOA_GROUP_ID: []},
            {},
        )
        known_state = {"was_online": True, "offline_since": None, "sent_alerts": set()}
        watch._state["alpha"] = known_state

        self.run_periodic_once(watch)

        self.assertIs(watch._state["alpha"], known_state)
        self.assertEqual(len(watch.errors), 1)
        self.assertIn("2 watched user(s)", watch.errors[0][0])
        self.assertIn("Alpha, Bravo", watch.errors[0][0])
        self.assertEqual(watch.errors[0][1], {"dedupe_key": "periodic-profile-lookups"})

    def test_periodic_check_flags_mod_milestones_while_user_stays_offline(self):
        from datetime import datetime, timedelta, timezone

        users = {"alpha": {"name": "Alpha", "online": False, "profileVisible": True}}
        watch = self.make_watch({self.module.MOD_GROUP_ID: ["Alpha"], self.module.OOA_GROUP_ID: []}, users)
        watch._state["alpha"] = {
            "was_online": False,
            "offline_since": datetime.now(timezone.utc) - timedelta(days=2, hours=23),
            "sent_alerts": set(),
        }

        self.run_periodic_once(watch)
        self.run_periodic_once(watch)

        self.assertEqual(watch.notifications, [("Offline Warning (2 Days 23 Hours)", "MOD")])
        self.assertIn("offline_mod_2d_23h", watch._state["alpha"]["sent_alerts"])
        self.assertEqual(watch.offline_records["alpha"]["sent_alerts"], ["offline_mod_2d_23h"])

    def test_periodic_check_uses_persisted_alerts_to_avoid_duplicate_after_restart(self):
        from datetime import datetime, timedelta, timezone

        offline_since = (datetime.now(timezone.utc) - timedelta(days=3, minutes=5)).isoformat()
        users = {"alpha": {"name": "Alpha", "online": False, "profileVisible": True}}
        watch = self.make_watch({self.module.MOD_GROUP_ID: ["Alpha"], self.module.OOA_GROUP_ID: []}, users)
        watch.offline_records["alpha"] = {
            "display_name": "Alpha",
            "policy": "MOD",
            "last_seen_online_at": None,
            "current_offline_since": offline_since,
            "sent_alerts": ["offline_mod_3d"],
            "history": [],
        }

        # Simulate a freshly restarted bot with empty in-memory state but JSON
        # showing that this same offline-window milestone already notified.
        self.run_periodic_once(watch)

        self.assertEqual(watch.notifications, [])
        self.assertEqual(watch._state["alpha"]["sent_alerts"], {"offline_mod_3d"})

    def test_periodic_check_flags_ooa_milestones_while_user_stays_offline(self):
        from datetime import datetime, timedelta, timezone

        users = {"alpha": {"name": "Alpha", "online": False, "profileVisible": True}}
        watch = self.make_watch({self.module.MOD_GROUP_ID: [], self.module.OOA_GROUP_ID: ["Alpha"]}, users)
        watch._state["alpha"] = {
            "was_online": False,
            "offline_since": datetime.now(timezone.utc) - timedelta(hours=23),
            "sent_alerts": set(),
        }

        self.run_periodic_once(watch)

        self.assertEqual(watch.notifications, [("OOA Offline Warning (23 Hours)", "OOA")])

    def test_evaluate_user_uses_discord_relative_times_for_offline_status(self):
        from datetime import datetime, timedelta, timezone

        users = {"alpha": {"name": "Alpha", "online": False, "profileVisible": True}}
        watch = self.make_watch({self.module.MOD_GROUP_ID: ["Alpha"], self.module.OOA_GROUP_ID: []}, users)

        embed, *_ = watch.evaluate_user(
            users["alpha"],
            "Alpha",
            datetime.now(timezone.utc) - timedelta(hours=2),
            "MOD",
        )

        self.assertIn(":R>", embed.description)
        self.assertNotIn(":F>", embed.description)

    def test_check_slash_without_username_forces_full_embed_upload(self):
        import asyncio

        users = {"alpha": {"name": "Alpha", "online": True, "profileVisible": True}}
        watch = self.make_watch({self.module.MOD_GROUP_ID: ["Alpha"], self.module.OOA_GROUP_ID: []}, users)
        interaction = FakeInteraction()

        asyncio.run(watch.habbo_check(interaction))

        self.assertEqual(watch.notifications, [("Online", "MOD")])
        self.assertEqual(interaction.response.deferred, [{"thinking": True, "ephemeral": True}])
        self.assertEqual(
            interaction.followup.messages,
            [(
                ("Check Complete: uploaded 1 embed(s) and used fallback profile embeds for 0 member(s).",),
                {"ephemeral": True},
            )],
        )


    def test_check_slash_with_username_posts_current_embed_even_without_warning(self):
        import asyncio

        users = {"alpha": {"name": "Alpha", "online": True, "profileVisible": True}}
        watch = self.make_watch({self.module.MOD_GROUP_ID: [], self.module.OOA_GROUP_ID: []}, users)
        interaction = FakeInteraction()

        asyncio.run(watch.habbo_check(interaction, "Alpha"))

        self.assertEqual(watch.notifications, [("Online", None)])
        self.assertEqual(interaction.followup.messages, [(("Check Complete",), {"ephemeral": True})])

    def test_force_upload_all_embeds_sends_current_embed_for_every_member(self):
        import asyncio

        users = {
            "alpha": {"name": "Alpha", "online": False, "profileVisible": True},
            "bravo": {"name": "Bravo", "online": True, "profileVisible": True},
            "charlie": {"name": "Charlie", "online": False, "profileVisible": True},
        }
        watch = self.make_watch(
            {self.module.MOD_GROUP_ID: ["Alpha", "Bravo"], self.module.OOA_GROUP_ID: ["Bravo", "Charlie"]},
            users,
        )
        watch.logoff_times["alpha"] = "2026-06-17T10:00:00+00:00"

        sent_count, unavailable_count, unavailable_usernames = asyncio.run(watch.force_upload_all_embeds())

        self.assertEqual(sent_count, 3)
        self.assertEqual(unavailable_count, 0)
        self.assertEqual(unavailable_usernames, [])
        self.assertEqual(len(watch.notifications), 3)
        self.assertEqual({policy for _title, policy in watch.notifications}, {"MOD", "OOA"})
        self.assertFalse(watch._state["alpha"]["was_online"])
        self.assertTrue(watch._state["bravo"]["was_online"])
        self.assertIn("bravo", watch.last_online_times)
        self.assertEqual(watch.offline_records["alpha"]["current_offline_since"], "2026-06-17T10:00:00+00:00")
        self.assertIn("last_online", watch.saved)
        self.assertIn("logoff", watch.saved)
        self.assertIn("offline_records", watch.saved)



    def test_force_upload_all_embeds_closes_offline_json_when_member_is_online(self):
        import asyncio

        users = {"alpha": {"name": "Alpha", "online": True, "profileVisible": True}}
        watch = self.make_watch({self.module.MOD_GROUP_ID: ["Alpha"], self.module.OOA_GROUP_ID: []}, users)
        watch.logoff_times["alpha"] = "2026-06-17T10:00:00+00:00"
        watch.offline_records["alpha"] = {
            "display_name": "Alpha",
            "policy": "MOD",
            "last_seen_online_at": None,
            "current_offline_since": "2026-06-17T10:00:00+00:00",
            "history": [],
        }

        sent_count, unavailable_count, unavailable_usernames = asyncio.run(watch.force_upload_all_embeds())

        self.assertEqual((sent_count, unavailable_count, unavailable_usernames), (1, 0, []))
        self.assertIn("alpha", watch.last_online_times)
        self.assertNotIn("alpha", watch.logoff_times)
        self.assertIsNone(watch.offline_records["alpha"]["current_offline_since"])
        self.assertEqual(watch.offline_records["alpha"]["history"][0]["offline_since"], "2026-06-17T10:00:00+00:00")

    def test_force_upload_all_embeds_retries_each_user_before_using_fallback(self):
        import asyncio

        attempts = []
        watch = self.make_watch(
            {self.module.MOD_GROUP_ID: ["Alpha"], self.module.OOA_GROUP_ID: []},
            {},
        )

        async def fetch_habbo_user(username):
            attempts.append(username)
            if len(attempts) == 3:
                return {"name": "Alpha", "online": True, "profileVisible": True}
            return None

        watch.fetch_habbo_user = fetch_habbo_user

        sent_count, unavailable_count, unavailable_usernames = asyncio.run(watch.force_upload_all_embeds())

        self.assertEqual(attempts, ["Alpha", "Alpha", "Alpha"])
        self.assertEqual(sent_count, 1)
        self.assertEqual(unavailable_count, 0)
        self.assertEqual(unavailable_usernames, [])
        self.assertEqual(watch.notifications, [("Online", "MOD")])

    def test_force_upload_all_embeds_uses_fallback_for_profiles_that_cannot_be_fetched(self):
        import asyncio

        users = {"alpha": {"name": "Alpha", "online": True, "profileVisible": True}}
        watch = self.make_watch(
            {self.module.MOD_GROUP_ID: ["Alpha", "Missing"], self.module.OOA_GROUP_ID: []},
            users,
        )

        sent_count, unavailable_count, unavailable_usernames = asyncio.run(watch.force_upload_all_embeds())

        self.assertEqual(sent_count, 2)
        self.assertEqual(unavailable_count, 1)
        self.assertEqual(unavailable_usernames, ["Missing"])
        self.assertCountEqual(watch.notifications, [("Online", "MOD"), ("Profile Unavailable", "MOD")])
        self.assertEqual(
            watch.errors,
            [("Habbo profile lookup failed for watched user Missing during forced embed upload; posted a fallback embed instead.", {})],
        )

    def test_format_force_check_summary_lists_fallback_profiles(self):
        message = self.watch_cls.format_force_check_summary(20, [f"user{i}" for i in range(12)])

        self.assertEqual(
            message,
            "Check Complete: uploaded 20 embed(s) and used fallback profile embeds for 12 member(s). "
            "Fallbacks: user0, user1, user2, user3, user4, user5, user6, user7, user8, user9 (+2 more).",
        )


if __name__ == "__main__":
    unittest.main()
