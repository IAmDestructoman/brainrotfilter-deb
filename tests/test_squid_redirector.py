"""
test_squid_redirector.py - Unit tests for the Python Squid redirector module.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch


class TestExtractVideoId:
    def test_watch_url(self):
        from squid_redirector import extract_video_id

        assert extract_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"

    def test_watch_url_with_extra_params(self):
        from squid_redirector import extract_video_id

        assert extract_video_id("https://www.youtube.com/watch?v=abc123&t=30") == "abc123"

    def test_shorts_url(self):
        from squid_redirector import extract_video_id

        assert extract_video_id("https://www.youtube.com/shorts/xyz789") == "xyz789"

    def test_embed_url(self):
        from squid_redirector import extract_video_id

        assert extract_video_id("https://www.youtube.com/embed/test456") == "test456"

    def test_youtu_be_url(self):
        from squid_redirector import extract_video_id

        assert extract_video_id("https://youtu.be/short123") == "short123"

    def test_non_youtube_url_returns_none(self):
        from squid_redirector import extract_video_id

        result = extract_video_id("https://www.example.com/page")
        assert result is None

    def test_empty_url_returns_none(self):
        from squid_redirector import extract_video_id

        result = extract_video_id("")
        assert result is None
