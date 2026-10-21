"""Regression checks for competing immediate and background handshakes."""

import threading
import unittest
from unittest.mock import Mock, patch

from custom_components.localtuya.battletron_zones import ZoneSender


class ZoneSenderTests(unittest.TestCase):
    def test_concurrent_handshakes_send_once_and_retry_after_timeout(self):
        sender = ZoneSender()
        with patch("custom_components.localtuya.battletron_zones._route", return_value="127.0.0.1"):
            sender.add("test-device", "0123456789abcdef", "127.0.0.2")
        sender._send_unlocked = Mock()
        with patch("custom_components.localtuya.battletron_zones.time.monotonic", return_value=10.0):
            callers = [threading.Thread(target=sender.negotiate, args=("test-device",)) for _ in range(8)]
            for caller in callers:
                caller.start()
            for caller in callers:
                caller.join()
        self.assertEqual(sender._send_unlocked.call_count, 1)
        with patch("custom_components.localtuya.battletron_zones.time.monotonic", return_value=11.1):
            sender.negotiate("test-device")
        self.assertEqual(sender._send_unlocked.call_count, 2)
        sender._devices["test-device"]["session"] = b"established"
        with patch("custom_components.localtuya.battletron_zones.time.monotonic", return_value=20.0):
            sender.negotiate("test-device")
        self.assertEqual(sender._send_unlocked.call_count, 2)
        self.assertEqual(sender._devices["test-device"]["targets"], ["127.0.0.2"])


if __name__ == "__main__":
    unittest.main()
