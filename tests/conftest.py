import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the database and episode folder at a temporary directory."""
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "cruftcast.db")
    monkeypatch.setattr(db, "EPISODES_DIR", tmp_path / "episodes")
    db.init_db()
    return tmp_path
