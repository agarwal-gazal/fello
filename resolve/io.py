"""DataFrame <-> Postgres through psql \\copy (no Python driver available in the build sandbox)."""
from __future__ import annotations

import io
import subprocess

import pandas as pd

from ingest.load_raw import psql


def read_df(db_url: str, sql: str) -> pd.DataFrame:
    one_line = " ".join(sql.split())          # \copy needs the whole command on one line
    p = subprocess.run(["psql", db_url, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-c",
                        f"\\copy ({one_line}) to stdout with (format csv, header)"], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"psql failed:\n{p.stderr.strip()}")
    return pd.read_csv(io.StringIO(p.stdout), dtype=str, keep_default_na=False, na_values=[""])


def write_df(db_url: str, table: str, df: pd.DataFrame, ddl: str) -> None:
    cols = ", ".join(df.columns)
    csv = df.to_csv(index=False)
    psql(db_url, f"{ddl};\n\\copy {table} ({cols}) from stdin with (format csv, header)\n{csv}\\.\n")
