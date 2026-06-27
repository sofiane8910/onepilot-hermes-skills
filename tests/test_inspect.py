from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import skills_dump
from skill_lib import hub


def _run(argv, monkeypatch) -> dict:
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", buf)
    skills_dump.main(argv)
    return json.loads(buf.getvalue().strip())


def test_inspect_rejects_invalid_name(monkeypatch):
    out = _run(["--mode", "inspect", "--name", "../etc/passwd"], monkeypatch)
    assert out["error"] == "invalid_name"
    assert out["skill"] is None


def test_inspect_rejects_command_chars(monkeypatch):
    out = _run(["--mode", "inspect", "--name", "writer; rm -rf /"], monkeypatch)
    assert out["error"] == "invalid_name"


def test_inspect_rejects_too_long(monkeypatch):
    out = _run(["--mode", "inspect", "--name", "a" * 201], monkeypatch)
    assert out["error"] == "invalid_name"


def test_inspect_accepts_valid_short_name(monkeypatch):
    out = _run(["--mode", "inspect", "--name", "writer"], monkeypatch)
    assert out["skill"] is None
    assert out["error"] == "hermes_unavailable"


def test_inspect_accepts_valid_slash_path(monkeypatch):
    out = _run(["--mode", "inspect", "--name", "anthropics/skills/skill-creator"], monkeypatch)
    assert out["skill"] is None
    assert out["error"] == "hermes_unavailable"


def test_inspect_accepts_name_with_spaces(monkeypatch):
    """Real Hermes skill names like 'MD5 Tool' / 'GitHub PR Reviewer'
    contain spaces. Earlier regex `[A-Za-z0-9_./\\-]` rejected them
    and the iOS detail sheet showed `invalid_name`. Plugin >=0.1.3
    accepts them."""
    out = _run(["--mode", "inspect", "--name", "MD5 Tool"], monkeypatch)
    # Name passes validation; plugin then routes to Hermes (unavailable
    # in tests) and surfaces that as the only error.
    assert out["error"] == "hermes_unavailable"


def test_inspect_accepts_common_punctuation(monkeypatch):
    """Real-world skill names include parens, plus, ampersand, comma."""
    for name in ["Skill (beta)", "C++ Linter", "A & B", "X, Y, Z", "It's Fine"]:
        out = _run(["--mode", "inspect", "--name", name], monkeypatch)
        assert out["error"] == "hermes_unavailable", f"rejected: {name!r}"


def test_inspect_still_rejects_shell_metacharacters(monkeypatch):
    """The relaxed regex must still keep dangerous chars out."""
    for bad in [
        "name|cat /etc/passwd",
        "name>/tmp/x",
        "name<input",
        "name`whoami`",
        "name$(whoami)",
        "name\\nwith newline",
        "name\\with backslash",
    ]:
        out = _run(["--mode", "inspect", "--name", bad], monkeypatch)
        assert out["error"] == "invalid_name", f"accepted dangerous name: {bad!r}"


def test_unknown_mode_rejected_by_argparse(monkeypatch):
    with pytest.raises(SystemExit) as excinfo:
        _run(["--mode", "wat"], monkeypatch)
    assert excinfo.value.code == 2


def test_envelope_always_includes_plugin_version(monkeypatch):
    from skills_dump import PLUGIN_VERSION

    out = _run(["--mode", "inspect", "--name", ""], monkeypatch)
    assert out["plugin_version"] == PLUGIN_VERSION

    out = _run(["--mode", "hub"], monkeypatch)
    assert out["plugin_version"] == PLUGIN_VERSION


# --- source-scoped resolution (0.1.4+) --------------------------------------
#
# Regression guard for the marketplace bug: a skill shows up in `browse` but
# `inspect` returns `skill: null`. `inspect_skill(name)` re-resolves a bare
# name through `unified_search`, which skips index-covered external sources,
# caps results at 20, and dedupes by name. The fix passes the browse row's own
# `source`; the plugin resolves the skill ENTIRELY within that source
# (search + fetch on the owning adapter) and never re-enters `inspect_skill`.
#
# 0.1.6: critically, the source-scoped path does NOT feed a resolved slug back
# into `inspect_skill`. Many sources (clawhub/skills.sh/lobehub) use bare-slug
# identifiers with no `/`, so `inspect_skill` would re-run `_resolve_short_name`
# on the slug, which matches on the DISPLAY name and misses (slug
# `airbnb-gateway` vs name `Airbnb Gateway`). See
# `test_inspect_resolves_clawhub_slug_skill_directly`.
#
# These tests stub Hermes (absent in unit env) so the routing logic is
# exercised deterministically without a live registry fan-out.


class _Bundle:
    """SkillBundle-shaped stub: just a `files` dict (for SKILL.md preview)."""

    def __init__(self, files=None):
        self.files = files or {}


class _Meta:
    """SkillMeta-shaped stub: attribute access for the fields the resolver
    reads off a search hit."""

    def __init__(self, name, identifier, source, trust_level="community", description="", tags=None):
        self.name = name
        self.identifier = identifier
        self.source = source
        self.trust_level = trust_level
        self.description = description
        self.tags = tags or []


class _Adapter:
    """SkillSource-shaped stub: substring `search` over fixed metas, plus a
    `fetch` returning a fixed bundle (keyed by identifier)."""

    def __init__(self, sid, metas, bundles=None):
        self._sid = sid
        self._metas = metas
        self._bundles = bundles or {}
        self.searches = 0
        self.fetches = 0

    def source_id(self):
        return self._sid

    def search(self, query, limit=10):
        self.searches += 1
        q = (query or "").lower()
        return [m for m in self._metas if q in m.name.lower()][:limit]

    def fetch(self, identifier):
        self.fetches += 1
        return self._bundles.get(identifier)


def _patch_hermes(monkeypatch, *, adapters, inspect_map):
    """Stub `_import_sources` to yield `adapters` and `_import_hub` so the
    bare-name fallback `inspect_skill(name)` returns `inspect_map.get(name)`."""
    monkeypatch.setattr(hub, "_import_sources",
                        lambda: ((lambda _auth: adapters), object))
    monkeypatch.setattr(hub, "_import_hub",
                        lambda: (None, lambda ident: inspect_map.get(ident)))


def test_inspect_resolves_via_source_when_bare_name_misses(monkeypatch):
    # Bare name → None (the unified_search miss); identifier → real record.
    adapters = [_Adapter("github", [_Meta("PDF Tools", "anthropics/skills/pdf", "github")])]
    inspect_map = {
        "PDF Tools": None,
        "anthropics/skills/pdf": {
            "name": "PDF Tools", "description": "d", "source": "github",
            "identifier": "anthropics/skills/pdf", "tags": [],
        },
    }
    _patch_hermes(monkeypatch, adapters=adapters, inspect_map=inspect_map)
    out = hub.inspect("0.1.6", "PDF Tools", source="github")
    assert out.get("error") is None
    assert out["skill"]["name"] == "PDF Tools"
    assert out["skill"]["identifier"] == "anthropics/skills/pdf"


def test_inspect_maps_skills_sh_label_to_adapter_id(monkeypatch):
    # Browse row label is "skills.sh"; the owning adapter's id is "skills-sh".
    adapters = [_Adapter("skills-sh", [_Meta("Writer", "writer@1.0.0", "skills.sh")])]
    inspect_map = {
        "Writer": None,
        "writer@1.0.0": {
            "name": "Writer", "description": "", "source": "skills.sh",
            "identifier": "writer@1.0.0", "tags": [],
        },
    }
    _patch_hermes(monkeypatch, adapters=adapters, inspect_map=inspect_map)
    out = hub.inspect("0.1.6", "Writer", source="skills.sh")
    assert out["skill"]["identifier"] == "writer@1.0.0"


def test_inspect_all_source_uses_bare_name_only(monkeypatch):
    # source="all" (older iOS / no hint) must short-circuit before any search.
    adapters = [_Adapter("github", [_Meta("PDF Tools", "x/y/pdf", "github")])]
    inspect_map = {"PDF Tools": {
        "name": "PDF Tools", "description": "", "source": "github",
        "identifier": "x/y/pdf", "tags": [],
    }}
    _patch_hermes(monkeypatch, adapters=adapters, inspect_map=inspect_map)
    out = hub.inspect("0.1.6", "PDF Tools", source="all")
    assert out["skill"]["name"] == "PDF Tools"
    assert adapters[0].searches == 0


def test_inspect_resolves_clawhub_slug_skill_directly(monkeypatch):
    # The real-world bug: clawhub "Airbnb Gateway" has slug identifier
    # `airbnb-gateway` (no `/`). Feeding that slug to `inspect_skill` fails
    # (`_resolve_short_name` matches display name, not slug). The source-scoped
    # path resolves it directly off the clawhub adapter — so even with
    # inspect_skill returning None for BOTH the slug and the name, it works.
    bundle = _Bundle({"SKILL.md": "# Airbnb Gateway\nline2\nline3"})
    adapters = [_Adapter(
        "clawhub",
        [_Meta("Airbnb Gateway", "airbnb-gateway", "clawhub")],
        bundles={"airbnb-gateway": bundle},
    )]
    inspect_map = {"airbnb-gateway": None, "Airbnb Gateway": None}
    _patch_hermes(monkeypatch, adapters=adapters, inspect_map=inspect_map)
    out = hub.inspect("0.1.6", "Airbnb Gateway", source="clawhub")
    assert out.get("error") is None
    assert out["skill"] is not None, "clawhub slug skill must resolve, not null"
    assert out["skill"]["name"] == "Airbnb Gateway"
    assert out["skill"]["identifier"] == "airbnb-gateway"
    assert out["skill"]["skillMdPreview"].startswith("# Airbnb Gateway")
    assert adapters[0].fetches == 1  # preview came from the source's own fetch


def test_inspect_returns_detail_even_when_fetch_yields_no_preview(monkeypatch):
    # Metadata comes from the search hit; the SKILL.md fetch is best-effort.
    # A fetch miss must still yield the skill (install button needs only the
    # identifier), just without a preview — never a spurious null.
    adapters = [_Adapter("github", [_Meta("Ghost", "owner/ghost", "github")])]  # no bundle
    _patch_hermes(monkeypatch, adapters=adapters, inspect_map={})
    out = hub.inspect("0.1.6", "Ghost", source="github")
    assert out["skill"]["name"] == "Ghost"
    assert out["skill"]["identifier"] == "owner/ghost"
    assert "skillMdPreview" not in out["skill"]


def test_inspect_identifier_input_skips_source_search(monkeypatch):
    # A slash-bearing name is already an identifier → no source search at all.
    adapters = [_Adapter("github", [])]
    inspect_map = {"owner/repo/skill": {
        "name": "X", "description": "", "source": "github",
        "identifier": "owner/repo/skill", "tags": [],
    }}
    _patch_hermes(monkeypatch, adapters=adapters, inspect_map=inspect_map)
    out = hub.inspect("0.1.6", "owner/repo/skill", source="github")
    assert out["skill"]["identifier"] == "owner/repo/skill"
    assert adapters[0].searches == 0


def test_inspect_scans_other_sources_when_owning_source_misses(monkeypatch):
    # Owning adapter (lobehub) has no exact match; pass 2 finds it elsewhere.
    owning = _Adapter("lobehub", [])
    other = _Adapter("github", [_Meta("Roamer", "gh/roamer", "github")])
    inspect_map = {
        "Roamer": None,
        "gh/roamer": {
            "name": "Roamer", "description": "", "source": "github",
            "identifier": "gh/roamer", "tags": [],
        },
    }
    _patch_hermes(monkeypatch, adapters=[owning, other], inspect_map=inspect_map)
    out = hub.inspect("0.1.6", "Roamer", source="lobehub")
    assert out["skill"]["identifier"] == "gh/roamer"


def test_inspect_skips_centralized_index_in_pass_two(monkeypatch):
    # The index is the path we route around — pass 2 must not search it.
    index = _Adapter("hermes-index", [_Meta("Indexed", "idx/indexed", "github")])
    owning = _Adapter("clawhub", [])
    inspect_map = {"Indexed": None}  # nothing fetches → result stays null
    _patch_hermes(monkeypatch, adapters=[index, owning], inspect_map=inspect_map)
    out = hub.inspect("0.1.6", "Indexed", source="clawhub")
    assert out["skill"] is None
    assert index.searches == 0
