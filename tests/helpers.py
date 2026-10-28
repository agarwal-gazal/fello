"""Shared test setup: build a small but real dataset through raw -> staging in a scratch database."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pandas as pd

from data_gen import generate
from ingest import build_staging, load_raw
from ingest.load_raw import psql

DB = os.environ.get("TEST_DATABASE_URL", "postgresql://fello:fello@localhost/fello_test")


def q(sql: str) -> str:
    return psql(DB, sql)


def build_dataset(scale: float = 0.05, seed: int = 3):
    tmp = Path(tempfile.mkdtemp(prefix="fello_test_"))
    data = tmp / "data"
    generate.main(["--out", str(data), "--seed", str(seed), "--scale", str(scale)])
    return tmp, data


def load_and_stage(data: Path):
    q("drop schema if exists raw cascade")
    load_raw.main(["--data", str(data), "--database-url", DB])
    build_staging.build(DB)


def truth_links(data: Path) -> pd.DataFrame:
    return pd.read_csv(data / "ground_truth" / "entity_links.csv", dtype=str)


def cleanup(tmp: Path):
    shutil.rmtree(tmp, ignore_errors=True)
