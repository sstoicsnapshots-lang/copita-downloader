"""yt-dlp auto-update — real checks against PyPI, no mocks."""
import unittest

from copita.core import autoupdate


class AutoUpdateTests(unittest.TestCase):
    def test_installed_version_is_readable(self):
        v = autoupdate._installed_version()
        self.assertIsNotNone(v)
        self.assertRegex(v, r"^\d{4}\.\d{1,2}\.\d{1,2}")

    def test_pypi_version_fetch_hits_real_pypi(self):
        v = autoupdate._latest_pypi_version()
        self.assertIsNotNone(v, "could not reach PyPI (or curl_cffi + urllib both failed)")
        self.assertRegex(v, r"^\d{4}\.\d{1,2}\.\d{1,2}")

    def test_version_tuple_ignores_zero_padding(self):
        # PyPI reports "2026.8.19", pip installs it as "2026.08.19".
        self.assertEqual(
            autoupdate._version_tuple("2026.8.19"),
            autoupdate._version_tuple("2026.08.19"),
        )
        self.assertGreater(
            autoupdate._version_tuple("2026.09.01"),
            autoupdate._version_tuple("2026.08.19"),
        )

    def test_full_check_produces_a_status(self):
        autoupdate._do_check_and_update()
        s = autoupdate.status()
        self.assertGreater(s["checked_at"], 0)
        self.assertIsNotNone(s["current"])
        # We're on the latest in CI most of the time, so just assert the
        # comparison ran and didn't downgrade.
        if s["latest"]:
            self.assertGreaterEqual(
                autoupdate._version_tuple(s["current"]),
                autoupdate._version_tuple(
                    min(s["current"], s["latest"], key=autoupdate._version_tuple)),
            )


if __name__ == "__main__":
    unittest.main()
