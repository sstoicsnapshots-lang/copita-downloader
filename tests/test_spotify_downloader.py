"""Spotify downloader: podcast episodes come from the show's real RSS feed,
not a "closest guess" YouTube video; audiobooks are refused clearly; music
tracks still use metadata-matching.
"""
import unittest
from unittest.mock import patch

from copita.core.spotify_downloader import SpotifyDownloader, parse_spotify_url, _parse_hhmmss


_FEED = """<rss><channel><title>A Show</title>
<item><title>Episode Two: The Good One</title>
  <enclosure url="https://cdn.example.com/ep2.mp3" type="audio/mpeg" length="100"/>
  <itunes:duration>0:20:00</itunes:duration></item>
<item><title>Episode One</title>
  <enclosure url="https://cdn.example.com/ep1.mp3" type="audio/mpeg"/>
  <itunes:duration>15:00</itunes:duration></item>
</channel></rss>"""


class ParseTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(parse_spotify_url("https://open.spotify.com/episode/abc123")[0], "episode")
        self.assertEqual(parse_spotify_url("https://open.spotify.com/intl-de/audiobook/xy")[0], "audiobook")
        self.assertEqual(parse_spotify_url("spotify:track:zz")[0], "track")

    def test_hhmmss(self):
        self.assertEqual(_parse_hhmmss("1:02:03"), 3723)
        self.assertEqual(_parse_hhmmss("12:30"), 750)
        self.assertEqual(_parse_hhmmss("743"), 743)


class PodcastFeedTests(unittest.TestCase):
    def setUp(self):
        self.s = SpotifyDownloader("/tmp/sp_test_unused")

    def test_resolves_episode_from_feed_by_title(self):
        with patch.object(SpotifyDownloader, "_find_podcast_feed", return_value="https://feed"), \
             patch.object(SpotifyDownloader, "_http_get", return_value=_FEED.encode()):
            url = self.s._resolve_podcast_episode("A Show", "Episode Two: The Good One", 1200)
        self.assertEqual(url, "https://cdn.example.com/ep2.mp3")

    def test_returns_none_when_no_feed_found(self):
        with patch.object(SpotifyDownloader, "_find_podcast_feed", return_value=None):
            self.assertIsNone(self.s._resolve_podcast_episode("Nope", "x", 0))

    def test_returns_none_when_no_episode_matches(self):
        with patch.object(SpotifyDownloader, "_find_podcast_feed", return_value="https://feed"), \
             patch.object(SpotifyDownloader, "_http_get", return_value=_FEED.encode()):
            self.assertIsNone(
                self.s._resolve_podcast_episode("A Show", "A completely unrelated title", 0))


class DownloadRoutingTests(unittest.TestCase):
    def setUp(self):
        self.s = SpotifyDownloader("/tmp/sp_test_unused")

    def test_audiobook_is_refused_with_a_clear_message(self):
        with self.assertRaises(ValueError) as cm:
            self.s.download_track("https://open.spotify.com/audiobook/abc")
        self.assertIn("DRM", str(cm.exception))

    def test_episode_never_falls_back_to_youtube(self):
        info = {"kind": "episode", "title": "Ep", "artist": "Show", "cover_url": None,
                "preview_url": "https://p/clip.mp3", "duration": 1800}
        with patch.object(SpotifyDownloader, "get_item_info", return_value=info), \
             patch.object(SpotifyDownloader, "_resolve_podcast_episode", return_value=None), \
             patch.object(SpotifyDownloader, "_search_provider") as search:
            with self.assertRaises(RuntimeError) as cm:
                self.s.download_track("https://open.spotify.com/episode/abc")
        search.assert_not_called()
        self.assertIn("Spotify-exclusive", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
