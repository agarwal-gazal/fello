"""Turns a feature table into the matrix the model sees. Shared by training and scoring so they cannot drift apart."""
from __future__ import annotations

import numpy as np
import pandas as pd

KEYS = ["tenant_id", "account_id", "snap_at", "renewal_date", "outcome", "segment", "y"]
SEGMENTS = ["smb", "mid_market", "enterprise"]
# human-readable names and groups: drivers shown to a CSM are built from these
GROUPS = {"usage": ("p_",), "support": ("tk_",), "email": ("em", "champion_"), "billing": ("pay_fail", "seat_drops", "has_billing"),
          "account": ("age_days", "log_employees", "seg_", "k_weeks")}


def group_of(col: str) -> str:
    for g, prefixes in GROUPS.items():
        if col.startswith(prefixes) or col in prefixes:
            return g
    return "other"


def numeric_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if c not in KEYS and c not in ("tenant_id",) and pd.api.types.is_numeric_dtype(df[c])]


def design(df: pd.DataFrame, columns: list[str] | None = None) -> pd.DataFrame:
    """Raw numeric features + segment one-hot. Counts are sign-log transformed later, inside the pipeline."""
    X = df[[c for c in (columns or numeric_columns(df)) if not c.startswith("seg_")]].copy()
    seg = df["segment"].fillna("").str.lower().str.replace("-", "_")
    for s in SEGMENTS:
        X[f"seg_{s}"] = (seg == s).astype(float)
    return X.astype(float)
