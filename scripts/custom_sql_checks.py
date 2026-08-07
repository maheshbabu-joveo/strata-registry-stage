#!/usr/bin/env python3
"""Custom lint for strata-semantic-layer.

Runs three checks against `projects/`:
  1. Every project.yml is valid YAML with all three envs (preview/preprod/prod)
     each declaring database + schema + warehouse.
  2. Every semantic_views/*.yaml has a name, at least one table with
     base_table.{database,schema,table}, and at least one dimension or metric.
  3. Every base/*.sql and semantic_views/*.sql parses under sqlfluff's snowflake
     dialect. Parse failures on constructs sqlfluff doesn't yet support (native
     CREATE SEMANTIC VIEW) are treated as warnings, not failures — mirrors the
     idp-snowflake-sql-migrations linter behaviour.

Exit code is 1 if any hard failure was found, else 0.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECTS_DIR = REPO_ROOT / "projects"
REQUIRED_ENVS = ("preview", "preprod", "prod")
REQUIRED_SNOW_KEYS = ("database", "schema", "warehouse")

# Patterns where a sqlfluff parse-error is likely a real syntax problem, not a
# parser limitation. If the file starts with one of these, PRS violations fail
# the lint. Otherwise (e.g. CREATE SEMANTIC VIEW) we downgrade to a warning.
SQLFLUFF_WELL_SUPPORTED = [
    re.compile(r"CREATE\s+(OR\s+REPLACE\s+)?(SECURE\s+)?VIEW\b", re.I),
    re.compile(r"CREATE\s+(OR\s+REPLACE\s+)?TABLE\b", re.I),
    re.compile(r"^\s*SELECT\b", re.I),
    re.compile(r"^\s*WITH\b", re.I),
]

_failures: list[str] = []
_warnings: list[str] = []


def fail(msg: str) -> None:
    _failures.append(msg)
    print(f"FAIL: {msg}", file=sys.stderr)


def warn(msg: str) -> None:
    _warnings.append(msg)
    print(f"WARN: {msg}")


def check_project_yml(project_dir: Path) -> None:
    yml = project_dir / "project.yml"
    try:
        cfg = yaml.safe_load(yml.read_text()) or {}
    except yaml.YAMLError as e:
        fail(f"{yml.relative_to(REPO_ROOT)}: invalid YAML: {e}")
        return
    if not isinstance(cfg, dict):
        fail(f"{yml.relative_to(REPO_ROOT)}: top-level must be a mapping")
        return

    if not cfg.get("name"):
        fail(f"{yml.relative_to(REPO_ROOT)}: missing `name`")

    snow = cfg.get("snowflake") or {}
    for env in REQUIRED_ENVS:
        block = snow.get(env)
        if not block:
            fail(f"{yml.relative_to(REPO_ROOT)}: snowflake.{env} block missing")
            continue
        for k in REQUIRED_SNOW_KEYS:
            if not block.get(k):
                fail(f"{yml.relative_to(REPO_ROOT)}: snowflake.{env}.{k} missing")


def check_sv_yaml(sv_file: Path) -> None:
    rel = sv_file.relative_to(REPO_ROOT)
    try:
        doc = yaml.safe_load(sv_file.read_text())
    except yaml.YAMLError as e:
        fail(f"{rel}: invalid YAML: {e}")
        return
    if not isinstance(doc, dict):
        fail(f"{rel}: top-level must be a mapping")
        return

    if not doc.get("name"):
        fail(f"{rel}: missing `name`")

    tables = doc.get("tables")
    if not tables or not isinstance(tables, list):
        fail(f"{rel}: `tables` must be a non-empty list")
    else:
        for i, t in enumerate(tables):
            bt = (t or {}).get("base_table") or {}
            for k in ("database", "schema", "table"):
                if not bt.get(k):
                    fail(f"{rel}: tables[{i}].base_table.{k} missing")

    # dimensions/metrics may be at top-level OR nested under each table.
    has_measure = bool(doc.get("dimensions")) or bool(doc.get("metrics")) or bool(doc.get("time_dimensions"))
    if not has_measure and isinstance(tables, list):
        for t in tables:
            if not isinstance(t, dict):
                continue
            if t.get("dimensions") or t.get("metrics") or t.get("time_dimensions"):
                has_measure = True
                break
    if not has_measure:
        fail(f"{rel}: must declare at least one dimension or metric (top-level or per-table)")


def _is_likely_real_syntax_error(sql: str) -> bool:
    stripped = re.sub(r"(--[^\n]*\n)|(/\*.*?\*/)", "", sql, flags=re.DOTALL).strip()
    return any(p.match(stripped) for p in SQLFLUFF_WELL_SUPPORTED)


def sqlfluff_check(sql_file: Path) -> None:
    rel = sql_file.relative_to(REPO_ROOT)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "sqlfluff", "lint",
             "--dialect", "snowflake", "--nocolor", str(sql_file)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    except FileNotFoundError:
        warn(f"sqlfluff not installed — skipping SQL parse check for {rel}")
        return

    has_prs = bool(re.search(r"\bPRS\b", proc.stdout))
    if not has_prs:
        return
    sql = sql_file.read_text()
    if _is_likely_real_syntax_error(sql):
        fail(f"{rel}: sqlfluff parse errors:\n{proc.stdout.strip()}")
    else:
        warn(f"{rel}: sqlfluff parse warnings (unsupported DDL — probably CREATE SEMANTIC VIEW)")


def main() -> int:
    if not PROJECTS_DIR.is_dir():
        print(f"FAIL: projects dir not found: {PROJECTS_DIR}", file=sys.stderr)
        return 1

    projects = [d for d in sorted(PROJECTS_DIR.iterdir())
                if d.is_dir() and (d / "project.yml").is_file()]

    print(f"Linting {len(projects)} project(s) under {PROJECTS_DIR.relative_to(REPO_ROOT)}/\n")

    for pdir in projects:
        print(f"=== {pdir.name} ===")
        check_project_yml(pdir)
        # Loose folders: lint by EXTENSION across every subpath, not by folder. Every
        # non-project YAML is a semantic view (validate its shape — this still catches
        # a malformed/incomplete SV, wherever it lives); every .sql (base view / sql
        # SV / function / procedure) goes through sqlfluff.
        for f in sorted(pdir.rglob("*")):
            if not f.is_file() or f.name == "project.yml" or f.name.startswith("."):
                continue
            suf = f.suffix.lower()
            if suf in (".yml", ".yaml"):
                check_sv_yaml(f)
            elif suf == ".sql":
                sqlfluff_check(f)

    print()
    if _warnings:
        print(f"{len(_warnings)} warning(s).")
    if _failures:
        print(f"\n{len(_failures)} failure(s). Lint FAILED.")
        return 1
    print("Lint PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
