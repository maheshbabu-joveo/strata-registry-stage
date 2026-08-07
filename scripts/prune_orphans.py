#!/usr/bin/env python3
"""Drop objects in a project's schema that the repo no longer declares.

schemachange applies (CREATE OR REPLACE) the declared objects but can NEVER drop a
removed one — a deleted source file just vanishes from ``changes/`` and the object
lingers in Snowflake. This reconciles ONE project's schema against its declared
objects and drops the orphans.

Dry-run by default (prints what it WOULD drop). Wired into deploy_and_verify.sh
AFTER schemachange, runs whenever the deploy is project-scoped (STRATA_DEPLOY_PROJECT
set), and it only ``--apply``s drops when STRATA_PRUNE_APPLY=true.

Safety:
  * scoped to the single project schema (never another schema/database);
  * only VIEW + SEMANTIC VIEW (never tables, functions, etc.);
  * fail-safe — if the live SHOW errors, it skips the prune (never reads "couldn't
    list" as "drop everything");
  * semantic views dropped before base views (reverse dependency).
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import yaml

import strata_plan  # shared classify + dependency-order

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECTS_DIR = REPO_ROOT / "projects"


def _die(msg: str) -> None:
    print(f"prune_orphans: ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _schema_name(project: str) -> str:
    """Per-project schema — matches render_changes._schema_name()."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", project).strip("_").upper()


def _declared_and_bodies(project_dir: Path):
    """(declared_names, bodies) for this project via the shared compiler (so it
    agrees exactly with what the renderer deploys). ``bodies`` are the declared
    objects' raw texts — scanned so we never drop an object still NAMED by a live
    object (e.g. a base view whose file was removed but which a live SV still reads:
    keep it, don't break the SV). Note a still-referenced name may not itself be a
    declared object, so we scan the text, not just the in-plan dependency edges."""
    files = []
    for f in sorted(project_dir.rglob("*")):
        if f.is_file() and f.name != "project.yml" and not f.name.startswith("."):
            try:
                files.append((str(f.relative_to(project_dir)), f.read_text()))
            except (UnicodeDecodeError, OSError):
                continue
    plan = strata_plan.compile_plan(files)
    declared = {o["name"].upper() for o in plan}
    bodies = [o["content"] for o in plan]
    return declared, bodies


def _is_referenced(name: str, bodies) -> bool:
    """Does ``name`` appear (whole-word) in any declared object's body?"""
    return any(
        re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", b or "", re.I)
        for b in bodies
    )


def _connect(cfg: dict):
    """Key-pair connection reusing the deploy creds (account + key from env, the
    rest from the schemachange config)."""
    import snowflake.connector
    from cryptography.hazmat.primitives import serialization

    account = os.environ.get("SNOWFLAKE_ACCOUNT") or _die("SNOWFLAKE_ACCOUNT not set")
    key_path = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PATH") or _die(
        "SNOWFLAKE_PRIVATE_KEY_PATH not set"
    )
    passphrase = os.environ.get("SNOWFLAKE_PRIVATE_KEY_PASSPHRASE")
    with open(key_path, "rb") as fh:
        pkey = serialization.load_pem_private_key(
            fh.read(), password=passphrase.encode() if passphrase else None
        )
    pkb = pkey.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return snowflake.connector.connect(
        account=account,
        user=cfg["snowflake-user"],
        private_key=pkb,
        role=cfg.get("snowflake-role"),
        warehouse=cfg.get("snowflake-warehouse"),
        database=cfg["snowflake-database"],
    )


def _live_objects(cur, database: str, schema: str):
    """{NAME: 'SEMANTIC VIEW'|'VIEW'} in the schema, or None on any SHOW error
    (fail-safe: the caller then skips the prune rather than dropping blind)."""
    live: dict = {}
    for kind, label in (("SEMANTIC VIEWS", "SEMANTIC VIEW"), ("VIEWS", "VIEW")):
        try:
            cur.execute(f'SHOW {kind} IN SCHEMA "{database}"."{schema}"')
        except Exception as exc:  # noqa: BLE001
            print(
                f"prune_orphans: SHOW {kind} failed ({exc}); skipping prune (fail-safe)",
                file=sys.stderr,
            )
            return None
        cols = [d[0].lower() for d in cur.description]
        for row in cur.fetchall():
            rec = dict(zip(cols, row))
            nm = str(rec.get("name", "")).upper()
            if nm:
                live.setdefault(nm, label)
    return live


def _prune_one(cur, database: str, project: str, apply: bool) -> int:
    """Reconcile ONE project's schema: drop objects it no longer declares (and that
    no live object still references). Returns the number dropped (0 in dry-run or on
    a fail-safe skip)."""
    project_dir = PROJECTS_DIR / project
    schema = _schema_name(project)
    declared, bodies = _declared_and_bodies(project_dir)
    live = _live_objects(cur, database, schema)
    if live is None:
        return 0  # fail-safe: couldn't list — do nothing for this project
    orphans = [
        (n, t)
        for n, t in live.items()
        if n not in declared and not _is_referenced(n, bodies)
    ]
    # Semantic views first (they depend on base views), then base views.
    orphans.sort(key=lambda x: 0 if x[1] == "SEMANTIC VIEW" else 1)
    if not orphans:
        print(
            f"prune_orphans: {project} @ {database}.{schema}: nothing to drop "
            f"({len(declared)} declared, {len(live)} live)"
        )
        return 0
    print(f"prune_orphans: {project} @ {database}.{schema}: {len(orphans)} orphan(s):")
    for n, t in orphans:
        print(f"    DROP {t} {n}")
    if not apply:
        print(f"prune_orphans: {project}: DRY-RUN — pass --apply to drop (nothing changed).")
        return 0
    for n, t in orphans:
        stmt = f'DROP {t} IF EXISTS "{database}"."{schema}"."{n}"'
        print(f"    {stmt}")
        cur.execute(stmt)
    print(f"prune_orphans: {project}: dropped {len(orphans)} orphan(s).")
    return len(orphans)


def main() -> int:
    ap = argparse.ArgumentParser(description="Drop repo-removed objects from a project's schema")
    ap.add_argument("--env", required=True, choices=("preview", "preprod", "prod"))
    ap.add_argument("--project", required=True, help="the project to reconcile (ALWAYS project-scoped)")
    ap.add_argument("--apply", action="store_true", help="execute drops (default: dry-run)")
    args = ap.parse_args()

    if not (PROJECTS_DIR / args.project / "project.yml").is_file():
        _die(f"no such project: {args.project!r}")
    conf = REPO_ROOT / "schemachange" / f"schemachange-config.{args.env}.yml"
    if not conf.is_file():
        _die(f"config not found: {conf}")
    cfg = yaml.safe_load(conf.read_text()) or {}
    database = cfg["snowflake-database"]

    conn = _connect(cfg)
    cur = conn.cursor()
    try:
        dropped = _prune_one(cur, database, args.project, args.apply)
        if args.apply and dropped:
            conn.commit()
    finally:
        cur.close()
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
