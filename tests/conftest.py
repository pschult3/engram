from __future__ import annotations

import os
from pathlib import Path

import pytest

from engram.config import Config
from engram.storage import EventQueue, Store


@pytest.fixture()
def tmp_config(tmp_path: Path) -> Config:
    cfg = Config(home=tmp_path, project="test", log_level="INFO")
    cfg.ensure_dirs()
    # Make sure ENGRAM_HOME points at the temp dir for any subprocess-y
    # code paths that re-call load_config().
    os.environ["ENGRAM_HOME"] = str(tmp_path)
    os.environ["ENGRAM_PROJECT"] = "test"
    return cfg


@pytest.fixture()
def store(tmp_config: Config) -> Store:
    s = Store.open(tmp_config.db_path, tmp_config.project)
    yield s
    s.close()


@pytest.fixture()
def queue(tmp_config: Config) -> EventQueue:
    return EventQueue(tmp_config.queue_path)
