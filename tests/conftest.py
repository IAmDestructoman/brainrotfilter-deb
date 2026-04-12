"""
conftest.py - Shared pytest fixtures for BrainrotFilter test suite.

Provides:
  - temp_db          : isolated SQLite database per test
  - db_manager       : DatabaseManager backed by temp_db
  - mock_yt_api      : mock for youtube_api module
  - sample_metadata  : realistic video metadata dicts
  - brainrot_metadata: clearly brainrot video metadata
  - normal_metadata  : clearly non-brainrot video metadata
  - test_keywords    : minimal keyword list for deterministic tests
  - tmp_keywords_file: temporary keywords.json file
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Add the package source to sys.path so tests can import our modules
# ---------------------------------------------------------------------------
_SRC = str(Path(__file__).parent.parent / "src" / "brainrotfilter")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


# ---------------------------------------------------------------------------
# Minimal keyword data used in unit tests (avoids loading the real file)
# ---------------------------------------------------------------------------

MINIMAL_KEYWORDS: Dict[str, Any] = {
    "categories": {
        "slang": [
            {"keyword": "skibidi", "weight": 8.0},
            {"keyword": "rizz", "weight": 7.0},
            {"keyword": "gyatt", "weight": 7.5},
            {"keyword": "sigma", "weight": 6.0},
            {"keyword": "ohio", "weight": 5.0},
            {"keyword": "slay", "weight": 4.0},
            {"keyword": "based", "weight": 4.0},
            {"keyword": "no cap", "weight": 6.0},
        ],
        "phrases": [
            {"keyword": "only in ohio", "weight": 9.0},
            {"keyword": "rizz god", "weight": 8.5},
            {"keyword": "delulu is the solulu", "weight": 8.0},
            {"keyword": "brain rot", "weight": 7.0},
        ],
        "references": [
            {"keyword": "skibidi toilet", "weight": 9.0},
            {"keyword": "grimace shake", "weight": 7.5},
        ],
        "music_artists": [
            {"keyword": "playboi carti", "weight": 5.0},
            {"keyword": "lil tecca", "weight": 4.5},
        ],
        "emojis": [
            {"keyword": "\U0001f98b", "weight": 1.5},
        ],
    }
}

# Keywords that should always produce high scores for brainrot videos
BRAINROT_KEYWORDS_HIGH = ["skibidi", "rizz", "sigma", "ohio", "gyatt", "no cap"]

# Titles that definitely represent brainrot content
BRAINROT_TITLES = [
    "SKIBIDI TOILET COMPILATION (rizz edition)",
    "sigma male grindset - ohio only #nocap",
    "gyatt gyatt rizz rizz no cap skibidi ohio sigma",
    "skibidi toilet vs grimace shake | rizz god tier brain rot",
]

# Titles that should never trigger brainrot detection
NORMAL_TITLES = [
    "How to cook pasta carbonara | Italian recipe tutorial",
    "NASA's James Webb Telescope reveals new galaxy images",
    "Introduction to Python programming for beginners",
    "Classic piano sonata in C major - Beethoven",
]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Return path to a fresh temporary SQLite database file."""
    db_path = tmp_path / "test_brainrotfilter.db"
    return db_path


@pytest.fixture
def db_manager(temp_db: Path):
    """
    A DatabaseManager instance pointing at a fresh temporary database.

    The DB is initialized (schema created) on first use.
    """
    from db_manager import DatabaseManager

    manager = DatabaseManager(db_path=str(temp_db))
    manager.initialize()
    yield manager


@pytest.fixture
def tmp_keywords_file(tmp_path: Path) -> Path:
    """Write the minimal keyword set to a temp file and return its path."""
    kw_file = tmp_path / "keywords.json"
    kw_file.write_text(json.dumps(MINIMAL_KEYWORDS, ensure_ascii=False, indent=2))
    return kw_file


@pytest.fixture
def keyword_list(tmp_keywords_file: Path):
    """A KeywordList loaded from the minimal test keywords file."""
    from keyword_analyzer import KeywordList

    return KeywordList(path=str(tmp_keywords_file))


@pytest.fixture
def mock_yt_api():
    """
    Mock out all calls to youtube_api so tests do not hit the network.

    Returns a MagicMock that can be further configured per-test.
    """
    mock = MagicMock()
    mock.get_video_metadata.return_value = {
        "video_id": "dQw4w9WgXcQ",
        "title": "Test Video",
        "description": "A test description.",
        "tags": ["test", "video"],
        "channel_id": "UC_test_channel",
        "channel_title": "Test Channel",
        "category_id": "22",
        "thumbnail_url": "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg",
        "duration_seconds": 180,
        "view_count": 1000,
        "subscriber_count": 50000,
    }
    mock.get_video_captions.return_value = ""
    mock.get_channel_info.return_value = {
        "channel_id": "UC_test_channel",
        "channel_name": "Test Channel",
        "subscriber_count": 50000,
        "total_videos": 120,
        "upload_frequency": 2.0,
    }
    with patch.dict("sys.modules", {"youtube_api": mock}):
        yield mock


@pytest.fixture
def sample_video_metadata() -> Dict[str, Any]:
    """Realistic but neutral video metadata for general-purpose tests."""
    return {
        "video_id": "dQw4w9WgXcQ",
        "title": "Never Gonna Give You Up",
        "description": "Rick Astley's classic hit from 1987.",
        "tags": ["rick astley", "80s", "pop", "music"],
        "channel_id": "UCuAXFkgsw1L7xaCfnd5JJOw",
        "category_id": "10",  # Music
        "thumbnail_url": "",
    }


@pytest.fixture
def brainrot_video_metadata() -> Dict[str, Any]:
    """Video metadata that should score high on brainrot detection."""
    return {
        "video_id": "br41nr0t01",
        "title": "SKIBIDI TOILET RIZZ COMPILATION sigma only in ohio",
        "description": "no cap this is the most skibidi sigma grindset ohio rizz god video ever made brain rot compilation gyatt",
        "tags": ["skibidi", "rizz", "sigma", "ohio", "no cap", "gyatt", "brainrot"],
        "channel_id": "UC_brainrot_channel",
        "category_id": "22",  # People & Blogs
        "thumbnail_url": "",
    }


@pytest.fixture
def normal_video_metadata() -> Dict[str, Any]:
    """Video metadata that should score near zero on brainrot detection."""
    return {
        "video_id": "normalvideo1",
        "title": "Introduction to Quantum Computing - Lecture 3",
        "description": "In this lecture we cover quantum entanglement, superposition, "
                       "and the basics of quantum gates. Suitable for undergraduate students.",
        "tags": ["quantum computing", "physics", "lecture", "education"],
        "channel_id": "UC_edu_channel",
        "category_id": "27",  # Education
        "thumbnail_url": "",
    }


@pytest.fixture
def music_video_metadata() -> Dict[str, Any]:
    """Music video metadata -- should have dampening applied."""
    return {
        "video_id": "musicvideo1",
        "title": "Sigma Sigma on the wall - official music video",
        "description": "The latest hit featuring sigma and rizz vibes.",
        "tags": ["music", "sigma", "rizz", "pop"],
        "channel_id": "UC_music_channel",
        "category_id": "10",  # Music -- triggers dampening
        "thumbnail_url": "",
    }
