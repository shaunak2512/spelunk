"""Validate the claim register and report claim coverage.

The register (``evals/claims/*.yaml``) is the eval's denominator: every falsifiable assertion
the docs make, and whether anything would fail if it were false. This script checks the
register is well-formed, resolves every ``covered_by`` node id against the actual test files
(so a renamed test breaks the build instead of silently orphaning a claim), and prints
coverage by area and by falsification mode.

    python evals/coverage.py              # report
    python evals/coverage.py --strict     # non-zero exit on any problem
    python evals/coverage.py --unverified # just the gaps, for planning
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CLAIMS_DIR = Path(__file__).resolve().parent / "claims"
TESTS_DIR = ROOT / "tests"

MODES = {"invariant", "behavioral", "quantitative", "affordance"}
STATUSES = {"verified", "partial", "unverified", "refuted"}
REQUIRED = ("id", "claim", "source", "mode", "falsifier", "status")


def collect_test_ids(tests_dir: Path) -> set[str]:
    """Every pytest node id in the suite, as ``path::Class::test`` or ``path::test``.

    Parsed with ``ast`` rather than shelling out to pytest: no import side effects, no
    collection errors, and it works even when the suite is red.
    """
    found: set[str] = set()
    for path in sorted(tests_dir.rglob("test_*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name.startswith("test"):
                found.add(f"{rel}::{node.name}")
            elif isinstance(node, ast.ClassDef):
                for sub in node.body:
                    if isinstance(sub, ast.FunctionDef) and sub.name.startswith("test"):
                        found.add(f"{rel}::{node.name}::{sub.name}")
    return found


def load_register() -> tuple[list[dict], list[str]]:
    """Load every claim file. Returns (claims, structural problems)."""
    claims: list[dict] = []
    problems: list[str] = []
    seen_ids: set[str] = set()

    files = sorted(CLAIMS_DIR.glob("*.yaml"))
    if not files:
        problems.append(f"no claim files found under {CLAIMS_DIR}")

    for path in files:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            # A parse error is a register problem like any other — reporting it beside the
            # rest beats a traceback that hides whatever the other nine files say.
            problems.append(f"{path.name}: not valid YAML ({' '.join(str(exc).split())})")
            continue
        # Shape checks first: the register is the eval's denominator, so a file that silently
        # contributes zero claims (`claims: {}`, a scalar root, a typo'd key) would understate
        # the denominator and inflate every percentage in the report. Say so instead.
        if doc is None:
            problems.append(f"{path.name}: file is empty")
            continue
        if not isinstance(doc, dict):
            problems.append(f"{path.name}: top level is {type(doc).__name__}, expected a mapping")
            continue
        raw_claims = doc.get("claims")
        if raw_claims is None:
            problems.append(f"{path.name}: no 'claims' key")
            continue
        if not isinstance(raw_claims, list):
            problems.append(
                f"{path.name}: 'claims' is {type(raw_claims).__name__}, expected a list"
            )
            continue

        area = doc.get("area", path.stem)
        prefix = doc.get("prefix")
        for index, claim in enumerate(raw_claims):
            if not isinstance(claim, dict):
                problems.append(
                    f"{path.name}: claims[{index}] is {type(claim).__name__}, expected a mapping"
                )
                continue
            claim["_file"] = path.name
            claim["_area"] = area
            claims.append(claim)

            cid = claim.get("id", "<no id>")
            for field in REQUIRED:
                if not claim.get(field):
                    problems.append(f"{cid}: missing required field '{field}'")
            if cid in seen_ids:
                problems.append(f"{cid}: duplicate claim id")
            seen_ids.add(cid)
            if prefix and not str(cid).startswith(f"{prefix}-"):
                problems.append(f"{cid}: id does not match file prefix '{prefix}-'")
            if claim.get("mode") not in MODES:
                problems.append(f"{cid}: mode '{claim.get('mode')}' not in {sorted(MODES)}")
            if claim.get("status") not in STATUSES:
                problems.append(f"{cid}: status '{claim.get('status')}' not in {sorted(STATUSES)}")

    return claims, problems


def check_coverage(claims: list[dict], test_ids: set[str]) -> list[str]:
    """Rules that keep the register honest as the code moves under it."""
    problems: list[str] = []
    for claim in claims:
        cid = claim.get("id", "<no id>")
        covered = claim.get("covered_by") or []
        status = claim.get("status")

        if not isinstance(covered, list):
            # A bare string is the tempting typo, and iterating it would "resolve" one
            # character at a time — nonsense problems instead of the real one.
            problems.append(
                f"{cid}: covered_by is {type(covered).__name__}, expected a list of node ids"
            )
            continue

        for node_id in covered:
            if node_id not in test_ids:
                problems.append(f"{cid}: covered_by does not resolve -> {node_id}")

        if status == "verified" and not covered:
            problems.append(f"{cid}: status 'verified' with no covered_by")
        if status in {"partial", "unverified", "refuted"} and not claim.get("gap"):
            problems.append(f"{cid}: status '{status}' requires a 'gap' explaining what is unchecked")
        if status == "unverified" and covered:
            problems.append(f"{cid}: status 'unverified' but lists covered_by")
    return problems


def bar(part: int, whole: int, width: int = 24) -> str:
    filled = 0 if not whole else round(width * part / whole)
    return "#" * filled + "." * (width - filled)


def report(claims: list[dict], test_ids: set[str], unverified_only: bool) -> None:
    total = len(claims)
    by_status = Counter(c.get("status") for c in claims)
    verified = by_status["verified"]
    partial = by_status["partial"]

    if unverified_only:
        print("Claims with no falsifier\n" + "=" * 60)
        for claim in claims:
            if claim.get("status") == "unverified":
                print(f"\n{claim['id']}  [{claim['mode']}]  {claim['_file']}")
                print(f"  claim: {' '.join(str(claim['claim']).split())[:150]}")
                print(f"  gap:   {' '.join(str(claim.get('gap', '')).split())[:200]}")
        print(f"\n{by_status['unverified']} of {total} claims have no falsifier.")
        return

    areas = sorted({c["_area"] for c in claims})
    print("Spelunk claim register\n" + "=" * 72)
    print(f"{total} claims across {len(areas)} areas, "
          f"resolved against {len(test_ids)} tests\n")

    print("By status")
    for status in ("verified", "partial", "unverified", "refuted"):
        n = by_status[status]
        print(f"  {status:<12} {n:>3}  {bar(n, total)}  {n / total:>5.0%}")

    # Grouped by the DECLARED area, not the filename: two files may share one area, and a
    # file's name need not match the area it declares. Areas are prose, so the label is
    # collapsed and clipped to keep the columns aligned.
    print("\nBy area" + " " * 26 + "verified  partial  unverified")
    for name in areas:
        rows = [c for c in claims if c["_area"] == name]
        s = Counter(c.get("status") for c in rows)
        label = " ".join(str(name).split())
        label = label[:27] + "…" if len(label) > 28 else label
        print(f"  {label:<28} {len(rows):>3} claims  "
              f"{s['verified']:>5}  {s['partial']:>7}  {s['unverified']:>10}")

    print("\nBy falsification mode" + " " * 12 + "verified  partial  unverified")
    for mode in sorted(MODES):
        rows = [c for c in claims if c.get("mode") == mode]
        if not rows:
            continue
        s = Counter(c.get("status") for c in rows)
        print(f"  {mode:<28} {len(rows):>3} claims  "
              f"{s['verified']:>5}  {s['partial']:>7}  {s['unverified']:>10}")

    strong = verified / total
    any_cover = (verified + partial) / total
    print(f"\nFully verified:      {strong:.0%}  (a listed test fails if the claim is false)")
    print(f"Touched at all:      {any_cover:.0%}  (verified + partial)")
    print(f"No falsifier at all: {by_status['unverified'] / total:.0%}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strict", action="store_true", help="exit non-zero on any problem")
    parser.add_argument("--unverified", action="store_true", help="list only claims with no falsifier")
    args = parser.parse_args()

    claims, problems = load_register()
    test_ids = collect_test_ids(TESTS_DIR)
    problems += check_coverage(claims, test_ids)

    report(claims, test_ids, args.unverified)

    if problems:
        print(f"\n{len(problems)} register problem(s):")
        for problem in problems:
            print(f"  - {problem}")
        return 1 if args.strict else 0

    print("\nRegister is well-formed; every covered_by node id resolves.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
