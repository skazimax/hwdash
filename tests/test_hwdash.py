import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hwdash  # noqa: E402


class SensorMetaTest(unittest.TestCase):
    def test_cpu_package_is_focused(self):
        m = hwdash.sensor_meta("k10temp", "Tctl", "temp", 45.0)
        self.assertEqual((m["title"], m["group"], m["warn"], m["focus"], m["hidden"]), ("CPU", "cpu", 90, True, False))

    def test_regex_groups_in_title(self):
        m = hwdash.sensor_meta("coretemp", "Core 3", "temp", 50.0)
        self.assertEqual(m["title"], "CPU core 3")
        self.assertTrue(m["hidden"])

    def test_board_chip_keeps_driver_label(self):
        m = hwdash.sensor_meta("nct6798", "SYSTIN", "temp", 33.0)
        self.assertEqual((m["title"], m["group"]), ("nct6798 SYSTIN", "board"))

    def test_unknown_and_bogus(self):
        self.assertEqual(hwdash.sensor_meta("weird", "temp1", "temp", 40.0)["group"], "other")
        self.assertTrue(hwdash.sensor_meta("weird", "temp1", "temp", 0.0)["hidden"])
        self.assertTrue(hwdash.sensor_meta("weird", "temp1", "temp", 255.0)["hidden"])

    def test_fans(self):
        self.assertEqual(hwdash.sensor_meta("nct6798", "fan2", "fan", 900)["title"], "Fan 2")
        self.assertTrue(hwdash.sensor_meta("nct6798", "fan5", "fan", 0)["hidden"])


class ConfigTest(unittest.TestCase):
    def test_clean_config(self):
        cfg = hwdash.clean_config({"hidden": {"a": 1}, "focus": {"b": 0}, "names": {"a": "  CPU  ", "c": " ", "d": 5}})
        self.assertEqual(cfg, {"hidden": {"a": True}, "focus": {"b": False}, "names": {"a": "CPU"}})

    def test_clean_config_rejects_bad_shape(self):
        for bad in ([1], {"hidden": []}, {"names": "x"}):
            with self.assertRaises(ValueError):
                hwdash.clean_config(bad)

    def test_settings_reject_unknown_keys(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[server]\nprot = 1\n')
        with self.assertRaises(SystemExit):
            hwdash.load_settings(Path(f.name))

    def test_settings_merge_defaults(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write('[server]\nport = 9000\n[sensors."t:x/y"]\ntitle = "Chipset"\n')
        s = hwdash.load_settings(Path(f.name))
        self.assertEqual((s["server"]["port"], s["server"]["step"]), (9000, 10))
        self.assertEqual(s["sensors"]["t:x/y"]["title"], "Chipset")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = hwdash.Store(Path(tempfile.mkdtemp()) / "m.db", step=10)

    def test_history_buckets_and_gaps(self):
        now = 1_000_000_000 - 1_000_000_000 % 3600
        # first half hour: value 10 then 20; second half: nothing
        self.store.add_many([(ts, {"x": 10 if ts % 20 else 20}) for ts in range(now - 3600, now - 1800, 10)])
        h = self.store.history(3600, now=now)
        self.assertEqual(h["step"], 20)
        self.assertEqual(h["series"]["x"][0], 15)
        self.assertIsNone(h["series"]["x"][-1])
        self.assertEqual((h["min"]["x"], h["max"]["x"]), (10, 20))

    def test_baseline_percentiles(self):
        now = 2_000_000
        self.store.add_many([(now - 1000 + i, {"x": i}) for i in range(100)])
        b = self.store.baseline(now=now)["keys"]["x"]
        self.assertEqual((b["p10"], b["p50"], b["p90"]), (10, 50, 90))

    def test_baseline_needs_enough_samples(self):
        self.store.add_many([(i, {"x": i}) for i in range(10)])
        self.assertNotIn("x", self.store.baseline(now=100)["keys"])


class DemoTest(unittest.TestCase):
    def test_demo_values_cover_metadata_and_overrides(self):
        c = hwdash.DemoCollector(dict(hwdash.SERVER_DEFAULTS), {})
        _, values = c.sample()
        self.assertTrue(set(c.meta) <= set(values))
        self.assertEqual(c.meta["t:nct6798/CPUTIN"]["title"], "CPU VRM")
        self.assertEqual(c.meta["t:spd5118/temp1#2"]["title"], "Memory DIMM #2")
        self.assertTrue(c.meta["f:nct6798/fan3"]["hidden"])


if __name__ == "__main__":
    unittest.main()
