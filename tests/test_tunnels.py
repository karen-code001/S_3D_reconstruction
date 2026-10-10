import sys
import time
import unittest

from transfer_node.tunnels import TunnelSupervisor


class TunnelSupervisorTests(unittest.TestCase):
    def test_restarts_tunnel_after_it_exits(self):
        supervisor = TunnelSupervisor(["host-a"], 0.05, [sys.executable, "-c", "import sys; sys.exit(3)"])
        with self.assertLogs("transfer_node.tunnels", level="WARNING") as logs:
            supervisor.start()
            time.sleep(1.5)
            supervisor.stop()
        self.assertGreaterEqual(sum("code=3" in line for line in logs.output), 2)

    def test_stop_terminates_running_tunnel(self):
        supervisor = TunnelSupervisor(["host-a"], 0.05, [sys.executable, "-c", "import time; time.sleep(60)"])
        supervisor.start()
        time.sleep(0.5)
        process = supervisor._processes["host-a"]
        supervisor.stop()
        self.assertIsNotNone(process.poll())
        self.assertFalse(any(thread.is_alive() for thread in supervisor._threads))

    def test_no_hosts_starts_nothing(self):
        supervisor = TunnelSupervisor([], 1)
        supervisor.start()
        supervisor.stop()
        self.assertEqual(supervisor._threads, [])


if __name__ == "__main__":
    unittest.main()
