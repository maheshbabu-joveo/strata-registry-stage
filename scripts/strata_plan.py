"""Shared project compiler — classify objects by CONTENT and order them by
DEPENDENCY.

This is the single source of truth for "what is this file?" and "in what order do
its objects deploy?". It is imported by:
  * scripts/render_changes.py  — the deploy renderer (this repo), and
  * strata-engine              — sandbox/test, which imports it FROM the mounted
                                 registry clone (see repo_service._load_plan()).
so deploy, sandbox, and test order objects identically with no duplicated logic.

⚠️ The engine also vendors a fallback mirror of this file at
strata-engine/src/service/strata_plan.py (used when the mounted clone predates this
file). Keep the two in sync — a change here must be mirrored there.

Pure stdlib + PyYAML — deliberately NO SQL-parser dependency. Dependencies are
inferred by object-NAME appearance (a base view / function whose name occurs in
another object's body must be created first), with a coarse tier
(function → procedure → base view → semantic view) as the tiebreak.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

import yaml

# Coarse deploy tiers — lower is created first. Used as the tiebreak between
# objects with no dependency between them (and as the fallback if a name-cycle is
# detected). A real dependency edge always overrides the tier.
TIER = {"function": 0, "procedure": 1, "base_view": 2, "semantic_view": 3}

_TEMPLATE_TAG = re.compile(r"\{\{[^{}]*\}\}")
_SEMVIEW_RE = re.compile(r"create\s+(or\s+replace\s+)?semantic\s+view", re.I)
_FUNC_RE = re.compile(
    r"create\s+(or\s+replace\s+)?(secure\s+)?(temporary\s+)?function", re.I
)
_PROC_RE = re.compile(r"create\s+(or\s+replace\s+)?(secure\s+)?procedure", re.I)
# CREATE [OR REPLACE] [SECURE] [RECURSIVE|TEMPORARY] <KIND> [IF NOT EXISTS]
# <possibly-qualified-name> — grab the (maybe db.schema.name) identifier.
_CREATE_NAME_RE = re.compile(
    r"create\s+(?:or\s+replace\s+)?(?:secure\s+)?(?:recursive\s+|temporary\s+)?"
    r"(?:semantic\s+view|view|function|procedure|table)\s+(?:if\s+not\s+exists\s+)?"
    r"([\w$.\"`]+)",
    re.I,
)
# Base views are replaced in place — COPY GRANTS preserves their grants. Required.
_COPY_GRANTS_RE = re.compile(r"copy\s+grants", re.I)


def _safe_yaml(content: str) -> dict:
    """Parse SV YAML tolerant of unquoted Jinja {{ vars }} (invalid raw YAML)."""
    try:
        return yaml.safe_load(_TEMPLATE_TAG.sub("__var__", content or "")) or {}
    except Exception:  # noqa: BLE001
        return {}


def base_view_name(stem: str) -> str:
    """Object name for a base view = uppercase filename stem, non-alnum -> _.
    Matches strata-engine's base_view_name() and render_changes._base_view_name()."""
    return re.sub(r"[^A-Za-z0-9_]+", "_", stem).strip("_").upper()


def classify(path: str, content: str) -> Optional[str]:
    """The object KIND from a file's path + content, or None to ignore it.
    Folder-independent: a `.yaml` with `tables:` is a semantic view; a `.sql` with
    CREATE SEMANTIC VIEW / FUNCTION / PROCEDURE is that; any other `.sql` is a base
    view. project.yml, dotfiles, READMEs, etc. are ignored."""
    fn = str(path).rsplit("/", 1)[-1].lower()
    if fn.startswith(".") or fn == "project.yml" or not fn:
        return None
    if fn.endswith((".yml", ".yaml")):
        doc = _safe_yaml(content)
        return "semantic_view" if isinstance(doc, dict) and "tables" in doc else None
    if fn.endswith(".sql"):
        body = content or ""
        if _SEMVIEW_RE.search(body):
            return "semantic_view"
        if _FUNC_RE.search(body):
            return "function"
        if _PROC_RE.search(body):
            return "procedure"
        return "base_view"
    return None


def name_from_create(content: str) -> Optional[str]:
    """The object name declared in a CREATE statement — the last segment of a
    (possibly db.schema.name) identifier, uppercased. Template tags are neutralized
    first so `CREATE VIEW {{ database }}.{{ schema }}.FOO` yields FOO."""
    m = _CREATE_NAME_RE.search(_TEMPLATE_TAG.sub("x", content or ""))
    if not m:
        return None
    return m.group(1).rsplit(".", 1)[-1].strip('"`').upper() or None


def object_name(kind: str, path: str, content: str) -> Optional[str]:
    """The Snowflake object NAME for a classified file (uppercased).

    CONTENT declares the name — a base view / function / procedure from its CREATE
    statement, a semantic view from its `name:` (YAML) or CREATE (DDL). This lets
    file NAMES be free-form (even duplicated across folders) while OBJECT names stay
    authoritative. A bare-SELECT base view (no CREATE) falls back to the filename."""
    stem = str(path).rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if kind == "semantic_view" and str(path).lower().endswith((".yml", ".yaml")):
        nm = (_safe_yaml(content) or {}).get("name")
        return str(nm).upper() if nm else base_view_name(stem)
    return name_from_create(content) or base_view_name(stem)


def base_view_missing_copy_grants(content: str) -> bool:
    """A base view authored as an explicit ``CREATE … VIEW`` must include COPY GRANTS
    — otherwise its grants are dropped every time it's replaced. A bare SELECT is
    auto-wrapped WITH COPY GRANTS, so it's exempt (returns False)."""
    body = content or ""
    if not re.search(
        r"create\s+(?:or\s+replace\s+)?(?:secure\s+|recursive\s+)*view", body, re.I
    ):
        return False
    return not _COPY_GRANTS_RE.search(body)


def build_objects(files: Iterable[Tuple[str, str]]) -> List[Dict]:
    """From (path, content) pairs → [{path, kind, name, content}] for real objects
    (ignored files dropped)."""
    objs: List[Dict] = []
    for path, content in files:
        kind = classify(path, content)
        if not kind:
            continue
        name = object_name(kind, path, content)
        if name:
            objs.append(
                {"path": path, "kind": kind, "name": name, "content": content or ""}
            )
    return objs


def _deps_of(obj: Dict, names: set) -> set:
    """Names of OTHER project objects referenced in obj's body (word-boundary)."""
    body = obj["content"]
    out = set()
    for n in names:
        if n == obj["name"]:
            continue
        if re.search(rf"(?<![\w$]){re.escape(n)}(?![\w$])", body, re.I):
            out.add(n)
    return out


def order(objects: List[Dict]) -> List[Dict]:
    """Topologically sort objects so every dependency is created before its
    dependents. Objects with no edge between them fall back to (tier, name) order
    for determinism. A name-cycle (rare, e.g. a false match) degrades gracefully to
    tier order for the remainder rather than looping."""
    names = {o["name"] for o in objects}
    for o in objects:
        o["deps"] = _deps_of(o, names)
    ordered: List[Dict] = []
    placed: set = set()
    remaining = list(objects)
    while remaining:
        ready = [o for o in remaining if o["deps"] <= placed] or remaining
        ready.sort(key=lambda o: (TIER.get(o["kind"], 99), o["name"]))
        nxt = ready[0]
        ordered.append(nxt)
        placed.add(nxt["name"])
        remaining.remove(nxt)
    return ordered


def compile_plan(files: Iterable[Tuple[str, str]]) -> List[Dict]:
    """(path, content) pairs → dependency-ordered [{path, kind, name, content, deps}]."""
    return order(build_objects(files))
