# ADR-009: agent-core owns prompt rendering and artifact mechanics

Status: accepted
Date: 2026-08-18
Context: [spec](../spec-executable-generation-agent-turn.md),
[design](../design-executable-generation-agent-turn.md)

## Context

UTA's durable graph already runs model work through agent-core's shared
`agent_turn`, but the preceding prompt node still renders with a UTA-owned
Jinja loader and writes files directly. The legacy adapter also renders in UTA
before using agent-core only as a file writer. This creates two undefined-value
policies, two artifact paths, and no durable `prompt_inputs_file` for the new
cycle.

UTA additionally marks part of several templates as a stable prefix for prompt
analysis/cache policy. The marker and policy are product-owned, but splitting
and rendering template sections is general prompt construction.

## Decision

Extend agent-core's existing `PromptLibrary` with an additive
`render_sections(name, boundary, values, **kwargs) -> RenderedPrompt` API.
`RenderedPrompt` is an immutable value containing stable and volatile rendered
text and an exact concatenated `text` property. The caller supplies the marker;
agent-core knows nothing about UTA, languages, phases, or provider caching.

The raw template is partitioned on the first marker occurrence before both
sections are rendered through the existing `StrictUndefined` environment. A
template without the marker is entirely volatile. An empty marker is invalid.
`render_sections` accepts `keep_trailing_newline`; UTA passes `False` to match
the exact historical `jinja2.Template` bytes, while existing consumers retain
agent-core's `True` default. No caller trims or normalizes rendered text.
`PromptLibrary` also offers opt-in source-text caching so UTA preserves its
current process-lifetime cache without owning a loader. Existing rendering and
artifact defaults remain unchanged.

UTA historically exposed both a whole-template render and independently
rendered stable/volatile sections. Those outputs differ by one LF at the
boundary. UTA preserves each API's bytes separately: split rendering uses the
real boundary, while whole-template rendering uses the same shared API with a
sentinel absent from the source. No compatibility path trims or normalizes the
difference.

UTA will use this API behind its prompt facade and will use the existing
`materialize_prompt` function for legacy and durable prompt artifacts. Its
additive strict metadata mode rejects unsupported/coerced values and oversized
payloads; UTA supplies a single allowlisted projection for both engines. UTA continues to
own template files, defaults, RDC-context validation, domain suffix
composition, and operation identity. Both paths write beneath the owner-only
UTA application-state `workflow-state/prompts` root outside the target
repository. One UTA resolver supplies managed identities and standalone
invocation UUIDs, validates symlink-resolved ancestry against the repository,
and never derives prompt placement from an arbitrary task DB path. Managed
artifacts share 30-day workflow retention; standalone scopes are deleted on
close and crash orphans after 30 days. `CycleState` explicitly declares both
artifact paths.

The low-level UTA `load_prompt` and `load_prompt_split` exports remain as
deprecated, one-release views supporting only `render(**values) -> str` and
the shared missing-variable exception. They contain no Jinja environment and
do not promise Template identity, `generate`, `stream`, or module access. They
may be removed only in a separately reviewed major UTA API cleanup.

## Consequences

- Missing variables fail before any agent session or model cost.
- Stable/volatile semantics are reusable by other agent-core consumers without
  importing UTA policy.
- UTA prompt output remains byte-compatible and gains deterministic identity
  metadata for durable artifacts.
- Source-bearing prompts cannot be swept into delivery's repository staging;
  the product retention owner deletes them with their workflow lineage.
- agent-core adds a small public API and releases version 0.6.0 before UTA pins
  it.
- UTA keeps Jinja for HTML report rendering, but not for agent prompts.
- Strict rendering may expose templates that relied on silent empty values;
  those must receive explicit UTA defaults rather than a permissive fallback.
- Legacy prompt migration is not protected by the durable-v2 flag. Release
  requires a default legacy canary; rollback deploys the previous UTA build and
  dependency pin.

## Alternatives considered

### Add a new `PromptBuilder` framework

Rejected. `PromptLibrary` and `materialize_prompt` already own the two required
mechanics. A hierarchy would duplicate working APIs and force consumers to
migrate for no additional capability.

### Keep section splitting in UTA

Rejected. Splitting and strict rendering must share one environment to prevent
legacy/durable drift, and another consumer may need the same stable/volatile
contract.

### Return a prompt-request DTO from every language phase

Rejected for this iteration. UTA phases append domain-specific text after
template rendering; changing every backend method adds surface without moving
additional common mechanics into agent-core.

### Serialize all template values beside the prompt

Rejected. Values can include source content, absolute paths, provider data, or
future live objects. The prompt itself preserves exact model input; a bounded
allowlisted identity projection provides safe audit context.

### Store prompt artifacts beneath repository `.uta_cache`

Rejected. Non-RDC delivery currently stages `.uta_cache/` wholesale and a
target repository is not required to ignore it. Source-bearing prompts and
metadata therefore belong under the existing owner-only workflow-state root,
not under Git control.

### Derive prompt storage from the configured task DB

Rejected. Standalone commands legitimately have no task DB, and a configured
DB may live inside the target repository. A dedicated application-state root
supports both modes and makes the Git boundary enforceable.

### Normalize newlines after rendering

Rejected. Trimming or adding newlines changes section bytes and hashes. Jinja's
native `keep_trailing_newline=False` mode reproduces UTA's legacy semantics and
is verified against frozen pre-migration fixtures.

### Remove UTA loader exports immediately

Rejected. They are exported even though repository production code does not
call them. A one-release `.render(...)` facade avoids an immediate source break
while clearly ending unsupported Jinja object behavior.
