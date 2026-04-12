"""
test_config.py - Unit tests for the Config class and settings helpers.

Tests:
  - Default values returned when no DB entries exist
  - score_to_status tier assignment
  - compute_combined_score weighting
  - Boundary conditions (scores at exact tier boundaries)
  - Config correctly reads overridden values from DB
"""

from __future__ import annotations

# conftest adds source to sys.path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config_with_db(db_manager):
    """Return a Config instance backed by the test database."""
    from config import Config

    return Config(db_path=str(db_manager.db_path))


# ---------------------------------------------------------------------------
# Default values
# ---------------------------------------------------------------------------


class TestDefaultValues:
    def test_keyword_threshold_default(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.keyword_threshold == 40

    def test_scene_threshold_default(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.scene_threshold == 50

    def test_audio_threshold_default(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.audio_threshold == 45

    def test_combined_threshold_default(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.combined_threshold == 45

    def test_monitor_score_min_default(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.monitor_score_min == 20

    def test_soft_block_score_min_default(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.soft_block_score_min == 35

    def test_block_score_min_default(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.block_score_min == 55

    def test_weights_default(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        w = cfg.weights
        assert abs(w["keyword"] - 0.25) < 0.001
        assert abs(w["scene"] - 0.20) < 0.001
        assert abs(w["audio"] - 0.15) < 0.001

    def test_service_port_default(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.service_port == 8199

    def test_channel_flag_percentage_default(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.channel_flag_percentage == 30


# ---------------------------------------------------------------------------
# score_to_status tier assignment
# ---------------------------------------------------------------------------


class TestScoreToStatus:
    def test_score_0_is_allow(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.score_to_status(0.0) == "allow"

    def test_score_below_monitor_is_allow(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.score_to_status(19.9) == "allow"

    def test_score_at_monitor_boundary_is_monitor(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.score_to_status(20.0) == "monitor"

    def test_score_in_monitor_range(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.score_to_status(27.0) == "monitor"

    def test_score_at_soft_block_boundary(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.score_to_status(35.0) == "soft_block"

    def test_score_in_soft_block_range(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.score_to_status(45.0) == "soft_block"

    def test_score_at_block_boundary(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.score_to_status(55.0) == "block"

    def test_score_100_is_block(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.score_to_status(100.0) == "block"

    def test_all_valid_statuses_covered(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        valid = {"allow", "monitor", "soft_block", "block"}
        for score in [0, 10, 19.9, 20, 25, 34.9, 35, 45, 54.9, 55, 80, 100]:
            result = cfg.score_to_status(score)
            assert result in valid, f"score_to_status({score}) = '{result}' not in {valid}"

    def test_score_below_0_treated_as_allow(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.score_to_status(-1.0) == "allow"


# ---------------------------------------------------------------------------
# compute_combined_score
# ---------------------------------------------------------------------------


class TestComputeCombinedScore:
    def test_zero_inputs_return_zero(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        assert cfg.compute_combined_score(0, 0, 0) == 0.0

    def test_keyword_weight_applied(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        score = cfg.compute_combined_score(keyword_score=100, scene_score=0, audio_score=0)
        # weight_keyword = 0.25
        assert abs(score - 25.0) < 0.5

    def test_scene_weight_applied(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        score = cfg.compute_combined_score(keyword_score=0, scene_score=100, audio_score=0)
        # weight_scene = 0.20
        assert abs(score - 20.0) < 0.5

    def test_audio_weight_applied(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        score = cfg.compute_combined_score(keyword_score=0, scene_score=0, audio_score=100)
        # weight_audio = 0.15
        assert abs(score - 15.0) < 0.5

    def test_result_clamped_to_100(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        score = cfg.compute_combined_score(200, 200, 200)
        assert score == 100.0

    def test_result_clamped_to_0(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        score = cfg.compute_combined_score(-50, -50, -50)
        assert score == 0.0

    def test_result_is_float(self, db_manager):
        cfg = _make_config_with_db(db_manager)
        score = cfg.compute_combined_score(50, 60, 40)
        assert isinstance(score, float)


# ---------------------------------------------------------------------------
# Config reads overridden values from DB
# ---------------------------------------------------------------------------


class TestConfigReadsFromDb:
    def test_overridden_threshold_reflected(self, db_manager):
        """After writing a setting to DB, Config should reflect the new value."""
        db_manager.set_setting("block_score_min", 70)
        cfg = _make_config_with_db(db_manager)
        assert cfg.block_score_min == 70

    def test_overridden_port_reflected(self, db_manager):
        db_manager.set_setting("service_port", 9000)
        cfg = _make_config_with_db(db_manager)
        assert cfg.service_port == 9000

    def test_score_to_status_uses_db_thresholds(self, db_manager):
        """Custom DB thresholds should change tier boundaries."""
        db_manager.set_setting("block_score_min", 80)
        cfg = _make_config_with_db(db_manager)
        assert cfg.score_to_status(65.0) != "block"
        assert cfg.score_to_status(85.0) == "block"
