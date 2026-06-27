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
    uses internally — we call it directly to resolve a browse row entirely
    within its own source (see `_source_scoped_detail`). Returns
    ``(None, None)`` off-host so callers degrade to bare-name inspect.
    """
    try:
        from tools.skills_hub import GitHubAuth, create_source_router
        return create_source_router, GitHubAuth
    except ImportError:
        return None, None


def _import_parallel():
    """Hermes' parallel source fan-out with a per-call overall timeout.

    Returns ``(parallel_search_sources, create_source_router, GitHubAuth)``
    or ``(None, None, None)`` when unavailable (off-host, or a Hermes
    build old enough to predate `parallel_search_sources`) so callers fall
    back to the sequential `browse_skills` path.
    """
    try:
        from tools.skills_hub import (
            GitHubAuth,
            create_source_router,
            parallel_search_sources,
        )
        return parallel_search_sources, create_source_router, GitHubAuth
    except ImportError:
        return None, None, None


# A browse row's `source` label equals the adapter's `source_id()` for every
# source EXCEPT skills.sh, whose adapter id is "skills-sh" while the row label
# carries the dotted "skills.sh". Keep this in sync if Hermes adds a source
# whose display label diverges from its id.
_SOURCE_LABEL_TO_ID = {"skills.sh": "skills-sh"}


def _skill_md_preview(bundle: Any) -> Optional[str]:
    """First ~50 lines of a bundle's SKILL.md, matching `inspect_skill`'s
    preview shape. Best-effort: any failure yields no preview, never raises."""
    try:
        files = getattr(bundle, "files", None) or {}
        content = files.get("SKILL.md")
        if content is None:
            return None
        if isinstance(content, bytes):
            content = content.decode("utf-8", errors="replace")
        lines = str(content).split("\n")
        preview = "\n".join(lines[:50])
        if len(lines) > 50:
            preview += f"\n\n... ({len(lines) - 50} more lines)"
        return preview
    except Exception:
        return None


def _detail_from_result(src: Any, r: Any) -> Optional[dict[str, Any]]:
    """Build a translate-ready inspect dict from a source search hit `r`,
    fetching SKILL.md from the SAME adapter for the preview.

    Crucially this does NOT route back through `inspect_skill`: many sources
    (clawhub, skills.sh, lobehub) use bare-slug identifiers with no ``/``, so
    `inspect_skill` would re-run `_resolve_short_name` on the slug — which
    matches on the *display name*, not the slug, and fails (e.g. slug
    ``airbnb-gateway`` vs name ``Airbnb Gateway``). Resolving on the owning
    adapter directly skips that lossy round-trip entirely.
    """
    ident = getattr(r, "identifier", "") or ""
    if not ident:
        return None
    out: dict[str, Any] = {
        "name": str(getattr(r, "name", "") or ""),
        "description": str(getattr(r, "description", "") or ""),
        "source": str(getattr(r, "source", "") or ""),
        "trust": str(getattr(r, "trust_level", "community") or "community"),
        "identifier": ident,
        "tags": [str(t) for t in (getattr(r, "tags", None) or []) if isinstance(t, (str, int))],
    }
    try:
        preview = _skill_md_preview(src.fetch(ident))
    except Exception:
        preview = None
    if preview:
        out["skill_md_preview"] = preview
    return out


def _exact_detail_from(src: Any, name: str, target_name: str) -> Optional[dict[str, Any]]:
    """Search one adapter for `name`; build the full detail off the exact
    (case-insensitive) name match. Swallows per-source failures."""
    try:
        results = src.search(name, limit=50)
    except Exception:
        return None
    for r in results or []:
        if str(getattr(r, "name", "")).lower() == target_name:
            detail = _detail_from_result(src, r)
            if detail:
                return detail
    return None


def _source_scoped_detail(name: str, source: str) -> Optional[dict[str, Any]]:
    """Resolve a browse row to its full inspect detail within the source the
    row came from — search + fetch on the owning adapter, no `inspect_skill`.

    Why this exists: `inspect_skill(name)` re-resolves a bare name through
    `unified_search`, which (a) skips the external sources when the
    centralized index is present, (b) caps the merged result at 20, and
    (c) dedupes by name across sources — so a skill plainly visible in
    `browse_skills` can come back `null`. And feeding it a resolved *slug*
    doesn't help: a slug without ``/`` re-enters `_resolve_short_name`, which
    matches on display name and misses (``airbnb-gateway`` vs ``Airbnb
    Gateway``). Going straight to the owning adapter sidesteps both.

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
        detail = _exact_detail_from(src, name, target_name)
        if detail:
            return detail

    # Pass 2 — owning adapter missed (renamed/unknown label, or the skill now
    # surfaces under a different source). Scan the rest, skipping the index
    # (the path we're routing around). Rare, bounded, never worse than the
    # bare-name fallback below it.
    for src in sources:
        try:
            sid = src.source_id()
        except Exception:
            continue
        if sid in (want_id, "hermes-index"):
            continue
        detail = _exact_detail_from(src, name, target_name)
        if detail:
            return detail
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
# Only the legacy fallback path (no `parallel_search_sources`) uses this.
_MAX_AGGREGATE_PAGES = 10

# Trust ranking + per-source caps, mirroring Hermes' own `browse_skills`
# so the resilient path produces an identical catalog ordering.
_TRUST_RANK = {"builtin": 3, "trusted": 2, "community": 1}
_PER_SOURCE_LIMIT = {
    "official": 100, "skills-sh": 100, "well-known": 25, "github": 100,
    "clawhub": 50, "claude-marketplace": 50, "lobehub": 50,
}

# Wall-clock bound on the whole multi-registry fan-out. A slow or dead
# registry is dropped (returned in `timed_out`) rather than blocking the
# others — partial results beat an infinite spinner. Sources that miss
# this window surface on the next page load once their own cache warms.
_BROWSE_TIMEOUT_SECONDS = 20


def _translate_meta(r: Any) -> dict[str, Any]:
    """`SkillMeta` (from `parallel_search_sources`) → iOS-canonical dict.

    Same output shape as `_translate_browse_item`, but reads object
    attributes instead of dict keys.
    """
    tags_raw = getattr(r, "tags", None) or []
    return {
        "name": str(getattr(r, "name", "") or ""),
        "description": str(getattr(r, "description", "") or ""),
        "source": str(getattr(r, "source", "") or ""),
        "trustLevel": str(getattr(r, "trust_level", "community") or "community"),
        "tags": [str(t) for t in tags_raw if isinstance(t, (str, int))],
    }


def _resilient_catalog(source: str) -> Optional[list[dict[str, Any]]]:
    """Full deduped catalog via Hermes' parallel, timeout-bounded fan-out.

    Returns the translated catalog (every page, alphabetical within trust
    tiers) so the caller can slice/filter in memory. Returns ``None`` when
    `parallel_search_sources` isn't importable — the caller then falls
    back to the sequential `browse_skills` path. Slow registries are
    skipped, never awaited to completion, so this cannot hang the way a
    bare `browse_skills` call can.
    """
    parallel_search_sources, create_source_router, GitHubAuth = _import_parallel()
    if parallel_search_sources is None or create_source_router is None or GitHubAuth is None:
        return None

    sources = create_source_router(GitHubAuth())
    all_results, _counts, _timed_out = parallel_search_sources(
        sources,
        query="",
        per_source_limits=_PER_SOURCE_LIMIT,
        source_filter=source,
        overall_timeout=_BROWSE_TIMEOUT_SECONDS,
    )

    # Dedup by name; higher trust tier wins ties (mirrors browse_skills).
    seen: dict[str, Any] = {}
    for r in all_results:
        name = getattr(r, "name", "") or ""
        if not name:
            continue
        rank = _TRUST_RANK.get(getattr(r, "trust_level", ""), 0)
        prev = seen.get(name)
        if prev is None or rank > _TRUST_RANK.get(getattr(prev, "trust_level", ""), 0):
            seen[name] = r

    deduped = list(seen.values())
    deduped.sort(key=lambda r: (
        -_TRUST_RANK.get(getattr(r, "trust_level", ""), 0),
        (getattr(r, "source", "") or "") != "official",
        (getattr(r, "name", "") or "").lower(),
    ))
    return [_translate_meta(r) for r in deduped]


def browse(
    plugin_version: str,
    page: int = 1,
    page_size: int = 100,
    source: str = "all",
    query: str = "",
) -> dict[str, Any]:
    page = max(1, min(int(page), 1000))
    page_size = max(1, min(int(page_size), 100))
    if not isinstance(source, str) or len(source) > 32:
        source = "all"
    if not isinstance(query, str):
        query = ""
    query = query.strip()[:128]  # length clamp; argparse already capped via shellQuote

    # Preferred path: Hermes' parallel, timeout-bounded fan-out. Fetches
    # the whole catalog once (partial on slow registries), then slices /
    # filters in memory. This is what stops the marketplace hanging when
    # one federated registry is unreachable. Falls through to the legacy
    # `browse_skills` path below when the helper isn't available.
    try:
        catalog = _resilient_catalog(source)
    except Exception as e:
        return {
            "plugin_version": plugin_version,
            "items": [],
            "page": page,
            "total_pages": 1,
            "total": 0,
            "error": type(e).__name__,
        }
    if catalog is not None:
        if query:
            q_lower = query.lower()
            catalog = [it for it in catalog if _matches_query(it, q_lower)]
        total = len(catalog)
        total_pages = max(1, (total + page_size - 1) // page_size)
        if page > total_pages:
            page = total_pages
        start = (page - 1) * page_size
        return {
            "plugin_version": plugin_version,
            "items": catalog[start : start + page_size],
            "page": page,
            "total_pages": total_pages,
            "total": total,
        }

    # ---- Legacy fallback (Hermes without `parallel_search_sources`) ----
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

    # Primary: when the browse row's source is known, resolve the skill
    # entirely within that source (search + fetch on the owning adapter). This
    # handles bare-slug identifiers (clawhub/skills.sh/lobehub) that
    # `inspect_skill` can't round-trip, and sidesteps `unified_search`'s
    # index-skip / 20-cap / cross-source name dedupe. `source` defaults to
    # "all"/empty for older callers → returns None → bare-name fallback below,
    # identical to the pre-0.1.4 behavior.
    try:
        detail = _source_scoped_detail(name, source)
    except Exception:
        detail = None
    if detail is not None:
        return {"plugin_version": plugin_version, "skill": _translate_inspect_skill(detail)}

    # Fallback: bare-name resolution via Hermes' own helper. Handles
    # source="all"/empty, slash-identifier inputs, and anything the
    # source-scoped pass didn't surface — never worse than pre-0.1.4.
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
