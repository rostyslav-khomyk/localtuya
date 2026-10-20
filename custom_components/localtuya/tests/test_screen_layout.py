import unittest

from custom_components.localtuya.screen_layout import color_payload, zone_counts


class ZoneTests(unittest.TestCase):
    def test_monitor_strip_lengths(self):
        self.assertEqual(zone_counts([27, 57, 27]), [4, 8, 4])

    def test_known_four_color_payload(self):
        colors = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)]
        payload = color_payload(colors, [4, 4, 4, 4])
        self.assertEqual(
            payload,
            "0004000003e803e8007803e803e800f003e803e8003c03e803e8"
            "001001010101020202020303030304040404",
        )


if __name__ == "__main__":
    unittest.main()
