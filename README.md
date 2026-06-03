# onepilot-hermes-skills (Hermes plugin)

Read-only skill discovery surface for the [Onepilot](https://onepilotapp.com)
iOS app. Sits on top of Hermes' built-in `hermes_cli.skills_hub` helpers
(`browse_skills`, `inspect_skill`) and `agent.skill_commands.scan_skill_commands`,
combining them with a filesystem walk + `~/.hermes/config.yaml` +
`~/.hermes/skills/.hub/lock.json` to give the iOS app a single,
version-stamped JSON envelope per request.

## Public-repo discipline

This repo is public and the source ships to every user's host. **Write for strangers, not insiders.**

- **No backend stack hints.** Don't reference internal vendor names, project IDs, dashboard URLs, deploy hostnames, or service-internal tooling. Generic terms (`backend`, `auth provider`) over branded ones.
- **Comments only for the non-obvious why.** No multi-paragraph docstrings, no internal context that only makes sense to someone on the team. If a reader needs three paragraphs to understand a function, the code is wrong, not the comments. Default to writing none.
- **No JIRA / Linear / PR / incident references in code.** They rot, they leak our process. Put that context in the commit message where it belongs.
- **No hardcoded internal URLs or staging hostnames.** Anything network-bound comes from runtime config, never a constant in source.
- **Log lines are user-facing too** — they end up in the gateway log and the user's terminal. No PII, no full bearer tokens (prefix-only is fine for diagnostics), no internal paths.
- **Error messages exposed via JSON envelopes are bounded** — keep them generic exception-class names; no tracebacks.

## Why a separate plugin?

The Onepilot ecosystem already ships a `onepilot` Hermes plugin that
handles chat I/O and cron-delivery — runtime-critical surfaces. This
plugin is intentionally separate so:

- **Independent versioning** — a skill-fetch shape change ships without
  redeploying the chat plugin (and vice versa).
- **Smaller blast radius** — a bug here can leave the marketplace empty;
  it cannot touch the chat channel.
- **Faster iteration** — Hermes upstream API drift in `skills_hub`
  affects only this plugin; a one-line patch + plugin reinstall is
  enough, no iOS App Store cycle.

## Install

```sh
hermes plugins install https://github.com/sofiane8910/onepilot-hermes-skills
```

Lives at `~/.hermes/plugins/onepilot-hermes-skills/` after install. The Onepilot
iOS app probes for the script and offers a one-tap install when it isn't
present.

## Usage

```
python3 ~/.hermes/plugins/onepilot-hermes-skills/skills_dump.py --mode <mode> [args]
```

Modes:

| Mode | Args | Returns |
|---|---|---|
| `installed` | _(none)_ | `{plugin_version, skills:[…], count}` |
| `hub` | `[--page N] [--page-size N] [--source S]` | `{plugin_version, items, page, total_pages, total}` |
| `inspect` | `--name <skill>` | `{plugin_version, skill: {…} \| null}` |

Every envelope carries `plugin_version` so the consumer can detect drift.
Errors are returned as `{plugin_version, error: "<exception-class>"}` —
never as tracebacks. See `SECURITY.md` for the full threat model.

## Security

This plugin runs entirely offline. It performs zero network calls of its
own (Hermes' helpers may; that's their boundary, not ours). It writes
zero files. It reads only `~/.hermes/{skills,profiles,config.yaml}` and
the `HERMES_HOME` env var. It executes no subprocesses.

`ci/plugin/onepilot-hermes-skills/security-check.sh` (in the Onepilot repo)
greps the source tree on every CI run and fails on any of:
`import requests/httpx/urllib/socket/subprocess`, `os.system`,
`shell=True`, or any non-`HERMES_HOME` `os.environ[...]` access. See
`SECURITY.md` for the full invariant list.

## Release discipline

**Push == release.** This plugin doesn't have a `plugin_manifest` row of its own (yet) — it ships from `main` and users re-run `hermes plugins install` to upgrade. So every change pushed to `main` is, by definition, a release. That means every push must:

1. **Bump `PLUGIN_VERSION`** in `skills_dump.py` (the value the iOS app reads from every JSON envelope to detect drift). Same number you intend to tag.
2. **Bump `pyproject.toml`** so packaging tools see the same version.
3. **Tag the commit**: `git tag vX.Y.Z && git push --follow-tags`. The tag is what the iOS app's "installed plugin version" probe compares against `PLUGIN_VERSION` to surface upgrade hints.
4. **Bump the iOS-side mirror** if you changed the envelope shape: `HermesSkillsPlugin.swift` carries a constant matching `PLUGIN_VERSION` so the app refuses to talk to a wildly older / newer host plugin. Mismatch = "Reinstall" prompt.
5. (Future) When this plugin gets a `plugin_manifest` row of its own, this list grows to include the Supabase UPDATE — same discipline as the platform plugin (`onepilot-platform`). Until then: **don't push without bumping the version everywhere it appears**, or users land on a stale build that lies about its version.

The rule: a contributor browsing GitHub at any commit on `main` should see the same version number in `skills_dump.py`, `pyproject.toml`, and the latest tag. If they disagree, the release is broken.

## Development

```
cd onepilotapp/plugins/hermes/onepilot-hermes-skills
pytest tests/
```

## License

MIT.
