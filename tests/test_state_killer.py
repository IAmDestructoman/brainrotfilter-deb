"""
test_state_killer.py - Unit tests for the Linux state_killer module.
"""

from __future__ import annotations

from unittest.mock import patch


class TestIsYoutubeCdnIp:
    def test_google_cdn_prefix_matches(self):
        from state_killer import _is_youtube_cdn_ip

        assert _is_youtube_cdn_ip("142.250.80.46") is True
        assert _is_youtube_cdn_ip("172.217.14.110") is True
        assert _is_youtube_cdn_ip("216.58.214.206") is True

    def test_non_google_ip_does_not_match(self):
        from state_killer import _is_youtube_cdn_ip

        assert _is_youtube_cdn_ip("8.8.8.8") is False
        assert _is_youtube_cdn_ip("1.1.1.1") is False
        assert _is_youtube_cdn_ip("192.168.1.1") is False

    def test_private_ip_not_google(self):
        from state_killer import _is_youtube_cdn_ip

        assert _is_youtube_cdn_ip("10.0.0.1") is False
        assert _is_youtube_cdn_ip("127.0.0.1") is False


class TestIsPrivateIp:
    def test_rfc1918_ranges(self):
        from state_killer import _is_private_ip

        assert _is_private_ip("10.0.0.1") is True
        assert _is_private_ip("192.168.1.1") is True
        assert _is_private_ip("172.16.0.1") is True
        assert _is_private_ip("127.0.0.1") is True

    def test_public_ip_not_private(self):
        from state_killer import _is_private_ip

        assert _is_private_ip("8.8.8.8") is False
        assert _is_private_ip("142.250.80.46") is False


class TestKillStatesForVideo:
    @patch("state_killer._run_conntrack")
    def test_empty_client_ip_returns_false(self, mock_conntrack):
        from state_killer import kill_states_for_video

        success, killed = kill_states_for_video("")
        assert success is False
        assert killed == 0
        mock_conntrack.assert_not_called()

    @patch("state_killer._run_conntrack")
    def test_no_connections_found(self, mock_conntrack):
        from state_killer import kill_states_for_video

        # conntrack -L returns no output
        mock_conntrack.return_value = (0, "", "")

        success, killed = kill_states_for_video("192.168.1.5")
        assert success is True
        assert killed == 0

    @patch("state_killer._run_conntrack")
    def test_kills_youtube_connection(self, mock_conntrack):
        from state_killer import kill_states_for_video

        conntrack_output = (
            "tcp  6 300 ESTABLISHED src=192.168.1.5 dst=142.250.80.46 "
            "sport=54321 dport=443 src=142.250.80.46 dst=192.168.1.5 "
            "sport=443 dport=54321 [ASSURED] mark=0 use=1\n"
        )

        def side_effect(args, timeout=10):
            if "-L" in args:
                return (0, conntrack_output, "")
            if "-D" in args:
                return (0, "", "")
            return (0, "", "")

        mock_conntrack.side_effect = side_effect

        success, killed = kill_states_for_video("192.168.1.5", video_id="test123")
        assert success is True
        assert killed == 1


class TestIsConntrackAvailable:
    @patch("state_killer._run_conntrack")
    def test_available_when_rc_0(self, mock_conntrack):
        from state_killer import is_conntrack_available

        mock_conntrack.return_value = (0, "42", "")
        assert is_conntrack_available() is True

    @patch("state_killer._run_conntrack")
    def test_not_available_when_rc_nonzero(self, mock_conntrack):
        from state_killer import is_conntrack_available

        mock_conntrack.return_value = (-1, "", "conntrack not found")
        assert is_conntrack_available() is False
