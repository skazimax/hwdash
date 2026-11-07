import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import topology  # noqa: E402

NODES = [
    {"id": "hub", "ping": "hub"},
    {"id": "site", "ping": "site", "parent": "hub", "link": "VPN"},
    {"id": "phone", "ping": "phone", "parent": "hub", "optional": True},
    {"id": "tablet", "parent": "hub"},
]
UP = (0.0, 1.0, 2.0, 3.0)
LOST = (100.0, None, None, None)


def monitor(results):
    return topology.TopologyMonitor({"nodes": NODES, "rtt_warn_ms": 100}, probe=lambda target, count: results[target])


def states(mon):
    return {n["id"]: n["state"] for n in mon.snapshot()["nodes"]}


class CleanNodesTest(unittest.TestCase):
    def test_rejects_broken_trees(self):
        for nodes in ([], [{"id": "a"}, {"id": "b"}], [{"id": "a"}, {"id": "a", "parent": "a"}],
                      [{"id": "a"}, {"id": "b", "parent": "missing"}],
                      [{"id": "r"}, {"id": "a", "parent": "b"}, {"id": "b", "parent": "a"}]):
            with self.assertRaises(ValueError):
                topology.clean_nodes(nodes)

    def test_rejects_unsafe_target_and_unknown_field(self):
        with self.assertRaises(ValueError):
            topology.clean_nodes([{"id": "a", "ping": "-f"}])
        with self.assertRaises(ValueError):
            topology.clean_nodes([{"id": "a", "ping": "1.1.1.1; reboot"}])
        with self.assertRaises(ValueError):
            topology.clean_nodes([{"id": "a", "nope": 1}])


class ParsePingTest(unittest.TestCase):
    def test_reply(self):
        text = "3 packets transmitted, 3 received, 0% packet loss, time 402ms\nrtt min/avg/max/mdev = 7.0/8.5/9.1/0.8 ms\n"
        self.assertEqual(topology.parse_ping(text), (0.0, 7.0, 8.5, 9.1))

    def test_no_reply(self):
        self.assertEqual(topology.parse_ping("3 packets transmitted, 0 received, 100% packet loss, time 2ms\n"), LOST)

    def test_garbage_counts_as_lost(self):
        self.assertEqual(topology.parse_ping(""), LOST)


class MonitorTest(unittest.TestCase):
    def test_unprobed_node_stays_unknown(self):
        mon = monitor({"hub": UP, "site": UP, "phone": UP})
        mon.check_once()
        self.assertEqual(states(mon), {"hub": "up", "site": "up", "phone": "up", "tablet": "unknown"})

    def test_slow_or_lossy_is_degraded(self):
        mon = monitor({"hub": UP, "site": (0.0, 200.0, 250.0, 300.0), "phone": (33.3, 1.0, 2.0, 3.0)})
        mon.check_once()
        self.assertEqual(states(mon)["site"], "degraded")
        self.assertEqual(states(mon)["phone"], "degraded")

    def test_down_only_after_repeated_loss(self):
        results = {"hub": UP, "site": LOST, "phone": UP}
        mon = monitor(results)
        mon.check_once()
        self.assertEqual(states(mon)["site"], "degraded")
        mon.check_once()
        self.assertEqual(states(mon)["site"], "down")
        results["site"] = UP
        mon.check_once()
        self.assertEqual(states(mon)["site"], "up")

    def test_optional_node_goes_idle_not_down(self):
        mon = monitor({"hub": UP, "site": UP, "phone": LOST})
        mon.check_once()
        mon.check_once()
        self.assertEqual(states(mon)["phone"], "idle")

    def test_children_of_a_dead_parent_are_unknown(self):
        mon = monitor({"hub": LOST, "site": LOST, "phone": LOST})
        mon.check_once()
        mon.check_once()
        st = states(mon)
        self.assertEqual(st["hub"], "down")
        self.assertEqual((st["site"], st["phone"]), ("unknown", "unknown"))

    def test_since_changes_only_with_state(self):
        mon = monitor({"hub": UP, "site": UP, "phone": UP})
        mon.check_once()
        first = {n["id"]: n["since"] for n in mon.snapshot()["nodes"]}
        mon.status["hub"]["since"] = 1
        mon.check_once()
        self.assertEqual(mon.status["hub"]["since"], 1)
        self.assertIsNotNone(first["hub"])

    def test_demo_runs_without_network(self):
        st = {n["id"]: n["state"] for n in topology.make_demo({}).snapshot()["nodes"]}
        self.assertEqual(st["laptop"], "idle")
        self.assertEqual(st["cabin"], "degraded")
        self.assertEqual(st["phone"], "unknown")


class FakeApi:
    def __init__(self, replies):
        self.replies = replies

    def get(self, path):
        reply = self.replies[path]
        if isinstance(reply, Exception):
            raise reply
        return reply


def wg_interfaces(age=40, online=True, rx=1000, tx=2000):
    return {"Wireguard2": {"wireguard": {"peer": [{"online": online, "last-handshake": age, "rxbytes": rx, "txbytes": tx}]}}}


class TunnelStatusTest(unittest.TestCase):
    NODE = {"tunnel_if": "Wireguard2", "tunnel_user": "", "optional": False}
    USER = {"tunnel_if": "", "tunnel_user": "alice", "optional": True}

    def test_wireguard_states(self):
        self.assertEqual(topology.tunnel_status(self.NODE, wg_interfaces(), [], 300)["state"], "up")
        self.assertEqual(topology.tunnel_status(self.NODE, wg_interfaces(age=301), [], 300)["state"], "down")
        self.assertEqual(topology.tunnel_status(self.NODE, wg_interfaces(online=False), [], 300)["state"], "down")

    def test_missing_interface_says_nothing(self):
        self.assertIsNone(topology.tunnel_status(self.NODE, {}, [], 300))

    def test_session_presence(self):
        session = {"username": "alice", "uptime": 5, "statistic": {"rxbytes": 1, "txbytes": 2}}
        self.assertEqual(topology.tunnel_status(self.USER, {}, [session], 300)["state"], "up")
        self.assertEqual(topology.tunnel_status(self.USER, {}, [], 300)["state"], "idle")
        self.assertEqual(topology.tunnel_status({**self.USER, "optional": False}, {}, [], 300)["state"], "down")

    def test_router_status_counts_cache_as_free(self):
        st = topology.router_status({"cpuload": 7, "memtotal": 100, "memfree": 50, "memcache": 20, "membuffers": 10, "uptime": "9"})
        self.assertEqual(st, {"cpu": 7, "mem": 20, "uptime": 9})


class RouterReadTest(unittest.TestCase):
    NODES = [{"id": "hub", "api": "http://192.0.2.1"}, {"id": "site", "parent": "hub", "tunnel_if": "Wireguard2", "ping": "site"}]

    def monitor(self, replies):
        return topology.TopologyMonitor({"nodes": self.NODES}, probe=lambda t, c: UP, apis={"hub": FakeApi(replies)})

    def test_link_follows_tunnel_and_api_address_stays_private(self):
        mon = self.monitor({"show/system": {"cpuload": 5}, "show/interface": wg_interfaces(age=999)})
        mon.check_once()
        mon.read_routers()
        site = next(n for n in mon.snapshot()["nodes"] if n["id"] == "site")
        self.assertEqual((site["state"], site["link_state"]), ("up", "down"))
        self.assertNotIn("api", next(n for n in mon.snapshot()["nodes"] if n["id"] == "hub"))

    def test_snapshot_hides_router_details(self):
        nodes = [{"id": "hub", "api": "http://192.0.2.1"}, {"id": "phone", "parent": "hub", "tunnel_user": "secret-login"},
                 {"id": "site", "parent": "hub", "tunnel_if": "Wireguard9"}]
        mon = topology.TopologyMonitor({"nodes": nodes}, probe=lambda t, c: UP,
                                       apis={"hub": FakeApi({"show/system": {}, "show/interface": {}, "show/sstp-server": {"tunnel": []}})})
        mon.read_routers()
        text = str(mon.snapshot())
        for private in ("192.0.2.1", "secret-login", "Wireguard9"):
            self.assertNotIn(private, text)

    def test_rates_from_counters(self):
        replies = {"show/system": {}, "show/interface": wg_interfaces(rx=0, tx=0)}
        mon = self.monitor(replies)
        mon.read_routers()
        mon._counters["site"] = (mon._counters["site"][0] - 10, 0, 0)
        replies["show/interface"] = wg_interfaces(rx=1000, tx=2000)
        mon.read_routers()
        self.assertAlmostEqual(mon.tunnels["site"]["rx_bps"], 800, delta=5)

    def test_unreachable_router_falls_back_to_ping(self):
        mon = self.monitor({"show/system": OSError("timeout")})
        mon.check_once()
        mon.read_routers()
        site = next(n for n in mon.snapshot()["nodes"] if n["id"] == "site")
        self.assertIsNone(site["tunnel"])
        self.assertEqual(site["link_state"], "up")

    def test_no_credentials_means_no_router_reads(self):
        mon = topology.TopologyMonitor({"nodes": self.NODES, "credentials": "/nonexistent/cred"}, probe=lambda t, c: UP)
        self.assertEqual(mon.apis, {})


if __name__ == "__main__":
    unittest.main()
