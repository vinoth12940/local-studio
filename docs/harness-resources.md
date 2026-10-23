# Workspace instructions and local skills

Both features are opt-in in `$LOCAL_STUDIO_HOME/data/settings.json`:

```json
{
  "harness": {
    "nested_instructions": true,
    "skills": true
  }
}
```

Both flags default to false. With nested instructions disabled, the existing
root-only loader still combines its supported instruction files. With skills
disabled, resource tools are absent from the request tool list and direct skill
dispatch returns a disabled error. Neither feature grants edit, shell, MCP, or
network permission.

## Nested instructions

For each directory from the selected workspace root to a declared target, select
at most one instruction file in this order:

1. `AGENTS.override.md`
2. `AGENTS.md`
3. `CLAUDE.md`
4. `.localstudio.md`
5. `.claude/CLAUDE.md`
6. `.cursorrules`

An empty higher-priority file intentionally suppresses lower-priority files at
that directory. An unreadable or invalid higher-priority file makes the context
incomplete; the resolver does not silently replace it with lower-priority text.
Ancestor instructions apply first, with deeper applicable scopes taking
precedence. A sibling's instruction file never applies to another subtree.
Multi-target results preserve separate target-to-instruction mappings; shared
content appears once with path, scope, SHA-256 digest, and truncation metadata.

`instructions.resolve_instructions(workspace, targets)` rereads content each time;
same-size edits, preserved modification times, deletion, and changed precedence
therefore change its fingerprint. Targets remain within the selected workspace;
parent traversal, symlink descendants, credentials, and protected user-data paths
are rejected. Reads are bounded to 256 KiB per instruction file, 4,000 characters
per selected file, 16,000 content characters overall, 16 targets, and 32 nested
directories. Serialized provenance/context also has a 48,000-character cap.

Truncation is visible as `complete: false`, never a claim that every rule was
loaded. Main chat preflight must deliver a new target context before an affected
edit and require the model to reissue the call. Incomplete contexts block affected
edits; bounded reads and clarification remain available. These conventions do not
analyze arbitrary shell/Python bodies for every path they might touch and are not
a filesystem sandbox.

`delegate_task(path=...)` scopes the child to a workspace file or directory while
retaining root instruction inheritance. A child receives applicable initial rules
before its first model call and newly resolved target rules before the read that
uses them. Relative file reads stay in the declared subtree; indexed personal
files and repository-wide git views are unavailable for subtree-scoped children.
An incomplete initial policy returns `needs_user` without launching the child.

## Skills

Skills are discovered one directory deep at:

- `<workspace>/.localstudio/skills/<name>/SKILL.md`
- `$LOCAL_STUDIO_HOME/data/agent/skills/<name>/SKILL.md`

Workspace skills shadow state skills with the same name. Stable tool IDs have
the form `workspace:<name>` or `state:<name>`; tools do not accept arbitrary skill
filesystem paths. A truncated workspace scan never falls through to state names
whose workspace counterparts may be unscanned.

`SKILL.md` starts with constrained scalar frontmatter, followed by its body:

```markdown
---
name: project-checks
description: Explain and run this project's relevant verification workflow
version: '1'
---

Read references/checks.md before selecting the relevant check.
Existing execution approvals still apply.
```

`name` and `description` are required; `version` is optional. The lowercase
kebab-case name must match its directory and fit 64 characters. Descriptions fit
512 characters, versions 64. This is deliberately not general YAML: duplicate
or unknown keys, nested values, tags, anchors, and multiline scalars are rejected.
Double-quoted values use JSON escapes; single-quoted values use doubled
apostrophes. A `permissions` field is invalid. Permission requests in the body
remain untrusted text.

- `list_skills` reads metadata only, exposing no skill body. Each root scans at
  most 128 directory entries; the catalog returns at most 32 skills and 16,000
  metadata characters, plus bounded warnings. Metadata fits 4 KiB and the whole
  skill file fits 64 KiB. Invalid entries are reported in warnings.
- `load_skill(skill_id, expected_sha256?)` reads the selected body, returns its
  actual content digest, provenance, and at most 12,000 body characters. Supplying
  a previous digest rejects changed content. With nested instructions enabled,
  the result also contains the applicable scoped instruction context.
- `read_skill_resource(skill_id, path, skill_sha256, start_line?, max_lines?)`
  requires the digest returned by `load_skill`. A changed skill must be loaded
  again. The relative resource path stays within that skill directory, rejects
  traversal/symlinks/credentials, and reads regular UTF-8 text only. Resources fit
  64 KiB; each response returns 1–200 lines, at most 8,000 characters, an actual
  resource digest, and explicit truncation. No resource is executed.

Catalog metadata hashes identify frontmatter; the whole-body hash is established
on load. Skill text and resources cannot override user authorization or application
safety policy. Project tests and skill bodies are not made tamper-proof by this
feature.

## Verification

`macos/check_harness_resources.py` uses temporary state/workspaces, synthetic child
model turns, and local file fixtures. It never starts a model engine or touches
live user state. Run it with the pinned backend dependencies:

```sh
TMPDIR=/tmp PYTHONDONTWRITEBYTECODE=1 uv run -q --no-project --python 3.12 \
  --with-requirements requirements-app.txt python macos/check_harness_resources.py
```

The suite covers precedence, empty overrides, nested/sibling/multi-target scope,
freshness and deletion, symlink/traversal/credential fences, context limits,
catalog/metadata/body limits, malformed skill metadata, digest-pinned resource
reads, default-off behavior, and scoped child instruction/tool integration.
