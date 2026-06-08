from __future__ import annotations

from datetime import timedelta
import unittest

from agent_keepalive.timeparse import format_duration
from agent_keepalive.timeparse import parse_duration


class ParseDurationTests(unittest.TestCase):
    def test_parse_single_units(self) -> None:
        self.assertEqual(parse_duration("1h"), timedelta(hours=1))
        self.assertEqual(parse_duration("90m"), timedelta(minutes=90))
        self.assertEqual(parse_duration("45s"), timedelta(seconds=45))

    def test_parse_composite_duration(self) -> None:
        self.assertEqual(parse_duration("2h30m"), timedelta(hours=2, minutes=30))

    def test_format_duration(self) -> None:
        self.assertEqual(format_duration(timedelta(hours=1, minutes=30)), "1h30m")

    def test_parse_invalid_duration(self) -> None:
        with self.assertRaises(ValueError):
            parse_duration("")
        with self.assertRaises(ValueError):
            parse_duration("abc")


if __name__ == "__main__":
    unittest.main()
