"""Live smoke-check of the ``api:`` source kind against real public APIs.

NOT part of the pytest suite (network tests don't belong there) — run by hand:

    .venv/Scripts/python.exe tests/live_api_check.py

Exercises every pagination style, auth (TMDB Bearer token via auth_env), nested-schema
ingestion, cross-source joins over snapshots, and the auth failure modes. Cases whose
required env var is absent are skipped, so the script runs for everyone; a repo-root
``.env`` file (KEY=VALUE lines) is loaded first — values are exported to the process
environment only, never printed.
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback

from spelunk.core.duck import DuckSession

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv() -> None:
    """Export KEY=VALUE lines from the repo-root .env (existing env vars win)."""
    path = os.path.join(_REPO_ROOT, ".env")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


CASES = [
    # (label, spec, min_rows_expected, required_env_var_or_None)
    (
        "plain JSON array, no pagination (JSONPlaceholder)",
        "posts=api:https://jsonplaceholder.typicode.com/posts",
        100,
        None,
    ),
    (
        "cursor pagination via full next-URL (PokeAPI)",
        "pokemon=api:https://pokeapi.co/api/v2/pokemon?limit=100 "
        "records=results paginate=cursor cursor_path=next max_pages=3",
        300,
        None,
    ),
    (
        "offset/limit pagination (PokeAPI berries)",
        "berries=api:https://pokeapi.co/api/v2/berry "
        "records=results paginate=offset page_size=20 size_param=limit max_pages=3",
        41,
        None,
    ),
    (
        "offset pagination with custom param names skip/limit (DummyJSON)",
        "products=api:https://dummyjson.com/products "
        "records=products paginate=offset offset_param=skip size_param=limit "
        "page_size=100 max_pages=5",
        150,
        None,
    ),
    (
        "link-header pagination (GitHub issues)",
        "gh_issues=api:https://api.github.com/repos/duckdb/duckdb/issues?per_page=30 "
        "paginate=link max_pages=2",
        31,
        None,
    ),
    (
        "plain array with scalar-ish records (Datamuse)",
        "words=api:https://api.datamuse.com/words?ml=database",
        10,
        None,
    ),
    (
        "page-number pagination (Art Institute of Chicago)",
        "artworks=api:https://api.artic.edu/api/v1/artworks "
        "records=data paginate=page size_param=limit page_size=100 max_pages=2",
        150,
        None,
    ),
    (
        "cursor at a NESTED dot path (Rick and Morty, info.next)",
        "characters=api:https://rickandmortyapi.com/api/character "
        "records=results paginate=cursor cursor_path=info.next max_pages=3",
        41,
        None,
    ),
    (
        "wide nested array + 0-based page pagination (TVMaze shows)",
        "shows=api:https://api.tvmaze.com/shows "
        "paginate=page start=0 max_pages=2",
        300,
        None,
    ),
    (
        "single-object response -> one record (Open-Meteo)",
        "weather=api:https://api.open-meteo.com/v1/forecast?latitude=-33.87&longitude=151.21"
        "&current=temperature_2m,wind_speed_10m",
        1,
        None,
    ),
    (
        "OData v4 with $filter/$select pushed to the service (Northwind)",
        "big_freight=api:https://services.odata.org/V4/Northwind/Northwind.svc/Orders"
        "?$filter=Freight%20gt%20500&$select=OrderID,Freight,ShipCountry paginate=odata",
        5,
        None,
    ),
    (
        "SQL filter= translated to OData $filter server-side (Northwind)",
        "nw_germany=api:https://services.odata.org/V4/Northwind/Northwind.svc/Orders "
        "paginate=odata select=OrderID,Freight,ShipCountry "
        "filter=\"Freight > 100 AND ShipCountry = 'Germany'\"",
        10,
        None,
    ),
    (
        "OData v4 server-driven paging via @odata.nextLink (Northwind)",
        "nw_orders=api:https://services.odata.org/V4/Northwind/Northwind.svc/Orders"
        "?$select=OrderID,CustomerID,Freight paginate=odata max_pages=3",
        41,
        None,
    ),
    (
        "API key as env-injected query param, scrubbed from logs (NASA APOD)",
        "apod=api:https://api.nasa.gov/planetary/apod?count=5 param=api_key:NASA_KEY",
        5,
        "NASA_KEY",
    ),
    (
        "API key as env-injected custom header (NASA APOD, X-Api-Key)",
        "apod_hdr=api:https://api.nasa.gov/planetary/apod?count=3 header=X-Api-Key:NASA_KEY",
        3,
        "NASA_KEY",
    ),
    (
        "Bearer auth + page pagination (TMDB popular movies)",
        "tmdb_movies=api:https://api.themoviedb.org/3/movie/popular "
        "records=results paginate=page max_pages=3 auth_env=TMDB_API_READ_ACCESS_TOKEN",
        41,
        "TMDB_API_READ_ACCESS_TOKEN",
    ),
    (
        "Bearer auth, wrapped list (TMDB genre list)",
        "tmdb_genres=api:https://api.themoviedb.org/3/genre/movie/list "
        "records=genres auth_env=TMDB_API_READ_ACCESS_TOKEN",
        10,
        "TMDB_API_READ_ACCESS_TOKEN",
    ),
]


def _run_cases(session: DuckSession) -> tuple[int, set[str]]:
    failures, attached = 0, set()
    for label, spec, min_rows, needs_env in CASES:
        if needs_env and not os.environ.get(needs_env):
            print(f"[SKIP] {label}: {needs_env} not set")
            continue
        try:
            out = session.add_source(spec)
            info = out["info"]
            ok = info["row_count"] >= min_rows
            if not ok:
                failures += 1
            print(
                f"[{'OK ' if ok else 'LOW'}] {label}: {info['row_count']} rows, "
                f"{info['pages']} page(s)" + (" [truncated]" if info.get("truncated") else "")
            )
            cols = session.query(f'SELECT * FROM "{out["name"]}" LIMIT 3', name="peek")["columns"]
            print(f"      columns: {[c['name'] for c in cols][:8]}")
            attached.add(out["name"])
        except Exception as exc:
            failures += 1
            print(f"[ERR] {label}: {exc}")
            traceback.print_exc()
    return failures, attached


def _run_joins(session: DuckSession, attached: set[str]) -> int:
    """Cross-source joins over the snapshots — the point of having them in one engine."""
    failures = 0
    joins = []
    if {"posts", "words"} <= attached:
        joins.append((
            "public cross-API join (posts x words)",
            "SELECT p.userId, COUNT(*) AS posts, MAX(w.word) AS a_word "
            "FROM posts p CROSS JOIN (SELECT word FROM words LIMIT 1) w "
            "GROUP BY p.userId ORDER BY p.userId LIMIT 5",
        ))
    if "nw_germany" in attached:
        joins.append((
            "translated $filter verified locally (every row satisfies the predicate)",
            "SELECT CASE WHEN COUNT(*) = 0 THEN 'server applied the filter' "
            "ELSE error('rows violating the translated filter: ' || COUNT(*)) END AS verdict "
            "FROM nw_germany WHERE NOT (Freight > 100 AND ShipCountry = 'Germany')",
        ))
    if {"tmdb_movies", "tmdb_genres"} <= attached:
        joins.append((
            "authenticated join with UNNEST (TMDB movies x genres)",
            "SELECT g.name AS genre, COUNT(*) AS movies, ROUND(AVG(m.vote_average), 2) AS avg_vote "
            "FROM tmdb_movies m, UNNEST(m.genre_ids) AS t(gid) "
            "JOIN tmdb_genres g ON t.gid = g.id "
            "GROUP BY g.name ORDER BY movies DESC LIMIT 5",
        ))
    for label, sql in joins:
        try:
            out = session.query(sql, name="joined", description=label)
            print(f"[OK ] {label}: {out['row_count']} rows, top: {out['sample'][0]}")
        except Exception as exc:
            failures += 1
            print(f"[ERR] {label}: {exc}")
            traceback.print_exc()
    return failures


_TMDB_SPEC = r"C:\Users\shaun\repo\movie-tracker\tmdb-api.json"


def _run_openapi_loop(session: DuckSession) -> int:
    """The full agent loop on a real 148-path spec: catalog -> suggested_spec -> attach -> SQL.

    Runs only when the local TMDB spec file and the TMDB token are both available."""
    if not os.path.isfile(_TMDB_SPEC) or not os.environ.get("TMDB_API_READ_ACCESS_TOKEN"):
        print("[SKIP] openapi catalog loop: TMDB spec file or token not available")
        return 0
    try:
        out = session.add_source(f"openapi:{_TMDB_SPEC}")
        n = out["info"]["endpoints"]
        ok = n >= 100
        print(f"[{'OK ' if ok else 'ERR'}] openapi: catalog attached: {n} endpoints")
        # Agent move: find an endpoint by SQL, take its ready-made spec, fill in the env var.
        row = session.query(
            "SELECT suggested_spec FROM tmdb_api WHERE method = 'get' "
            "AND path = '/3/movie/top_rated'",
            name="pick",
        )["sample"]
        spec = "top_rated=" + row[0][0].replace("<SET_ME>", "TMDB_API_READ_ACCESS_TOKEN")
        spec += " max_pages=2"
        attached = session.add_source(spec)
        rows = attached["info"]["row_count"]
        got = session.query(
            "SELECT title FROM top_rated ORDER BY vote_average DESC LIMIT 1", name="best"
        )["sample"]
        print(f"[OK ] openapi->api loop: {rows} rows fetched via suggested_spec, top: {got[0][0]}")
        return 0 if ok else 1
    except Exception as exc:
        print(f"[ERR] openapi catalog loop: {exc}")
        traceback.print_exc()
        return 1


def _run_auth_failures(session: DuckSession) -> int:
    """The auth failure modes must fail fast with actionable errors, not hang or mangle."""
    failures = 0
    os.environ["SPELUNK_BAD_TOKEN"] = "not-a-real-token"
    try:
        session.add_source(
            "bad=api:https://api.themoviedb.org/3/movie/popular auth_env=SPELUNK_BAD_TOKEN"
        )
        failures += 1
        print("[ERR] invalid bearer token: expected HTTP 401 error, attach succeeded")
    except ValueError as exc:
        ok = "401" in str(exc)
        failures += 0 if ok else 1
        print(f"[{'OK ' if ok else 'ERR'}] invalid bearer token -> clean error: {str(exc)[:80]}")
    finally:
        del os.environ["SPELUNK_BAD_TOKEN"]

    try:
        session.add_source(
            "bad=api:https://api.themoviedb.org/3/movie/popular auth_env=SPELUNK_UNSET_VAR"
        )
        failures += 1
        print("[ERR] missing auth env var: expected error, attach succeeded")
    except ValueError as exc:
        ok = "SPELUNK_UNSET_VAR" in str(exc)
        failures += 0 if ok else 1
        print(f"[{'OK ' if ok else 'ERR'}] missing auth env var -> clean error: {str(exc)[:80]}")
    return failures


def main() -> int:
    _load_dotenv()
    # NASA's published public demo key — fine to default (30 req/hr/IP); a real key in the
    # environment or .env wins.
    os.environ.setdefault("NASA_KEY", "DEMO_KEY")
    failures = 0
    with tempfile.TemporaryDirectory(prefix="spelunk_live_") as tmp:
        session = DuckSession.open([], session_dir=tmp)
        try:
            case_failures, attached = _run_cases(session)
            failures += case_failures
            failures += _run_joins(session, attached)
            failures += _run_openapi_loop(session)
            failures += _run_auth_failures(session)
        finally:
            session.close()
    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} FAILURE(S)'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
