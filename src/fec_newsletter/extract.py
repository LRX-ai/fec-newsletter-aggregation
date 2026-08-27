"""Stage 1 -- pull source tables out of Postgres and cache them locally.

The aggregation is deliberately NOT pushed into SQL. The FEC tables carry no indexes and
the server dropped connections twice under moderate server-side joins, so the proven shape
is: extract once with COPY (~253k rows, ~35 MB, ~40s), then transform locally.
"""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import psycopg2
from dotenv import load_dotenv

from . import config


def connect():
    """Open a connection using credentials from the repo-root .env."""
    load_dotenv(config.REPO_ROOT / ".env")
    missing = [k for k in ("POSTGRES_USER", "POSTGRES_PASSWORD", "PGHOST") if not os.getenv(k)]
    if missing:
        raise RuntimeError(
            f"Missing required env var(s): {', '.join(missing)}. "
            f"Copy .env.example to .env and fill them in."
        )
    return psycopg2.connect(
        host=os.environ["PGHOST"],
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.getenv("PGDATABASE", config.DB_NAME),
    )


def _copy_to_csv(conn, sql: str, dest: Path) -> None:
    """Stream a query straight to a CSV file via COPY."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    statement = sql.strip().rstrip(";")
    copy_sql = f"COPY ({statement}) TO STDOUT WITH (FORMAT CSV, HEADER TRUE)"
    tmp = dest.with_suffix(dest.suffix + ".partial")
    try:
        with conn.cursor() as cur, tmp.open("w", encoding="utf-8") as fh:
            cur.copy_expert(copy_sql, fh)
        tmp.replace(dest)  # atomic: a killed run never leaves a truncated cache file
    finally:
        tmp.unlink(missing_ok=True)


def extract_all(
    refresh: bool = False, verbose: bool = True, extracts: dict | None = None
) -> dict[str, Path]:
    """Materialise every configured extract to cache/, reusing files unless refreshing.

    `extracts` selects which set to pull (defaults to the OTH pipeline's). The two
    pipelines share lookup extracts by cache filename, so running one warms the other.
    """
    extracts = extracts if extracts is not None else config.EXTRACTS
    paths = {name: config.CACHE_DIR / cache for name, (_, cache) in extracts.items()}
    needed = [n for n, p in paths.items() if refresh or not p.exists()]

    if not needed:
        if verbose:
            print(f"cache hit for all {len(paths)} extracts (use --refresh to re-pull)")
        return paths

    conn = connect()
    try:
        for name in needed:
            sql_file, _ = extracts[name]
            sql = (config.SQL_DIR / sql_file).read_text()
            if verbose:
                print(f"  extracting {name} ...", end="", flush=True)
            _copy_to_csv(conn, sql, paths[name])
            if verbose:
                size_mb = paths[name].stat().st_size / 1e6
                print(f" {size_mb:.1f} MB")
    finally:
        conn.close()
    return paths


def load(paths: dict[str, Path]) -> dict[str, pd.DataFrame]:
    """Read cached extracts as all-string frames.

    Everything is text in the source tables and blank is meaningful (it means "not
    reported"), so NaN coercion is disabled -- the transform treats "" explicitly.
    """
    return {
        name: pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[])
        for name, path in paths.items()
    }
