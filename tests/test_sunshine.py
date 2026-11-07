import datetime as dt
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sunshine  # noqa: E402

# Wednesday 2026-11-04 17:00 local time
WED_17 = dt.datetime(2026, 11, 4, 17, 0).timestamp()


def schedule(**kw):
    weekdays = [[[960, 1200]]] * 5  # 16:00–20:00
    return {**sunshine.empty_policy(), "mode": "schedule", "days": weekdays + [[[600, 1260]]] * 2, **kw}


class EvaluateTest(unittest.TestCase):
    def test_unmanaged_is_always_allowed(self):
        ev = sunshine.evaluate(None, WED_17, 10**6)
        self.assertEqual((ev["managed"], ev["allowed"]), (False, True))

    def test_inside_window(self):
        ev = sunshine.evaluate(schedule(), WED_17, 0)
        self.assertTrue(ev["allowed"])
        self.assertEqual(ev["ends_at"], WED_17 + 3 * 3600)

    def test_limit_shortens_the_window(self):
        ev = sunshine.evaluate(schedule(limit_weekday=60), WED_17, 20 * 60)
        self.assertEqual(ev["ends_at"], WED_17 + 40 * 60)

    def test_limit_used_up_opens_tomorrow(self):
        ev = sunshine.evaluate(schedule(limit_weekday=60), WED_17, 60 * 60)
        self.assertEqual((ev["allowed"], ev["reason"]), (False, "limit"))
        self.assertEqual(ev["opens_at"], dt.datetime(2026, 11, 5, 16, 0).timestamp())

    def test_outside_window_opens_later_today(self):
        ev = sunshine.evaluate(schedule(), WED_17 - 4 * 3600, 0)
        self.assertEqual((ev["allowed"], ev["reason"]), (False, "closed"))
        self.assertEqual(ev["opens_at"], WED_17 - 3600)

    def test_extension_and_pause(self):
        ev = sunshine.evaluate(schedule(allow_until=WED_17 + 4 * 3600), WED_17 + 3.5 * 3600, 0)
        self.assertEqual((ev["allowed"], ev["reason"]), (True, "extra"))
        ev = sunshine.evaluate(schedule(block_until=WED_17 + 600, allow_until=WED_17 + 9000), WED_17, 0)
        self.assertEqual((ev["allowed"], ev["reason"]), (False, "paused"))

    def test_extension_keeps_a_longer_window(self):
        ev = sunshine.evaluate(schedule(allow_until=WED_17 + 600), WED_17, 0)
        self.assertEqual(ev["ends_at"], WED_17 + 3 * 3600)

    def test_blocked_and_always(self):
        self.assertFalse(sunshine.evaluate({"mode": "blocked"}, WED_17, 0)["allowed"])
        self.assertTrue(sunshine.evaluate({"mode": "always", "limit_weekday": 0}, WED_17, 10**6)["allowed"])


class PolicyTest(unittest.TestCase):
    def test_clean_policy_merges_windows(self):
        p = sunshine.clean_policy({"mode": "schedule", "days": [[[600, 720], [700, 800], [900, 960]]] + [[]] * 6,
                                   "limit_weekday": 90})
        self.assertEqual(p["days"][0], [[600, 800], [900, 960]])
        self.assertEqual((p["limit_weekday"], p["limit_weekend"]), (90, None))

    def test_clean_policy_rejects(self):
        for bad in ({"mode": "x"}, {"mode": "schedule", "days": [[]] * 6},
                    {"mode": "schedule", "days": [[[50, 10]]] + [[]] * 6},
                    {"mode": "schedule", "limit_weekday": -1}):
            with self.assertRaises(ValueError):
                sunshine.clean_policy(bad)

    def test_pin(self):
        stored = sunshine.hash_pin("2468", rounds=1000)
        self.assertTrue(sunshine.check_pin(stored, "2468"))
        self.assertFalse(sunshine.check_pin(stored, "2469"))
        self.assertFalse(sunshine.check_pin("garbage", "2468"))


class LogTest(unittest.TestCase):
    def test_parse(self):
        p = sunshine.parse_log_line
        self.assertEqual(p("[2026-11-04 21:05:39.497]: Info: CLIENT CONNECTED")[1:], ("connected", None))
        self.assertEqual(p("[2026-11-04 21:06:04.467]: Info: CLIENT DISCONNECTED")[1], "disconnected")
        self.assertEqual(p("[2026-11-04 21:05:39.501]: Info: Creating encoder [hevc_nvenc]")[2], "HEVC")
        self.assertEqual(p("[2026-11-04 21:05:39.535]: Info: Streaming bitrate is 8988000")[2], 8988000)
        self.assertIsNone(p("[2026-11-04 21:05:39.501]: Info: Screencasting with KMS"))

    def test_tail_follows_rotation(self):
        path = Path(tempfile.mkdtemp()) / "sunshine.log"
        path.write_text("a\nb\n")
        tail = sunshine.LogTail(path)
        self.assertEqual(tail.read(), ["a", "b"])
        with open(path, "a") as f:
            f.write("c\n")
        self.assertEqual(tail.read(), ["c"])
        path.rename(path.with_suffix(".log.1"))
        path.write_text("d\n")
        self.assertEqual(tail.read(), ["d"])


class GateTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.db = tmp / "m.db"
        self.cfg = {**sunshine.SUNSHINE_DEFAULTS, "policy": str(tmp / "policy.json")}
        (tmp / "sunshine-clients.json").write_text(json.dumps([{"uuid": "U1", "name": "kid"}]))
        (tmp / "policy.json").write_text(json.dumps({"clients": {"U1": schedule(limit_weekday=60)}}))

    def gate(self, name, now):
        env = {"SUNSHINE_CLIENT_NAME": name, "SUNSHINE_APP_NAME": "Desktop", "SUNSHINE_CLIENT_FPS": "60"}
        return sunshine.run_gate(self.cfg, self.db, "start", env, now)

    def test_allow_deny_and_record(self):
        self.assertEqual(self.gate("kid", WED_17), 0)
        self.assertEqual(self.gate("kid", WED_17 + 4 * 3600), 1)
        self.assertEqual(self.gate("someone else", WED_17 + 4 * 3600), 0)
        rows = sunshine.StreamDB(self.db).q("SELECT name, denied, reason, fps FROM stream_launches ORDER BY id")
        self.assertEqual(rows, [("kid", 0, "window", 60), ("kid", 1, "closed", 60),
                                ("someone else", 0, "unmanaged", 60)])

    def test_used_limit_counts(self):
        db = sunshine.StreamDB(self.db)
        db.w("INSERT INTO stream_usage (uuid, start, end) VALUES ('U1', ?, ?)", (WED_17 - 3600, WED_17 + 300))
        self.assertEqual(self.gate("kid", WED_17 + 600), 1)

    def test_errors_let_the_launch_through(self):
        Path(self.cfg["policy"]).write_text("{not json")
        self.assertEqual(self.gate("kid", WED_17 + 4 * 3600), 0)


class ManagerTest(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.db_path = tmp / "metrics.db"
        self.mgr = sunshine.make_demo(tmp, {})
        self.kid = "DEMO-0001"

    def test_disabled_outside_schedule(self):
        laptop = next(c for c in self.mgr.api.clients() if c["name"] == "Old laptop")
        self.assertFalse(laptop["enabled"])

    def time_up(self):
        self.mgr.policies[self.kid]["limit_weekday"] = self.mgr.policies[self.kid]["limit_weekend"] = 10
        self.at = self.mgr.connected_since + 3600
        self.mgr.tick(self.at)

    def test_time_up_ends_the_stream_but_keeps_the_game(self):
        self.time_up()
        self.assertFalse(self.mgr.api.busy)
        kid = next(c for c in self.mgr.api.clients() if c["uuid"] == self.kid)
        self.assertFalse(kid["enabled"])
        self.assertEqual(len(self.mgr.game_procs.running()), 1)
        self.assertEqual(self.mgr.game_close["uuid"], self.kid)

    def test_connected_state_clears_without_disconnected_line(self):
        self.time_up()
        self.mgr.tick(self.at + 5)
        self.assertFalse(self.mgr.connected)
        self.assertIsNone(self.mgr.db.open_usage())

    def test_game_closes_after_the_grace_period(self):
        self.time_up()
        self.mgr.tick(self.at + 4 * 60)
        self.assertEqual(len(self.mgr.game_procs.running()), 1)
        self.mgr.tick(self.at + 5 * 60 + 1)
        self.assertEqual(self.mgr.game_procs.running(), [])
        self.mgr.tick(self.at + 5 * 60 + 6)
        self.assertIsNone(self.mgr.game_close)
        self.assertFalse(self.mgr.game_close_path.exists())
        self.assertIn("game_closed", [a[0] for a in self.mgr.db.q("SELECT action FROM stream_actions")])

    def test_extending_keeps_the_game(self):
        self.time_up()
        self.mgr.apply_action(self.kid, "extend", 15)
        self.assertIsNone(self.mgr.game_close)
        self.mgr.tick(self.at + 5 * 60 + 1)
        self.assertEqual(len(self.mgr.game_procs.running()), 1)

    def test_reconnecting_keeps_the_game(self):
        self.time_up()
        self.mgr._on_log(self.at + 60, "connected", None)
        self.mgr.api.busy = True
        self.mgr.tick(self.at + 61)
        self.assertIsNone(self.mgr.game_close)

    def test_waiting_game_survives_a_restart(self):
        self.time_up()
        again = sunshine.StreamManager(self.mgr.cfg, self.db_path, api=self.mgr.api, games=self.mgr.game_procs)
        self.assertEqual(again.game_close["uuid"], self.kid)

    def test_game_is_left_alone_for_unmanaged_device(self):
        self.mgr.policies[self.kid]["mode"] = "unmanaged"
        self.mgr.apply_action(self.kid, "end")
        self.assertFalse(self.mgr.api.busy)
        self.assertIsNone(self.mgr.game_close)

    def test_close_game_now(self):
        self.mgr.apply_action("", "close_game")
        self.assertEqual(self.mgr.game_procs.running(), [])
        with self.assertRaises(ValueError):
            self.mgr.apply_action("", "close_game")

    def test_login_lockout(self):
        for _ in range(5):
            self.assertIsNone(self.mgr.login("0000")[0])
        token, wait = self.mgr.login("1234")
        self.assertIsNone(token)
        self.assertGreater(wait, 0)


class SteamGamesTest(unittest.TestCase):
    def test_pattern_matches_only_the_process_itself(self):
        pattern = sunshine.SteamGames(sunshine.SUNSHINE_DEFAULTS["game_process"]).pattern
        self.assertEqual(pattern.match("/home/u/.steam/ubuntu12_32/reaper SteamLaunch AppId=228380 -- x")[1], "228380")
        self.assertIsNone(pattern.match("/bin/bash -c pgrep -f 'reaper SteamLaunch AppId='"))

    def test_signals_reach_the_tree(self):
        games = sunshine.SteamGames(r"^hwdashtestgame -c")
        proc = subprocess.Popen(["bash", "-c", "exec -a hwdashtestgame bash -c 'sleep 60 & wait'"])
        try:
            for _ in range(50):
                if games.running():
                    break
                time.sleep(0.1)
            root = games.running()[0]["pid"]
            games.signal(signal.SIGKILL)
            proc.wait(timeout=5)
            self.assertEqual(games.running(), [])
            self.assertFalse(os.path.exists(f"/proc/{root}"))
        finally:
            proc.kill()


if __name__ == "__main__":
    unittest.main()
