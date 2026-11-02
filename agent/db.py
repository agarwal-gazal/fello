from __future__ import annotations

from pathlib import Path

from ingest.load_raw import lit, psql

HERE = Path(__file__).parent


def ensure_schema(db_url: str) -> None:
    psql(db_url, (HERE / "schema.sql").read_text())


def tx(db_url: str, body: str) -> str:
    """Run a script as ONE transaction: all of it or none of it."""
    return psql(db_url, f"begin;\n{body}\ncommit;")


__all__ = ["ensure_schema", "tx", "lit", "psql"]
