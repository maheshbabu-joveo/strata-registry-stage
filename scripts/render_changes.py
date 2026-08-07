#!/usr/bin/env python3
"""Render projects/ into schemachange/changes/ for a target env.

Walks every projects/<name>/ directory that has a project.yml. Each project
deploys into its OWN Snowflake schema (``{{ database }}.<PROJECT>``) so objects
are segregated per project instead of sharing one MODELLED schema:

  * R__00_<project>_create_schema.sql  -> CREATE SCHEMA IF NOT EXISTS {{ database }}.<PROJECT>
  * base/*.sql              -> R__10_<project>_<name>_base.sql       (CREATE OR REPLACE VIEW)
  * semantic_views/*.yaml   -> R__20_<project>_<name>_semantic_view.sql
                               (wrapped in CALL SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML)
  * semantic_views/*.sql    -> R__20_<project>_<name>_semantic_view.sql (copied)

The per-project schema is substituted for ``{{ schema }}`` HERE, at render time
(render knows the project). ``{{ database }}`` and ``{{ source_db }}`` are LEFT
IN PLACE — schemachange renders them at deploy time from its config vars. The
change-history table stays in MODELLED (schemachange config).

Schema name is derived from the project directory name (uppercased, non-alnum
-> _). Because each project gets its own schema, two projects can safely share
a base-view/SV name — no cross-project collision.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Iterable

import yaml

import strata_plan  # shared classify + dependency-order (same module strata-engine uses)

REPO_ROOT = Path(__file__).resolve().parents[1]
PROJECTS_DIR = REPO_ROOT / "projects"
OUT_DIR = REPO_ROOT / "schemachange" / "changes"

VALID_ENVS = ("preview", "preprod", "prod")

# Matches the ``{{ schema }}`` Jinja placeholder (any inner spacing).
_SCHEMA_PLACEHOLDER = re.compile(r"\{\{\s*schema\s*\}\}")


def _die(msg: str) -> None:
    print(f"render_changes: ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _iter_projects() -> Iterable[Path]:
    if not PROJECTS_DIR.is_dir():
        _die(f"projects dir not found: {PROJECTS_DIR}")
    for child in sorted(PROJECTS_DIR.iterdir()):
        if child.is_dir() and (child / "project.yml").is_file():
            yield child


def _load_project_yml(project_dir: Path) -> dict:
    try:
        with (project_dir / "project.yml").open() as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        _die(f"{project_dir.name}: project.yml is not valid YAML: {e}")
    if not isinstance(data, dict):
        _die(f"{project_dir.name}: project.yml must be a mapping")
    return data


def _validate_env_config(project: str, cfg: dict, env: str) -> dict:
    snow = (cfg.get("snowflake") or {}).get(env)
    if not snow:
        _die(f"{project}: project.yml has no snowflake.{env} block")
    for k in ("database", "schema", "warehouse"):
        if not snow.get(k):
            _die(f"{project}: project.yml snowflake.{env}.{k} is missing/empty")
    return snow


def _schema_name(project: str) -> str:
    """Per-project schema: uppercase the project dir name, non-alnum -> _."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", project).strip("_").upper()


def _sub_schema(text: str, schema: str) -> str:
    """Replace the ``{{ schema }}`` placeholder with the concrete per-project
    schema. Leaves ``{{ database }}`` / ``{{ source_db }}`` for schemachange."""
    return _SCHEMA_PLACEHOLDER.sub(schema, text)


def _base_view_name(stem: str) -> str:
    """Match backend's base_view_name(): uppercase, non-alnum → _."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", stem).strip("_").upper()


def _wrap_base_sql(body: str, view_name: str, schema: str) -> str:
    """Wrap a raw SELECT as CREATE OR REPLACE VIEW in the project schema; pass
    through an authored CREATE statement untouched (schema placeholder subbed)."""
    body = _sub_schema(body.strip().rstrip(";"), schema)
    if re.match(r"(?is)^\s*create\s+", body):
        # Full CREATE … VIEW authored explicitly: rewrite its name to THIS project's
        # FQN (regardless of how it was written/qualified) so it always deploys to
        # the right schema, preserving the rest (COPY GRANTS, AS …). Matches the
        # sandbox's emit_base_view so deploy and preview agree.
        fqn = f"{{{{ database }}}}.{schema}.{view_name}"
        rewritten, n = re.subn(
            r"(?is)^\s*create\s+(?:or\s+replace\s+)?(?:secure\s+)?(?:recursive\s+)?"
            r"view\s+(?:if\s+not\s+exists\s+)?.+?"
            r"(?=\s+copy\s+grants|\s+as\b|\s*\(|\s+comment\b)",
            f"CREATE OR REPLACE VIEW {fqn}",
            body,
            count=1,
        )
        return (rewritten if n else body) + ";\n"
    return (
        f"CREATE OR REPLACE VIEW {{{{ database }}}}.{schema}.{view_name} "
        f"COPY GRANTS AS\n{body};\n"
    )


def _wrap_yaml_as_sql(yaml_body: str, project: str, sv_name: str, schema: str) -> str:
    """CALL SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML with DB.<project-schema> as the
    target. The SV YAML's own ``{{ schema }}`` (base_table.schema) is subbed too
    so it references the base view in the same project schema."""
    yaml_body = _sub_schema(yaml_body, schema)
    if "$$" in yaml_body:
        _die(
            f"{project}/{sv_name}: YAML body contains '$$' which conflicts with "
            "dollar-quoting. Rewrite the value or escape it."
        )
    header = (
        f"-- Generated by scripts/render_changes.py from "
        f"projects/{project}/semantic_views/{sv_name}.yaml\n"
        f"-- Deploys via SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML into schema {schema}.\n"
    )
    return (
        header
        + f"CALL SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML('{{{{ database }}}}.{schema}',\n"
        + "$$\n" + yaml_body + "\n$$);\n"
    )


def _wrap_create_schema(schema: str) -> str:
    return (
        f"-- Per-project schema (segregates this project's objects).\n"
        f"CREATE SCHEMA IF NOT EXISTS {{{{ database }}}}.{schema};\n"
    )


def _out_name(prefix: str, project: str, stem: str, suffix: str) -> str:
    # Prefix numeric ordering: 00 schema, 10 base views, 20 semantic views.
    return f"R__{prefix}_{project}_{stem}_{suffix}.sql"


def _check_yaml_shape(project: str, sv_file: Path, body: str) -> None:
    try:
        doc = yaml.safe_load(body)
    except yaml.YAMLError as e:
        _die(f"{project}/{sv_file.name}: not valid YAML: {e}")
    if not isinstance(doc, dict):
        _die(f"{project}/{sv_file.name}: top-level must be a mapping")
    if not doc.get("name"):
        _die(f"{project}/{sv_file.name}: missing `name`")
    tables = doc.get("tables")
    if not tables or not isinstance(tables, list):
        _die(f"{project}/{sv_file.name}: `tables` must be a non-empty list")
    for i, t in enumerate(tables):
        bt = (t or {}).get("base_table") or {}
        for k in ("database", "schema", "table"):
            if not bt.get(k):
                _die(f"{project}/{sv_file.name}: tables[{i}].base_table.{k} is missing")


def render(env: str, only_project: str | None = None) -> None:
    if env not in VALID_ENVS:
        _die(f"env must be one of {VALID_ENVS}, got {env!r}")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if only_project:
        # Project-level deploy: refresh only THIS project's rendered files and leave
        # every other project's outputs in place, so changes/ stays cumulative (a
        # per-project deploy doesn't wipe/re-churn the rest). Filenames are
        # R__<prefix>_<project>_<...>.sql — match on the <project>_ segment.
        for f in OUT_DIR.glob("R__*.sql"):
            after_prefix = f.name[len("R__") :].split("_", 1)[-1]
            if after_prefix == only_project or after_prefix.startswith(f"{only_project}_"):
                f.unlink()
    else:
        if OUT_DIR.exists():
            shutil.rmtree(OUT_DIR)
        OUT_DIR.mkdir(parents=True)

    emitted: dict[str, Path] = {}  # output filename -> source, for duplicate detection
    n_schema = n_base = n_sv_yaml = n_sv_sql = n_other = 0

    for project_dir in _iter_projects():
        if only_project and project_dir.name != only_project:
            continue
        project = project_dir.name
        cfg = _load_project_yml(project_dir)
        _validate_env_config(project, cfg, env)
        schema = _schema_name(project)

        # Loose folders: gather EVERY file under the project (any subpath), read it,
        # and let the shared compiler classify + dependency-order the objects. The
        # R__ numeric prefix reflects that topo order, so schemachange (which applies
        # by filename sort) creates each dependency before its dependents.
        files: list[tuple[str, str]] = []
        for f in sorted(project_dir.rglob("*")):
            if not f.is_file() or f.name == "project.yml" or f.name.startswith("."):
                continue
            try:
                files.append((str(f.relative_to(project_dir)), f.read_text()))
            except (UnicodeDecodeError, OSError):
                continue
        plan = strata_plan.compile_plan(files)
        if not plan:
            continue

        # 0. Per-project schema (before any object). Idempotent.
        schema_out = _out_name("0000", project, "create", "schema")
        (OUT_DIR / schema_out).write_text(_wrap_create_schema(schema))
        emitted[schema_out] = project_dir / "project.yml"
        n_schema += 1

        # 1..N objects in dependency order (0010, 0011, …).
        for idx, obj in enumerate(plan):
            body, kind = obj["content"], obj["kind"]
            src = project_dir / obj["path"]
            slug = obj["name"].lower()
            prefix = f"{10 + idx:04d}"
            if kind == "semantic_view":
                out_name = _out_name(prefix, project, slug, "semantic_view")
                if obj["path"].lower().endswith((".yml", ".yaml")):
                    _check_yaml_shape(project, src, body)
                    (OUT_DIR / out_name).write_text(
                        _wrap_yaml_as_sql(body, project, slug, schema)
                    )
                    n_sv_yaml += 1
                else:
                    (OUT_DIR / out_name).write_text(_sub_schema(body, schema))
                    n_sv_sql += 1
            elif kind == "base_view":
                out_name = _out_name(prefix, project, slug, "base_view")
                (OUT_DIR / out_name).write_text(_wrap_base_sql(body, obj["name"], schema))
                n_base += 1
            else:  # function / procedure — pass the DDL through (schema-subbed)
                out_name = _out_name(prefix, project, slug, kind)
                (OUT_DIR / out_name).write_text(_sub_schema(body, schema))
                n_other += 1
            if out_name in emitted and emitted[out_name] != src:
                _die(f"duplicate output {out_name}: {emitted[out_name]} and {src}")
            emitted[out_name] = src

    print(
        f"render_changes: env={env} → wrote {n_schema} schema(s), {n_base} base view(s), "
        f"{n_sv_yaml} yaml + {n_sv_sql} sql semantic view(s), {n_other} function/procedure(s) "
        f"into {OUT_DIR.relative_to(REPO_ROOT)}/"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Render projects/ into schemachange/changes/")
    ap.add_argument("--env", required=True, choices=VALID_ENVS)
    ap.add_argument(
        "--project",
        default=None,
        help="render only this project (default: all projects)",
    )
    args = ap.parse_args()
    render(args.env, args.project)


if __name__ == "__main__":
    main()
