"""Plugin/app boundary. Hermes' raw field names live here only — iOS sees canonical names.

Translations: `trust` → `trustLevel`, `skill_md_preview` → `skillMdPreview`.
A Hermes upstream rename is a one-line patch in the `_translate_*` helpers.
"""

from __future__ import annotations

from typing import Any, Optional


def _import_hub():
    try:
        from hermes_cli.skills_hub import browse_skills, inspect_skill
        return browse_skills, inspect_skill
    except ImportError:
        return None, None


def _import_sources():
    """Hermes' source-router factory. Same module family `inspect_skill`
    uses internally — we call it directly only to resolve a browse row's
    exact identifier inside its own source (see `_resolve_identifier_in_source`).
    Returns ``(None, None)`` off-host so callers degrade to bare-name inspect.
    """
    try:
        from tools.skills_hub import GitHubAuth, create_source_router
        return create_source_router, GitHubAuth
    except ImportError:
        return None, None


# A browse row's `source` label equals the adapter's `source_id()` for every
# source EXCEPT skills.sh, whose adapter id is "skills-sh" while the row label
# carries the dotted "skills.sh". Keep this in sync if Hermes adds a source
# whose display label diverges from its id.
_SOURCE_LABEL_TO_ID = {"skills.sh": "skills-sh"}


def _exact_identifier_from(src: Any, name: str, target_name: str) -> Optional[str]:
    """Search one source adapter for `name` and return the install identifier
    of the exact (case-insensitive) name match, or None. Swallows per-source
    failures so one flaky registry never sinks the whole resolution."""
    try:
        results = src.search(name, limit=50)
    except Exception:
        return None
    for r in results or []:
        if str(getattr(r, "name", "")).lower() == target_name:
            ident = getattr(r, "identifier", "") or ""
            if ident:
                return ident
    return None


def _resolve_identifier_in_source(name: str, source: str) -> Optional[str]:
    """Resolve a browse row's exact install identifier within the source the
    row came from, so `inspect` can hand `inspect_skill` a slash-bearing
    identifier (its direct fetch path) instead of a bare name.

    Why this exists: `inspect_skill(name)` re-resolves a bare name through
    `unified_search`, which (a) skips the external sources when the
    centralized index is present, (b) caps the merged result at 20, and
    (c) dedupes by name across sources. Any one of those makes a skill that
    is plainly visible in `browse_skills` come back `null` on inspect.
    Searching only the row's own source — directly, never via the index —
    and lifting the identifier off the exact-name hit sidesteps all three.

    Returns None (→ caller falls back to bare-name inspect) when there's no
    source hint, the name already looks like an identifier, Hermes is
    off-host, or no exact match surfaces.
    """
    if not isinstance(name, str) or not name or "/" in name:
        return None
    if not isinstance(source, str):
        return None
    src_label = source.strip()
    if not src_label or src_label == "all":
        return None

    create_source_router, GitHubAuth = _import_sources()
    if create_source_router is None or GitHubAuth is None:
        return None
    try:
        sources = create_source_router(GitHubAuth())
    except Exception:
        return None

    target_name = name.lower()
    want_id = _SOURCE_LABEL_TO_ID.get(src_label, src_label)

    # Pass 1 — the adapter that owns this row's source. The common path:
    # one targeted search, no cross-source ambiguity, never touches the index.
    for src in sources:
        try:
            if src.source_id() != want_id:
                continue
        except Exception:
            continue
        ident = _exact_identifier_from(src, name, target_name)
        if ident:
            return ident

    # Pass 2 — owning adapter missed (renamed/unknown label, or the skill now
    # surfaces under a different source). Scan the rest, skipping the index
    # (the path we're routing around), and take the first exact-name
    # identifier. Rare, bounded, and never worse than the bare-name fallback.
    for src in sources:
        try:
            sid = src.source_id()
        except Exception:
            continue
        if sid in (want_id, "hermes-index"):
            continue
        ident = _exact_identifier_from(src, name, target_name)
        if ident:
            return ident
    return None


def _translate_browse_item(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"name": "", "description": "", "source": "", "trustLevel": "community", "tags": []}
    tags_raw = raw.get("tags", [])
    if not isinstance(tags_raw, list):
        tags_raw = []
    return {
        "name": str(raw.get("name", "")),
        "description": str(raw.get("description", "")),
        "source": str(raw.get("source", "")),
        "trustLevel": str(raw.get("trust", "community")),
        "tags": [str(t) for t in tags_raw if isinstance(t, (str, int))],
    }


def _translate_inspect_skill(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw:
        return None
    tags_raw = raw.get("tags", [])
    if not isinstance(tags_raw, list):
        tags_raw = []
    out: dict[str, Any] = {
        "name": str(raw.get("name", "")),
        "description": str(raw.get("description", "")),
        "source": str(raw.get("source", "")),
        "trustLevel": str(raw.get("trust", "community")),
        "identifier": str(raw.get("identifier", "")),
        "tags": [str(t) for t in tags_raw if isinstance(t, (str, int))],
    }
    preview = raw.get("skill_md_preview")
    if isinstance(preview, str) and preview:
        out["skillMdPreview"] = preview
    return out


def _matches_query(item: dict[str, Any], q_lower: str) -> bool:
    """Case-insensitive substring match across name + description + tags.

    The exact same predicate the iOS Local view uses for client-side
    filtering, lifted into the plugin so search behaves identically
    across Local / Marketplace.
    """
    if (item.get("name") or "").lower().find(q_lower) >= 0:
        return True
    if (item.get("description") or "").lower().find(q_lower) >= 0:
        return True
    for tag in item.get("tags") or []:
        if isinstance(tag, str) and tag.lower().find(q_lower) >= 0:
            return True
    return False


# Cap the number of upstream pages we fetch when a query is active.
# Hermes' `browse_skills` is paginated server-side and may hit network
# per registry; pulling 10 pages × 100 = 1000 skills is plenty for any
# realistic query and keeps the rate-limit footprint bounded.
# Upstream's own disk index cache (1h TTL) makes repeats free.
_MAX_AGGREGATE_PAGES = 10


def browse(
    plugin_version: str,
    page: int = 1,
    page_size: int = 100,
    source: str = "all",
    query: str = "",
) -> dict[str, Any]:
    browse_skills, _ = _import_hub()
    if browse_skills is None:
        return {
            "plugin_version": plugin_version,
            "items": [],
            "page": 1,
            "total_pages": 1,
            "total": 0,
            "error": "hermes_unavailable",
        }

    page = max(1, min(int(page), 1000))
    page_size = max(1, min(int(page_size), 100))
    if not isinstance(source, str) or len(source) > 32:
        source = "all"
    if not isinstance(query, str):
        query = ""
    query = query.strip()[:128]  # length clamp; argparse already capped via shellQuote

    # Query-less path: defer to upstream pagination unchanged. This is
    # the hot path (every cold marketplace open) so we keep it cheap.
    if not query:
        try:
            result = browse_skills(page=page, page_size=page_size, source=source)
        except Exception as e:
            return {
                "plugin_version": plugin_version,
                "items": [],
                "page": page,
                "total_pages": 1,
                "total": 0,
                "error": type(e).__name__,
            }

        if not isinstance(result, dict):
            return {
                "plugin_version": plugin_version,
                "items": [],
                "page": page,
                "total_pages": 1,
                "total": 0,
                "error": "unexpected_shape",
            }

        raw_items = result.get("items", [])
        if not isinstance(raw_items, list):
            raw_items = []
        return {
            "plugin_version": plugin_version,
            "items": [_translate_browse_item(it) for it in raw_items],
            "page": result.get("page", page),
            "total_pages": result.get("total_pages", 1),
            "total": result.get("total", 0),
        }

    # Query path: Hermes' upstream `browse_skills` doesn't accept a
    # free-text filter, so we fetch up to `_MAX_AGGREGATE_PAGES` pages,
    # post-filter by substring match, then paginate the filtered set.
    # This is intentionally bounded — a search hit beyond page 10 of
    # the federated catalog won't surface; users narrow further or
    # switch the registry-source filter to find rarer skills.
    q_lower = query.lower()
    aggregated: list[dict[str, Any]] = []
    upstream_total_pages = 1
    for upstream_page in range(1, _MAX_AGGREGATE_PAGES + 1):
        try:
            result = browse_skills(page=upstream_page, page_size=100, source=source)
        except Exception as e:
            return {
                "plugin_version": plugin_version,
                "items": [],
                "page": page,
                "total_pages": 1,
                "total": 0,
                "error": type(e).__name__,
            }
        if not isinstance(result, dict):
            return {
                "plugin_version": plugin_version,
                "items": [],
                "page": page,
                "total_pages": 1,
                "total": 0,
                "error": "unexpected_shape",
            }
        raw_items = result.get("items", [])
        if not isinstance(raw_items, list):
            raw_items = []
        aggregated.extend(_translate_browse_item(it) for it in raw_items)
        upstream_total_pages = result.get("total_pages", upstream_page)
        # Stop early when we've drained the upstream catalog — no point
        # asking for empty pages we already know don't exist.
        if upstream_page >= upstream_total_pages:
            break

    filtered = [it for it in aggregated if _matches_query(it, q_lower)]
    total = len(filtered)
    total_pages = max(1, (total + page_size - 1) // page_size)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * page_size
    window = filtered[start : start + page_size]

    return {
        "plugin_version": plugin_version,
        "items": window,
        "page": page,
        "total_pages": total_pages,
        "total": total,
    }


def inspect(plugin_version: str, name: str, source: str = "all") -> dict[str, Any]:
    _, inspect_skill = _import_hub()
    if inspect_skill is None:
        return {
            "plugin_version": plugin_version,
            "skill": None,
            "error": "hermes_unavailable",
        }

    if not isinstance(name, str) or not name:
        return {
            "plugin_version": plugin_version,
            "skill": None,
            "error": "invalid_name",
        }

    # When the browse row's source is known, resolve the exact identifier in
    # that source and inspect by identifier (direct fetch path). `source`
    # defaults to "all"/empty for older callers → resolves to None → bare-name
    # inspect, identical to the pre-0.1.4 behavior.
    lookup = name
    try:
        ident = _resolve_identifier_in_source(name, source)
    except Exception:
        ident = None
    if ident:
        lookup = ident

    try:
        result = inspect_skill(lookup)
    except Exception as e:
        return {
            "plugin_version": plugin_version,
            "skill": None,
            "error": type(e).__name__,
        }

    # Source-scoped identifier didn't fetch a skill → retry the original bare
    # name so we never regress relative to the pre-0.1.4 path.
    if result is None and lookup != name:
        try:
            result = inspect_skill(name)
        except Exception as e:
            return {
                "plugin_version": plugin_version,
                "skill": None,
                "error": type(e).__name__,
            }

    if result is None:
        return {"plugin_version": plugin_version, "skill": None}

    if not isinstance(result, dict):
        return {
            "plugin_version": plugin_version,
            "skill": None,
            "error": "unexpected_shape",
        }

    return {"plugin_version": plugin_version, "skill": _translate_inspect_skill(result)}
