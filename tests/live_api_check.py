"""Live smoke-check of the ``api:`` source kind against real public APIs.

NOT part of the pytest suite (network tests don't belong there) — run by hand:

    .venv/Scripts/python.exe tests/live_api_check.py

Exercises each pagination style against a well-known public endpoint, then runs a
cross-source join to prove the snapshots behave as first-class DuckDB sources.
"""
from __future__ import annotations

import sys
import tempfile
import traceback

from spelunk.core.duck import DuckSession

CASES = [
    # (label, spec, min_rows_expected)
    (
        "plain JSON array, no pagination (JSONPlaceholder)",
        "posts=api:https://jsonplaceholder.typicode.com/posts",
        100,
    ),
    (
        "cursor pagination via full next-URL (PokeAPI)",
        "pokemon=api:https://pokeapi.co/api/v2/pokemon?limit=100 "
        "records=results paginate=cursor cursor_path=next max_pages=3",
        300,
    ),
    (
        "offset/limit pagination (PokeAPI berries)",
        "berries=api:https://pokeapi.co/api/v2/berry "
        "records=results paginate=offset page_size=20 size_param=limit max_pages=3",
        41,
    ),
    (
        "link-header pagination (GitHub issues)",
        "gh_issues=api:https://api.github.com/repos/duckdb/duckdb/issues?per_page=30 "
        "paginate=link max_pages=2",
        31,
    ),
    (
        "plain array with scalar-ish records (Datamuse)",
        "words=api:https://api.datamuse.com/words?ml=database",
        10,
    ),
    (
        "page-number pagination (Art Institute of Chicago)",
        "artworks=api:https://api.artic.edu/api/v1/artworks "
        "records=data paginate=page size_param=limit page_size=100 max_pages=2",
        150,
    ),
    (
        "single-object response -> one record (Open-Meteo)",
        "weather=api:https://api.open-meteo.com/v1/forecast?latitude=-33.87&longitude=151.21"
        "&current=temperature_2m,wind_speed_10m",
        1,
    ),
]


def main() -> int:
    failures = 0
    with tempfile.TemporaryDirectory(prefix="spelunk_live_") as tmp:
        session = DuckSession.open([], session_dir=tmp)
        try:
            for label, spec, min_rows in CASES:
                try:
                    out = session.add_source(spec)
                    info = out["info"]
                    ok = info["row_count"] >= min_rows
                    status = "OK " if ok else "LOW"
                    if not ok:
                        failures += 1
                    print(
                        f"[{status}] {label}: {info['row_count']} rows, "
                        f"{info['pages']} page(s)"
                        + (" [truncated]" if info.get("truncated") else "")
                    )
                    cols = session.query(
                        f'SELECT * FROM "{out["name"]}" LIMIT 3', name="peek"
                    )["columns"]
                    print(f"      columns: {[c['name'] for c in cols][:8]}")
                except Exception as exc:
                    failures += 1
                    print(f"[ERR] {label}: {exc}")
                    traceback.print_exc()

            # Cross-source proof: join two API snapshots in one SQL statement.
            try:
                out = session.query(
                    "SELECT p.userId, COUNT(*) AS posts, MAX(w.word) AS a_word "
                    "FROM posts p CROSS JOIN (SELECT word FROM words LIMIT 1) w "
                    "GROUP BY p.userId ORDER BY p.userId LIMIT 5",
                    name="joined",
                    description="posts per user joined against a datamuse word",
                )
                print(f"[OK ] cross-API join: {out['row_count']} rows, sample {out['sample'][0]}")
            except Exception as exc:
                failures += 1
                print(f"[ERR] cross-API join: {exc}")
        finally:
            session.close()
    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
