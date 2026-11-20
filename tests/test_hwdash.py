import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

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


class EmbedConfigTest(unittest.TestCase):
    def good(self, **changes):
        return {"id": "app", "title": "App", "upstream": "http://127.0.0.1:9000/", **changes}

    def test_upstream_is_normalized(self):
        self.assertEqual(hwdash.clean_embeds([self.good()], "c")[0]["upstream"], "http://127.0.0.1:9000")

    def test_bad_entries_stop_startup(self):
        for bad in (self.good(id="net"), self.good(id="A B"), self.good(title=""), self.good(upstream="file:///x"), self.good(extra=1)):
            with self.assertRaises(SystemExit):
                hwdash.clean_embeds([bad], "c")
        with self.assertRaises(SystemExit):
            hwdash.clean_embeds([self.good(), self.good()], "c")


class EmbedProxyTest(unittest.TestCase):
    def setUp(self):
        seen = self.seen = []

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                seen.append((self.command, self.path, dict(self.headers), body))
                code = 409 if self.path.startswith("/refused") else 200
                data = json.dumps({"ok": True}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Set-Cookie", "s=1; Path=/; HttpOnly")
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = reply

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        port = self.upstream.server_address[1]
        embeds = [{"id": "app", "title": "App", "upstream": f"http://127.0.0.1:{port}"},
                  {"id": "down", "title": "Down", "upstream": "http://127.0.0.1:9"}]
        handler = hwdash.make_handler(SimpleNamespace(meta={}, info={}), None, None, {"now": {}}, 5, embeds=embeds)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        for server in (self.server, self.upstream):
            server.shutdown()
            server.server_close()

    def call(self, path, data=None, headers=None):
        try:
            with self.opener.open(urllib.request.Request(self.base + path, data=data, headers=headers or {}), timeout=5) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_get_keeps_path_and_query_and_forwards_only_the_cookie(self):
        status, _, headers = self.call("/embed/app/api/x?a=1", headers={"Cookie": "s=1", "Authorization": "Bearer secret"})
        self.assertEqual(status, 200)
        method, path, sent, _ = self.seen[0]
        self.assertEqual((method, path, sent["Cookie"]), ("GET", "/api/x?a=1", "s=1"))
        self.assertNotIn("Authorization", sent)
        self.assertIn("HttpOnly", headers["Set-Cookie"])

    def test_post_passes_body_and_error_status(self):
        status, body, _ = self.call("/embed/app/refused", b'{"a":1}', {"Content-Type": "application/json"})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body), {"ok": True})
        self.assertEqual(self.seen[0][3], b'{"a":1}')

    def test_unknown_embed_and_missing_slash_are_not_found(self):
        self.assertEqual(self.call("/embed/other/")[0], 404)
        self.assertEqual(self.call("/embed/app")[0], 404)

    def test_unavailable_upstream(self):
        self.assertEqual(self.call("/embed/down/")[0], 502)

    def test_tabs_are_listed(self):
        info = json.loads(self.call("/api/now")[1])["info"]
        self.assertEqual(info["embeds"][0], {"id": "app", "title": "App"})


if __name__ == "__main__":
    unittest.main()
