from __future__ import annotations

import sys
import types

import pytest

import skill_lib.hub as hub_mod


def _stub_hermes_module(browse_impl=None, inspect_impl=None):
    hermes_cli = types.ModuleType("hermes_cli")
    skills_hub = types.ModuleType("hermes_cli.skills_hub")
    skills_hub.browse_skills = browse_impl or (
        lambda page=1, page_size=20, source="all": {
            "items": [],
            "page": page,
            "total_pages": 1,
            "total": 0,
        }
    )
    skills_hub.inspect_skill = inspect_impl or (lambda name: None)
    hermes_cli.skills_hub = skills_hub
    sys.modules["hermes_cli"] = hermes_cli
    sys.modules["hermes_cli.skills_hub"] = skills_hub


def _stub_tools_module(results, timed_out=None, on_call=None):
    """Stub `tools.skills_hub.parallel_search_sources` (+ router/auth) so the
    resilient browse path engages without a real Hermes host."""
    tools = types.ModuleType("tools")
    skills_hub = types.ModuleType("tools.skills_hub")

    class GitHubAuth:
        def __init__(self, *a, **k):
            pass

    def create_source_router(auth=None):
        return ["<opaque-source>"]

    def parallel_search_sources(sources, query="", per_source_limits=None,
                                source_filter="all", overall_timeout=30,
                                on_source_done=None):
        if on_call is not None:
            on_call({
                "query": query,
                "per_source_limits": per_source_limits,
                "source_filter": source_filter,
                "overall_timeout": overall_timeout,
            })
        return (results, {}, timed_out or [])

    skills_hub.GitHubAuth = GitHubAuth
    skills_hub.create_source_router = create_source_router
    skills_hub.parallel_search_sources = parallel_search_sources
    tools.skills_hub = skills_hub
    sys.modules["tools"] = tools
    sys.modules["tools.skills_hub"] = skills_hub


class _Meta:
    """Minimal `SkillMeta` stand-in (attribute access, not dict)."""

    def __init__(self, name, description="", source="clawhub",
                 trust_level="community", tags=None):
        self.name = name
        self.description = description
        self.source = source
        self.trust_level = trust_level
        self.tags = tags or []


@pytest.fixture(autouse=True)
def cleanup_hermes_modules():
    yield
    for k in ("hermes_cli", "hermes_cli.skills_hub", "tools", "tools.skills_hub"):
        sys.modules.pop(k, None)


def test_browse_translates_hermes_names_to_ios_canonical():
    fake = {
        "items": [
            {"name": "writer", "description": "Drafts", "source": "official", "trust": "builtin"},
            {"name": "researcher", "description": "Search", "source": "clawhub", "trust": "community", "tags": ["agent"]},
        ],
        "page": 1,
        "total_pages": 1,
        "total": 2,
    }

    def fake_browse(page, page_size, source):
        assert page == 1 and page_size == 50 and source == "all"
        return fake

    _stub_hermes_module(browse_impl=fake_browse)
    out = hub_mod.browse(plugin_version="0.1.0", page=1, page_size=50, source="all")

    assert out["plugin_version"] == "0.1.0"
    assert out["total"] == 2

    item = out["items"][0]
    assert "trust" not in item
    assert item["trustLevel"] == "builtin"
    assert item["name"] == "writer"
    assert item["tags"] == []

    item2 = out["items"][1]
    assert item2["trustLevel"] == "community"
    assert item2["tags"] == ["agent"]


def test_browse_translation_handles_malformed_items():
    def fake_browse(page, page_size, source):
        return {"items": [None, "not a dict", {"name": "ok", "trust": "builtin"}], "page": 1, "total_pages": 1, "total": 3}

    _stub_hermes_module(browse_impl=fake_browse)
    out = hub_mod.browse(plugin_version="0.1.0")
    assert len(out["items"]) == 3
    assert out["items"][0]["name"] == ""
    assert out["items"][0]["trustLevel"] == "community"
    assert out["items"][2]["name"] == "ok"
    assert out["items"][2]["trustLevel"] == "builtin"


def test_browse_clamps_pagination():
    seen = {}

    def fake_browse(page, page_size, source):
        seen.update(page=page, page_size=page_size, source=source)
        return {"items": [], "page": page, "total_pages": 1, "total": 0}

    _stub_hermes_module(browse_impl=fake_browse)
    hub_mod.browse(plugin_version="0.1.0", page=99999, page_size=99999, source="x" * 100)

    assert seen["page"] == 1000
    assert seen["page_size"] == 100
    assert seen["source"] == "all"


def test_browse_returns_error_envelope_on_hermes_failure():
    def boom(page, page_size, source):
        raise RuntimeError("registry timeout")

    _stub_hermes_module(browse_impl=boom)
    out = hub_mod.browse(plugin_version="0.1.0")
    assert out["items"] == []
    assert out["error"] == "RuntimeError"
    assert "registry timeout" not in str(out)  # exception message must not leak


def test_browse_handles_missing_hermes_module():
    out = hub_mod.browse(plugin_version="0.1.0")
    assert out["error"] == "hermes_unavailable"
    assert out["items"] == []


def test_browse_query_aggregates_pages_and_filters_by_substring():
    """When --query is set, plugin fetches multiple upstream pages and
    post-filters by case-insensitive substring on name/description/tags."""

    pages = {
        1: {
            "items": [
                {"name": "writer", "description": "Drafts", "trust": "builtin", "tags": []},
                {"name": "calendar", "description": "CalDAV", "trust": "community", "tags": []},
            ],
            "page": 1, "total_pages": 2, "total": 4,
        },
        2: {
            "items": [
                {"name": "researcher", "description": "Web search", "trust": "community", "tags": []},
                {"name": "calendar-sync", "description": "Sync calendars", "trust": "community", "tags": ["scheduling"]},
            ],
            "page": 2, "total_pages": 2, "total": 4,
        },
    }
    seen_pages: list[int] = []

    def fake_browse(page, page_size, source):
        seen_pages.append(page)
        return pages.get(page, {"items": [], "page": page, "total_pages": 2, "total": 4})

    _stub_hermes_module(browse_impl=fake_browse)
    out = hub_mod.browse(plugin_version="0.1.0", query="calendar")

    assert seen_pages == [1, 2]  # walked both pages then stopped
    assert out["total"] == 2
    names = {it["name"] for it in out["items"]}
    assert names == {"calendar", "calendar-sync"}


def test_browse_query_case_insensitive_and_matches_tags():
    pages = {
        1: {
            "items": [
                {"name": "writer", "description": "drafts long-form", "trust": "builtin", "tags": []},
                {"name": "skill-x", "description": "anything", "trust": "community", "tags": ["Calendar"]},
            ],
            "page": 1, "total_pages": 1, "total": 2,
        },
    }

    def fake_browse(page, page_size, source):
        return pages[page]

    _stub_hermes_module(browse_impl=fake_browse)
    out = hub_mod.browse(plugin_version="0.1.0", query="CALENDAR")
    # `skill-x` matches via tag, case-insensitive.
    names = {it["name"] for it in out["items"]}
    assert names == {"skill-x"}


def test_browse_query_clamped_in_length():
    """Long query strings are truncated to 128 chars; argparse layer
    is the second belt, this is the first."""
    pages = {1: {"items": [], "page": 1, "total_pages": 1, "total": 0}}

    def fake_browse(page, page_size, source):
        return pages[page]

    _stub_hermes_module(browse_impl=fake_browse)
    # Pass 500-char query; expect it to be processed (no exception)
    # and the search to return zero hits as expected.
    out = hub_mod.browse(plugin_version="0.1.0", query="x" * 500)
    assert out["total"] == 0


def test_browse_query_paginates_filtered_results():
    """Filtered result set respects page / page_size client-side."""
    items = [{"name": f"calendar-{i}", "description": "x", "trust": "community", "tags": []} for i in range(15)]

    def fake_browse(page, page_size, source):
        if page == 1:
            return {"items": items, "page": 1, "total_pages": 1, "total": 15}
        return {"items": [], "page": page, "total_pages": 1, "total": 15}

    _stub_hermes_module(browse_impl=fake_browse)
    page1 = hub_mod.browse(plugin_version="0.1.0", query="calendar", page=1, page_size=10)
    page2 = hub_mod.browse(plugin_version="0.1.0", query="calendar", page=2, page_size=10)

    assert page1["total"] == 15
    assert page1["total_pages"] == 2
    assert len(page1["items"]) == 10
    assert len(page2["items"]) == 5


def test_resilient_path_used_when_parallel_available_and_translates():
    """When `parallel_search_sources` is importable, browse uses it and
    never touches the hang-prone sequential `browse_skills`."""
    def browse_must_not_run(page, page_size, source):
        raise AssertionError("browse_skills must not be called on resilient path")

    _stub_hermes_module(browse_impl=browse_must_not_run)
    _stub_tools_module(results=[
        _Meta("writer", "Drafts", source="official", trust_level="builtin"),
        _Meta("researcher", "Search", source="clawhub", trust_level="community", tags=["agent"]),
    ])

    out = hub_mod.browse(plugin_version="0.1.0", page=1, page_size=50, source="all")
    assert out["total"] == 2
    item = out["items"][0]
    assert "trust" not in item
    # official + builtin sorts first.
    assert item["name"] == "writer"
    assert item["trustLevel"] == "builtin"
    assert out["items"][1]["tags"] == ["agent"]


def test_resilient_returns_partial_results_when_a_source_times_out():
    """A timed-out registry is dropped, not fatal — surviving results
    still populate the marketplace instead of hanging or erroring."""
    _stub_hermes_module()
    _stub_tools_module(
        results=[_Meta("calendar", "CalDAV")],
        timed_out=["github", "lobehub"],
    )
    out = hub_mod.browse(plugin_version="0.1.0")
    assert "error" not in out
    assert out["total"] == 1
    assert out["items"][0]["name"] == "calendar"


def test_resilient_dedupes_by_trust_rank():
    """Same skill name from two sources collapses to the higher trust tier."""
    _stub_hermes_module()
    _stub_tools_module(results=[
        _Meta("notes", "community copy", source="clawhub", trust_level="community"),
        _Meta("notes", "official copy", source="official", trust_level="builtin"),
    ])
    out = hub_mod.browse(plugin_version="0.1.0")
    assert out["total"] == 1
    assert out["items"][0]["trustLevel"] == "builtin"
    assert out["items"][0]["description"] == "official copy"


def test_resilient_filters_by_query():
    _stub_hermes_module()
    _stub_tools_module(results=[
        _Meta("calendar", "x"),
        _Meta("writer", "y"),
        _Meta("skill-x", "z", tags=["Calendar"]),
    ])
    out = hub_mod.browse(plugin_version="0.1.0", query="CALENDAR")
    names = {it["name"] for it in out["items"]}
    assert names == {"calendar", "skill-x"}  # name + tag, case-insensitive


def test_resilient_paginates_in_memory():
    _stub_hermes_module()
    _stub_tools_module(results=[_Meta(f"s-{i:02d}") for i in range(15)])
    page1 = hub_mod.browse(plugin_version="0.1.0", page=1, page_size=10)
    page2 = hub_mod.browse(plugin_version="0.1.0", page=2, page_size=10)
    assert page1["total"] == 15 and page1["total_pages"] == 2
    assert len(page1["items"]) == 10
    assert len(page2["items"]) == 5


def test_resilient_passes_timeout_and_source_filter():
    captured = {}
    _stub_hermes_module()
    _stub_tools_module(results=[], on_call=lambda kw: captured.update(kw))
    hub_mod.browse(plugin_version="0.1.0", source="clawhub")
    assert captured["overall_timeout"] == hub_mod._BROWSE_TIMEOUT_SECONDS
    assert captured["source_filter"] == "clawhub"
    assert captured["query"] == ""  # browse fan-out always empty-query


def test_resilient_surfaces_clean_error_on_unexpected_failure():
    """A genuine fault in the parallel path returns a class-name envelope,
    not a traceback, and does NOT silently fall back to browse_skills."""
    def boom(page, page_size, source):
        raise AssertionError("must not reach browse_skills")

    _stub_hermes_module(browse_impl=boom)

    tools = types.ModuleType("tools")
    skills_hub = types.ModuleType("tools.skills_hub")

    class GitHubAuth:
        def __init__(self, *a, **k):
            pass

    def create_source_router(auth=None):
        raise RuntimeError("router secret /path/leak")

    def parallel_search_sources(*a, **k):
        return ([], {}, [])

    skills_hub.GitHubAuth = GitHubAuth
    skills_hub.create_source_router = create_source_router
    skills_hub.parallel_search_sources = parallel_search_sources
    tools.skills_hub = skills_hub
    sys.modules["tools"] = tools
    sys.modules["tools.skills_hub"] = skills_hub

    out = hub_mod.browse(plugin_version="0.1.0")
    assert out["error"] == "RuntimeError"
    assert "/path/leak" not in str(out)


def test_inspect_translates_hermes_names_to_ios_canonical():
    fake = {
        "name": "writer",
        "description": "Drafts",
        "source": "official",
        "trust": "builtin",
        "identifier": "official/productivity/writer",
        "tags": ["docs"],
        "skill_md_preview": "# Writer\n\nUse this skill...",
    }
    _stub_hermes_module(inspect_impl=lambda name: fake if name == "writer" else None)
    out = hub_mod.inspect(plugin_version="0.1.0", name="writer")
    assert out["plugin_version"] == "0.1.0"

    skill = out["skill"]
    assert "trust" not in skill
    assert "skill_md_preview" not in skill
    assert skill["trustLevel"] == "builtin"
    assert skill["skillMdPreview"] == "# Writer\n\nUse this skill..."
    assert skill["identifier"] == "official/productivity/writer"
    assert skill["tags"] == ["docs"]


def test_inspect_omits_preview_when_hermes_doesnt_supply_one():
    fake = {"name": "writer", "description": "x", "source": "official", "identifier": "x", "trust": "builtin"}
    _stub_hermes_module(inspect_impl=lambda name: fake)
    out = hub_mod.inspect(plugin_version="0.1.0", name="writer")
    assert "skillMdPreview" not in out["skill"]


def test_inspect_handles_unknown_name():
    _stub_hermes_module(inspect_impl=lambda name: None)
    out = hub_mod.inspect(plugin_version="0.1.0", name="ghost")
    assert out["skill"] is None
    assert "error" not in out


def test_inspect_rejects_empty_name():
    _stub_hermes_module(inspect_impl=lambda name: {"name": name})
    out = hub_mod.inspect(plugin_version="0.1.0", name="")
    assert out["skill"] is None
    assert out["error"] == "invalid_name"


def test_inspect_handles_hermes_exception():
    def boom(name):
        raise PermissionError("/some/path/that/should/not/leak")

    _stub_hermes_module(inspect_impl=boom)
    out = hub_mod.inspect(plugin_version="0.1.0", name="writer")
    assert out["error"] == "PermissionError"
    assert "/some/path" not in str(out)
