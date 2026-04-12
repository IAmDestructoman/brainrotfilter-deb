"""
test_keyword_analyzer.py - Unit tests for the keyword analysis engine.

Tests:
  - KeywordList loading from file
  - Case-insensitive matching
  - Partial/phrase matching
  - Emoji detection
  - Score calculation
  - Music category dampening
  - Brainrot vs normal video discrimination
"""

from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import patch

# conftest adds the source directory to sys.path


# ---------------------------------------------------------------------------
# KeywordList
# ---------------------------------------------------------------------------


class TestKeywordList:
    def test_loads_from_file(self, tmp_keywords_file):
        from keyword_analyzer import KeywordList

        kl = KeywordList(path=str(tmp_keywords_file))
        assert len(kl.keywords) > 0, "Should load at least one non-emoji keyword"

    def test_returns_empty_when_file_missing(self, tmp_path):
        from keyword_analyzer import KeywordList

        kl = KeywordList(path=str(tmp_path / "nonexistent.json"))
        assert kl.keywords == []
        assert kl.emoji_keywords == []

    def test_non_emoji_keywords_in_main_list(self, tmp_keywords_file):
        from keyword_analyzer import KeywordList

        kl = KeywordList(path=str(tmp_keywords_file))
        kw_words = [k["keyword"] for k in kl.keywords]
        assert "skibidi" in kw_words
        assert "rizz" in kw_words

    def test_reload_updates_list(self, tmp_keywords_file):
        from keyword_analyzer import KeywordList

        kl = KeywordList(path=str(tmp_keywords_file))
        original_count = len(kl.keywords)

        data = json.loads(tmp_keywords_file.read_text())
        data["categories"]["slang"].append({"keyword": "newterm", "weight": 5.0})
        tmp_keywords_file.write_text(json.dumps(data))

        kl.reload()
        assert len(kl.keywords) == original_count + 1

    def test_malformed_json_returns_empty(self, tmp_path):
        from keyword_analyzer import KeywordList

        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{this is not valid json}")

        kl = KeywordList(path=str(bad_file))
        assert kl.keywords == []


# ---------------------------------------------------------------------------
# _search_text (case-insensitive matching)
# ---------------------------------------------------------------------------


class TestSearchText:
    def test_case_insensitive_match(self, tmp_keywords_file):
        from keyword_analyzer import KeywordList, _search_text

        kl = KeywordList(path=str(tmp_keywords_file))
        matches = _search_text("SKIBIDI TOILET IS HERE", kl.keywords, "title")
        kw_names = [m.keyword for m in matches]
        assert "skibidi" in kw_names

    def test_lowercase_match(self, tmp_keywords_file):
        from keyword_analyzer import KeywordList, _search_text

        kl = KeywordList(path=str(tmp_keywords_file))
        matches = _search_text("rizz god compilation", kl.keywords, "title")
        kw_names = [m.keyword for m in matches]
        assert "rizz" in kw_names

    def test_no_match_on_clean_text(self, tmp_keywords_file):
        from keyword_analyzer import KeywordList, _search_text

        kl = KeywordList(path=str(tmp_keywords_file))
        matches = _search_text("Introduction to quantum computing", kl.keywords, "title")
        assert len(matches) == 0

    def test_phrase_match(self, tmp_keywords_file):
        from keyword_analyzer import KeywordList, _search_text

        kl = KeywordList(path=str(tmp_keywords_file))
        matches = _search_text("this is no cap the best thing ever", kl.keywords, "title")
        kw_names = [m.keyword for m in matches]
        assert "no cap" in kw_names

    def test_empty_text_returns_empty(self, tmp_keywords_file):
        from keyword_analyzer import KeywordList, _search_text

        kl = KeywordList(path=str(tmp_keywords_file))
        matches = _search_text("", kl.keywords, "title")
        assert matches == []

    def test_multiple_keywords_detected(self, tmp_keywords_file):
        from keyword_analyzer import KeywordList, _search_text

        kl = KeywordList(path=str(tmp_keywords_file))
        matches = _search_text("skibidi rizz sigma ohio", kl.keywords, "title")
        kw_names = {m.keyword for m in matches}
        assert {"skibidi", "rizz", "sigma", "ohio"}.issubset(kw_names)


# ---------------------------------------------------------------------------
# _compute_score
# ---------------------------------------------------------------------------


class TestComputeScore:
    def test_empty_matches_returns_zero(self):
        from keyword_analyzer import _compute_score

        assert _compute_score([]) == 0.0

    def test_single_high_weight_match(self):
        from keyword_analyzer import _compute_score, _MAX_RAW_SCORE
        from models import KeywordMatch

        matches = [KeywordMatch(keyword="skibidi", weight=8.0, context="title")]
        score = _compute_score(matches)
        expected = round((8.0 / _MAX_RAW_SCORE) * 100.0, 2)
        assert score == expected

    def test_score_caps_at_100(self):
        from keyword_analyzer import _compute_score
        from models import KeywordMatch

        matches = [
            KeywordMatch(keyword=f"kw{i}", weight=10.0, context="title")
            for i in range(20)
        ]
        score = _compute_score(matches)
        assert score == 100.0

    def test_score_minimum_is_zero(self):
        from keyword_analyzer import _compute_score
        from models import KeywordMatch

        matches = [KeywordMatch(keyword="x", weight=0.0, context="title")]
        assert _compute_score(matches) == 0.0


# ---------------------------------------------------------------------------
# Full analyze() integration tests
# ---------------------------------------------------------------------------


class TestAnalyzeFunction:
    def test_brainrot_title_scores_high(self, tmp_keywords_file, mock_yt_api):
        with patch("keyword_analyzer.KEYWORDS_PATH", str(tmp_keywords_file)):
            with patch("keyword_analyzer._ocr_thumbnail", return_value=""):
                from keyword_analyzer import analyze

                result = analyze(
                    video_id="br01",
                    title="SKIBIDI TOILET RIZZ COMPILATION sigma only in ohio",
                    category_id="22",
                    fetch_captions=False,
                )

        assert result.score > 20.0, f"Expected high brainrot score, got {result.score}"

    def test_normal_title_scores_low(self, tmp_keywords_file, mock_yt_api):
        with patch("keyword_analyzer.KEYWORDS_PATH", str(tmp_keywords_file)):
            with patch("keyword_analyzer._ocr_thumbnail", return_value=""):
                from keyword_analyzer import analyze

                result = analyze(
                    video_id="edu01",
                    title="Introduction to Quantum Computing - Lecture 3",
                    description="In this lecture we cover quantum entanglement.",
                    category_id="27",
                    fetch_captions=False,
                )

        assert result.score < 5.0, f"Expected low score for educational content, got {result.score}"

    def test_result_module_is_keyword(self, tmp_keywords_file, mock_yt_api):
        with patch("keyword_analyzer.KEYWORDS_PATH", str(tmp_keywords_file)):
            with patch("keyword_analyzer._ocr_thumbnail", return_value=""):
                from keyword_analyzer import analyze

                result = analyze(video_id="test", title="test video", fetch_captions=False)

        assert result.module == "keyword"

    def test_result_score_between_0_and_100(self, tmp_keywords_file, mock_yt_api):
        with patch("keyword_analyzer.KEYWORDS_PATH", str(tmp_keywords_file)):
            with patch("keyword_analyzer._ocr_thumbnail", return_value=""):
                from keyword_analyzer import analyze

                result = analyze(
                    video_id="test",
                    title="skibidi rizz sigma ohio gyatt no cap",
                    fetch_captions=False,
                )

        assert 0.0 <= result.score <= 100.0
