# Layered Memory MCP — Layered Memory Graph v1 Architecture

> Status: IMPLEMENTATION BASELINE — Batch J complete; Batch K next
> Branch: `refactor/memory-graph-v1`
> Baseline commit: `668e402` (`chore: freeze pre-graph refactor baseline`)
> Version: 3.6.0
> Date: 2026-10-28

## 1. Executive decision

This refactor does not choose between a graph and vector retrieval.

The system will use both, with different responsibilities:

- The memory graph represents semantic structure, temporal state, provenance,
  conflicts, supersession, scope, and task/procedure relationships.
- Vector and BM25 indexes provide fuzzy semantic entry points into the graph.
- L1 Markdown, Skill Markdown, and the TODO view remain human-readable projections.
- L0 remains a small routing projection, not a fact store.
- The changelog remains an append-only mutation/evidence trail, not a second
  editable ledger.

The design rule is:

> Vector retrieval finds plausible seeds. The graph decides what those seeds
> mean, whether they are current and in scope, how they relate to other facts,
> and which evidence supports them.

The graph must be rebuildable from source evidence and memory events. No graph
edge or node may become an untraceable, independently editable fact source.

## 2. Why this is a major refactor

The current system already has several useful layers:

- L0 routing/index information;
- L1 Markdown knowledge files;
- Skill files for procedural memory;
- TODO storage and lifecycle management;
- SQLite-backed keyword/BM25/vector retrieval;
- changelog, write guard, maintenance, rot audit, and session scanning.

The missing abstraction is not another search backend. It is an explicit semantic
state model for the things the system remembers and the relationships between
them.

Today, relationships are distributed across free-text sections, tags,
`related_ids`, headings, changelog entries, and retrieval metadata. That is
sufficient for simple lookup but weak for questions such as:

- What decision replaced the previous decision?
- Which evidence supports this preference?
- What was true at a particular time?
- Which procedure applies to this project and why?
- Which memories conflict, and has the conflict been resolved?
- Which task blocks a later task?
- Which entity links two otherwise separate knowledge domains?

The refactor therefore changes the semantic center of gravity from “documents
with search metadata” to “evidence-backed temporal memory graph with document
and retrieval projections”.

## 3. Design goals and non-goals

### 3.1 Goals

1. Represent facts, observations, experiences, policies, procedures, events,
   entities, evidence, and tasks as typed memory objects.
2. Represent support, contradiction, supersession, derivation, applicability,
   mention, and dependency as explicit typed edges.
3. Preserve temporal validity and lifecycle state without deleting historical
   facts by default.
4. Bind every remembered claim and important edge to evidence or an explicit
   derivation record.
5. Keep vector/BM25 search as a first-class fuzzy retrieval mechanism.
6. Make graph, vector, L0, and Markdown projections rebuildable and drift
   auditable.
7. Keep old L1 files readable during migration.
8. Support explainable retrieval: returned memories include paths, evidence,
   status, validity, and warnings.
9. Allow an embedded graph backend first and keep the domain layer backend
   independent.
10. Make every migration phase independently testable and reversible.

### 3.2 Non-goals

1. Do not replace the memory framework with Mem0, Letta, Graphiti, Hindsight,
   or Microsoft GraphRAG.
2. Do not make an external graph service a mandatory runtime dependency in the
   first implementation batch.
3. Do not use an LLM synchronously on every read or every low-level storage
   operation.
4. Do not infer that high graph degree means high confidence or relevance.
5. Do not make generated community summaries authoritative facts.
6. Do not physically delete superseded or retracted memory by default.
7. Do not rewrite every legacy Markdown file in one migration.
8. Do not create a second editable memory ledger beside L1 and the graph.
9. Do not expose private paths, credentials, production data, or user-specific
   operational details in public documentation.
10. Do not introduce REST, multi-agent distribution, or deployment changes as
    prerequisites for the graph foundation.

## 4. Source-of-truth hierarchy

The system has different kinds of truth. They must not be conflated.

```text
┌──────────────────────────────────────────────────────────────────┐
│ Immutable source evidence                                        │
│ Session messages, user statements, imported files, manual input  │
└──────────────────────────────┬───────────────────────────────────┘
                               │ records / supports
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│ Append-only memory events                                        │
│ inject, revise, supersede, retract, approve, restore, migrate   │
└──────────────────────────────┬───────────────────────────────────┘
                               │ projects semantic state
                               ▼
┌──────────────────────────────────────────────────────────────────┐
│ Memory graph: semantic state                                     │
│ typed nodes, typed edges, validity, scope, confidence, conflicts │
└───────────────┬──────────────────┬──────────────────┬─────────────┘
                │                  │                  │
                │ renders          │ indexes           │ routes
                ▼                  ▼                  ▼
       L1 / Skill / TODO     Vector + BM25           L0
       human-readable        retrieval projection    routing projection
```

During migration, existing L1 Markdown and changelog entries are the legacy
source material from which the first graph snapshot is built. The graph is not
allowed to silently invent facts during this import.

### 4.1 What each layer owns

| Layer | Owns | Must not own |
|---|---|---|
| Source evidence | Original wording and source location | Derived truth without source reference |
| Memory events | What mutation was requested/applied and why | Current state as a mutable snapshot |
| Memory graph | Current semantic nodes, edges, status, validity, scope | Untraceable source text |
| L1/Skill/TODO | Human-readable projection and operational views | Independent hidden relationships |
| Vector/BM25 | Searchable representations and ranking metadata | Lifecycle decisions that bypass the graph |
| L0 | Lightweight routing pointers and topics | Full facts or graph state |

## 5. Architecture diagram

### 5.1 As-is baseline

```text
[Session / manual input]
          │
          ├── writes ──> [L1 Markdown]
          │                  │
          │                  ├── projects ──> [L0]
          │                  └── indexes ────> [SQLite vector/BM25]
          │
          ├── writes ──> [TODO store]
          └── appends ─> [memory changelog]

[Recall]
  query ──> [keyword/BM25/vector] ──> [L1 sections] ──> [injection]
```

Status: existing and preserved during migration. The main weakness is that
relationships, validity, and provenance are not yet a first-class semantic
layer.

### 5.2 To-be target

```text
External/session/manual evidence
            │ records
            ▼
      [Evidence / Episode store]
            │ extracts or explicitly declares
            ▼
      [Memory graph service]
       │  nodes: Entity, Claim, Event, Experience,
       │         Procedure, Policy, Task, Evidence
       │  edges: SUPPORTS, CONTRADICTS, SUPERSEDES,
       │         DERIVED_FROM, APPLIES_TO, BLOCKS, ...
       │  state: scope, time, confidence, status, provenance
       │
       ├── renders ──> [L1 / Skill / TODO projections]
       ├── indexes ──> [Vector + BM25 projection]
       └── routes ───> [L0 projection]

Recall:
  query
    ├──> [BM25/vector/entity match] ──> seed nodes
    ├──> [graph traversal + hard filters] ──> paths/evidence
    ├──> [rank + conflict warnings]
    └──> explainable context package
```

Legend:

- Solid arrows are actual data or control flow.
- A projection is rebuildable and is not an independent fact source.
- Graph traversal is bounded by scope, validity, lifecycle, relationship type,
  and hop count.

## 6. Domain model

### 6.1 Node types

The initial graph schema is deliberately small.

| Node | Meaning | Typical source |
|---|---|---|
| `Episode` | One ingestion event, session, import, or manual capture | session/import/manual |
| `Evidence` | A located source excerpt or source pointer | Episode/file/message |
| `Entity` | A person, project, component, concept, or named object | extracted/explicit |
| `Claim` | An atomic statement about one or more entities | explicit/extracted |
| `Event` | Something that happened at a point or interval | session/tool/manual |
| `Experience` | A reusable lesson derived from one or more events | reviewed/derived |
| `Procedure` | A repeatable method, normally projected to a Skill | Skill/manual |
| `Policy` | A preference, invariant, or operating rule | user/system/manual |
| `Task` | A TODO item with lifecycle and dependencies | TODO store |
| `Community` | A derived cluster or summary, never an authority | graph analysis |

The initial implementation must not create a new node type merely because a
single import has an unusual sentence shape. New types require an explicit
schema decision and migration story.

### 6.2 Edge types

| Edge | Direction | Meaning |
|---|---|---|
| `EVIDENCED_BY` | Claim/Experience/Policy → Evidence | Direct source support |
| `DERIVED_FROM` | Claim/Experience → Episode/Claim/Event | Derivation lineage |
| `ABOUT` | Claim/Event/Experience → Entity | Subject matter |
| `MENTIONS` | Episode/Evidence → Entity | Source mention |
| `SUPPORTS` | Claim → Claim | One claim supports another |
| `CONTRADICTS` | Claim → Claim | Claims cannot both be treated as settled |
| `SUPERSEDES` | New Claim/Policy/Procedure → Old node | New state replaces old state |
| `APPLIES_TO` | Policy/Procedure → Entity/Scope | Applicability boundary |
| `IMPLEMENTS` | Procedure → Policy | Procedure realizes a policy |
| `OBSERVED_IN` | Event/Claim → Episode | Observation source |
| `BLOCKS` | Task → Task/Entity | Work dependency |
| `RELATED_TO` | Any semantic node → Any semantic node | Weak explicit relation only |
| `PART_OF` | Node → Node | Hierarchical containment |

`RELATED_TO` is intentionally weak. It cannot by itself justify a high-trust
answer. Evidence, support, applicability, supersession, and contradiction carry
stronger semantics.

### 6.3 Claim and edge metadata

Every semantic node and every non-trivial edge must be able to carry:

```yaml
id: claim_xxx
kind: claim
scope:
  user_id: optional
  agent_id: optional
  namespace: optional
status: active | pending | superseded | retracted | expired | archived
memory_class: fact | observation | experience | procedure | belief | policy
confidence: 0.0..1.0
valid_from: optional timestamp
valid_to: optional timestamp
recorded_at: timestamp
source_refs:
  - episode_id: episode_xxx
    evidence_id: evidence_xxx
    source_file: optional
    source_section: optional
    quote_hash: optional
derivation:
  method: user_explicit | extracted | inferred | summarized | migrated
  model: optional
  prompt_version: optional
  generated_at: optional
conflict_group: optional
version: integer
```

An edge adds:

```yaml
relation_type: SUPERSEDES
source_id: claim_new
target_id: claim_old
status: active | retracted | expired
valid_from: optional timestamp
valid_to: optional timestamp
confidence: 0.0..1.0
evidence_refs: []
```

A migrated legacy entry must be marked as migrated or inferred where the new
fields were not present in the original source. The migration must never make a
missing timestamp look like an observed timestamp.

## 7. Graph and vector coexistence

There is no technical conflict if responsibilities are explicit.

### 7.1 Vector responsibilities

Vector/BM25 indexes are optimized for:

- fuzzy wording and synonym matching;
- finding a query's likely semantic entry point;
- searching evidence snippets, claims, entities, experiences, and procedures;
- fallback retrieval when graph traversal is unavailable.

A vector row must identify the graph object it represents:

```yaml
vector_id: vec_xxx
graph_node_id: claim_xxx
graph_version: 17
memory_class: policy
status: active
scope: ...
valid_from: ...
valid_to: ...
projection_version: ...
```

The vector row is never allowed to bypass graph state. A stale vector hit is a
candidate only; graph status and validity are checked before injection.

### 7.2 Graph responsibilities

The graph is optimized for:

- entity and relationship navigation;
- multi-hop context;
- temporal validity and point-in-time state;
- supersession and contradiction;
- provenance and evidence paths;
- scope/applicability filtering;
- task, policy, procedure, and project dependencies.

The graph is not required to perform all natural-language similarity matching.

### 7.3 Unified recall

Default recall is a two-stage hybrid:

```text
1. Query understanding
   extract candidate entities, time, scope, relation intent

2. Seed retrieval
   BM25 + vector + exact entity aliases

3. Graph expansion
   one or two hops by allowed relation types

4. Hard filtering
   scope, status, validity, permission, conflict policy

5. Evidence backfill
   retrieve source excerpts for selected claims/paths

6. Ranking
   semantic relevance + entity match + evidence quality
   + direct-path bonus - distance - conflict penalty

7. Explainable result
   claims + path + evidence + status + warnings
```

Graph degree or centrality must not be used as a substitute for evidence or
relevance.

### 7.4 Query modes

| Query shape | Primary route | Secondary route |
|---|---|---|
| Similar experience or wording | vector/BM25 → graph | evidence backfill |
| Why/what replaced/what conflicts | graph → vector | evidence backfill |
| What applies to this procedure/project | entity + graph | vector for details |
| Global themes or repeated patterns | community projection | vector/evidence details |
| Graph unavailable | vector/BM25 | L0/L1 direct fallback |

## 8. Write path and state transitions

### 8.1 Single semantic write path

No direct independent writes to graph and vector stores are permitted.

```text
Evidence / user statement / imported source
                 │
                 ▼
       Normalize source reference
                 │
                 ▼
       Create or update Episode/Evidence
                 │
                 ▼
       Extract or explicitly declare Entity/Claim/Event
                 │
                 ▼
       Resolve stable identities and scope
                 │
                 ▼
       Detect support, contradiction, and supersession
                 │
                 ▼
       Append memory event
                 │
                 ▼
       Apply graph state transition
                 │
                 ├── render L1 / Skill / TODO projection
                 ├── update or queue vector/BM25 projection
                 └── refresh L0 routing projection when needed
```

The initial graph batches may import existing L1 and changelog data without
changing the hot write path. Integration is a later, explicitly tested batch.

### 8.2 Lifecycle rules

Default transitions:

```text
pending ──approve──> active
active ──revise────> superseded + new active version
active ──retract───> retracted
active ──expire────> expired
any historical state ──archive──> archived
```

Old claims remain available for historical queries but do not enter the default
current-context injection when superseded, retracted, expired, or out of scope.

### 8.3 Conflict rules

A contradiction is not silently resolved by whichever node was written last.

- Put contradictory claims in a `conflict_group`.
- Preserve both claims and their evidence.
- Mark the conflict unresolved until a user/agent review or stronger evidence
  creates a new state transition.
- Default recall may return the current candidate plus a conflict warning, but
  must not present an unresolved contradiction as a settled fact.

## 9. Storage backend strategy

### 9.1 Backend abstraction

The domain layer exposes a small graph store contract, independent of a vendor:

```text
GraphStore
  create_node(node)
  append_edge(edge)
  get_node(node_id)
  get_edges(node_id, relation_types, direction)
  traverse(seed_ids, max_hops, filters)
  find_conflicts(scope, entity_ids)
  get_provenance(node_id)
  rebuild(source_events)
  health()
```

The contract is intentionally smaller than a general graph ORM. It should expose
only operations needed by memory semantics and retrieval.

### 9.2 Backend phases

1. Domain model and in-memory/fixture implementation.
2. Embedded graph backend spike, preferably Kuzu or an equivalent embedded
   property-graph backend, behind the same contract.
3. Rebuild and drift audit against deterministic fixtures.
4. Optional external backend adapter only after measured need for multi-process,
   remote, or multi-agent operation.

No external graph service is a prerequisite for the first graph-aware recall.

### 9.3 Why not couple the domain model to Neo4j/Kuzu

A backend is an implementation choice. The semantic contract must survive:

- an embedded-to-server migration;
- schema evolution;
- export/import;
- graph rebuild;
- graph outage and fallback retrieval.

The system should not require a Cypher-specific type to appear in `KnowledgeEntry`
or in the MCP-facing model.

## 10. Projection and consistency rules

### 10.1 Projection identity

Every projection records:

```yaml
source_node_id: claim_xxx
source_version: 17
projection_kind: l1 | skill | todo | vector | bm25 | l0
projection_version: ...
projected_at: ...
```

This makes stale projections detectable without treating them as alternative
truth.

### 10.2 Rebuildability

The following must be independently rebuildable:

- graph from evidence/events;
- L1/Skill/TODO views from graph state plus source excerpts;
- vector/BM25 index from active searchable graph nodes and evidence;
- L0 from domain/entity routing data.

A rebuild must produce stable IDs for the same source identity and must report
unresolvable or ambiguous references rather than silently dropping them.

### 10.3 Failure and fallback

```text
graph-aware recall
       │ failure
       ▼
vector + BM25 with state metadata
       │ failure
       ▼
L0/L1 direct lookup
       │
       ▼
limited but honest memory behavior
```

A graph outage may reduce relation-aware context, but it must not fabricate
success or silently return known superseded facts as current facts.

## 11. Migration plan

### Phase 0 — freeze and measure

- Freeze the pre-graph baseline at commit `668e402`.
- Work only on `refactor/memory-graph-v1`.
- Keep all test data isolated from real memory stores.
- Record current test output and current index/store health.
- Do not rewrite legacy Markdown.

Exit criteria:

- clean baseline commit;
- branch exists;
- full test suite passes;
- migration input/output locations are explicit.

### Phase 1 — semantic foundation

- Add graph node, edge, evidence, scope, validity, status, and provenance
  domain models.
- Add validation for IDs, relation types, lifecycle states, and temporal
  intervals.
- Add a backend-neutral `GraphStore` contract.
- Add deterministic in-memory/fixture storage for tests.

No production write path or default recall behavior changes in this phase.

### Phase 2 — graph snapshot import

- Read legacy L1 sections, existing `related_ids`, tags, and changelog events.
- Produce Episode/Evidence/Entity/Claim nodes and typed edges.
- Mark inferred legacy metadata explicitly.
- Generate a graph import report: missing source, ambiguous identity, invalid
  metadata, duplicate IDs, and unresolved edges.
- Keep import idempotent and rerunnable.

### Phase 3 — embedded graph projection

- Implement the backend adapter behind `GraphStore`.
- Add schema/index initialization and health checks.
- Add full rebuild from the deterministic import stream.
- Add graph-vs-source drift audit.
- Keep the graph read-only from normal MCP mutation tools.

### Phase 4 — graph-assisted recall

- Add seed retrieval from existing BM25/vector search.
- Add bounded graph expansion and hard state/scope/time filters.
- Add evidence backfill and explainable result shape.
- Add graph-unavailable fallback.
- Compare graph-assisted recall with the existing recall path on a fixed fixture.

### Phase 5 — unified semantic write path

- Add explicit claim/entity/event extraction boundaries.
- Append memory events before applying graph state transitions.
- Implement supersession and contradiction handling.
- Update projections through versioned projection events.
- Preserve backward-compatible MCP parameters where possible.

### Phase 6 — projection ownership

- Render L1/Skill/TODO views from semantic state where the mapping is stable.
- Stop allowing ad hoc relationship mutation outside the semantic write path.
- Add vector freshness and projection drift monitoring.
- Enable graph-aware default recall only after acceptance metrics pass.

### Phase 7 — optional higher-order graph features

Only after the core graph passes stability gates:

- community detection;
- community summaries;
- global/local/DRIFT-like query modes;
- relationship pattern analysis;
- optional remote graph backend.

Community summaries remain derived and evidence-linked. They never replace
atomic claims as the fact source.

## 12. Batch implementation order

Each batch must be independently reviewable and testable.

### Batch A — baseline and architecture

- Baseline commit and branch creation.
- This document updated.
- No runtime behavior change.

### Batch B — domain contracts

- Graph node/edge/evidence models.
- Validation and serialization.
- Backend-neutral store protocol.
- Unit tests first, then implementation.

### Batch C — deterministic fixture graph

- In-memory graph store.
- Add/get/traverse/filter/provenance/conflict operations.
- Rebuild and idempotency tests.

### Batch D — legacy importer

- L1/changelog-to-graph conversion.
- Explicit inferred metadata.
- Import report and rerun tests.

### Batch E — embedded backend

- Backend adapter and schema.
- Rebuild, health, and drift tests.
- No default read/write cutover yet.

### Batch F — graph-assisted recall

- Seed-to-graph expansion.
- State/time/scope filtering.
- Explainable result and fallback tests.

### Batch G — semantic write integration

- Single write path.
- Supersession, contradiction, and projection events.
- Regression tests for current write tools.

### Batch H — reversible runtime integration (complete)

- Add the explicit runtime modes `legacy`, `shadow`, `graph_recall`,
  `graph_write`, and `graph`.
- Keep `legacy` as the default and preserve the original L1 result on graph
  failure, missing mappings, unavailable stores, or invalid graph metadata.
- Add additive graph recall output and an explicit semantic graph-write seam;
  do not silently turn the existing injector into a second write path.
- Add runtime/config/version regression tests and verify that the default path
  performs no graph operation.
- Accepted in commit `660c8e2` (`feat: add reversible graph runtime
  integration`): `1441 passed, 2 skipped`; focused Batch H tests `117 passed`.

Batch H is integration scaffolding, not production cutover. It does not yet
construct a persistent graph store, generate graph node IDs for real vector
rows, run a legacy backfill, enable graph-aware defaults, or publish graph
quality metrics.

### Batch I — production assembly and migration readiness

Batch I is deliberately split so that runtime assembly, retrieval wiring, and
legacy data reconciliation cannot silently become one irreversible migration.
The default mode remains `legacy` throughout this batch, and no real user
memory store is read or written by the Batch I test suite.

#### Batch I-A — persistent assembly and identity producers (complete)

- Resolve one explicit graph database path from framework configuration and
  initialize the SQLite graph schema through the existing `GraphStore`
  contract. The path is injectable in tests and is never inferred from an
  agent's private memory path.
- Define the deterministic producer that maps a searchable L1/vector entry ID
  to the canonical `graph_node_id` used by the legacy graph importer.
- Keep missing, malformed, and ambiguous mappings visible as degradation
  states; never fabricate an identity from a filename, domain, heading, or
  body text.
- Add bounded read-only diagnostics for assembly and seed coverage without
  leaking paths or memory contents.

Accepted in commit `deb2af6` (`feat: assemble persistent graph runtime`):
focused I-A tests `88 passed`; full suite `1504 passed, 2 skipped`.

#### Batch I-B — semantic recall wiring (complete)

- Wire only the MCP `search_semantic` path to the existing graph recall seam,
  because this path already carries explicit vector metadata containing the
  graph seed identity.
- Preserve the legacy response byte-for-byte in `legacy` mode: no graph call,
  no added `graph` field, and no changed result ordering.
- Keep `shadow` diagnostic-only and make `graph_recall`/`graph` additive;
  graph failure, unavailable stores, malformed metadata, and missing mappings
  must fall back to the original vector result.
- Do not wire `recall_knowledge`, REST `/knowledge/search`, or the write path
  in this batch; those entrances have different identity/provenance contracts.

Accepted in commit `37da26f` (`feat: wire graph recall into semantic search`):
focused I-B tests `102 passed`; full suite `1518 passed, 2 skipped`.

#### Batch I-C — legacy identity backfill and reconciliation (complete)

I-C is a controlled data-preparation batch, not a default cutover. Its
deliverables are a read-only inventory and dry-run planner over explicitly
supplied, isolated L1/vector/changelog snapshots, plus a reversible
vector-metadata publication seam. It classifies every input row before any
write:

1. **Eligible mapping.** A vector row with a valid stable entry ID maps to the
   canonical legacy claim node ID through the existing identity function.
2. **Already mapped.** The row already carries the expected graph seed
   metadata; the operation is a no-op and is reported as such.
3. **Unusable identity.** Blank, multiline, malformed, or conflicting IDs are
   reported and never repaired by guessing.
4. **Orphan or mismatch.** A vector row that cannot be matched to the supplied
   L1 snapshot, or whose metadata disagrees with the deterministic mapping, is
   quarantined in the report rather than silently rewritten.

The apply phase, if separately enabled after a clean dry-run, has these hard
properties:

- update only graph seed metadata; do not change authored L1 text, vector text,
  embeddings, ranking order, or source timestamps;
- use the existing atomic vector publication boundary rather than ad-hoc SQL
  mutation of individual rows;
- keep graph projection separate from vector metadata publication. The I-C
  implementation does not implicitly create graph nodes/edges or open a live
  graph store; any graph projection must be explicitly supplied, independently
  auditable, disposable, and rebuildable from the same snapshots;
- do not let vector metadata become an independent semantic truth source;
- be idempotent: rerunning the same snapshots produces the same graph state and
  reports zero additional changes;
- emit counts and stable reason codes, but never emit source paths, credentials,
  or memory contents in the report.

Rollback is projection rollback, not source deletion. Before apply, the
planner must capture a content-free snapshot fingerprint and the exact set of
rows it intends to change. If the fingerprint is stale, identity coverage is
incomplete, or the planner rejected any supplied input, the apply must abort
before publication. ``require_clean=False`` can override incomplete row
coverage only; it cannot override an input-contract error. A successful
metadata publication must be reversible by restoring the prior vector
snapshot; the L1 snapshot and append-only changelog are never rewritten or
deleted. Graph data remains disposable until the reconciliation report and
reopen/rebuild checks pass.

I-C explicitly does not include LLM extraction, entity resolution, conflict
arbitration, automatic access to the real memory directory, default mode
switching, physical deletion, REST integration, or write-path ownership
changes. Those remain later, independently gated work.

I-C was accepted in commit ``a28669e`` (``feat: add reversible graph identity
backfill``):

- focused I-C tests: ``77 passed``;
- related graph/runtime regression tests: ``190 passed``;
- full suite: ``1595 passed, 2 skipped``;
- compile, diff, allowlist, and protected-path checks passed;
- security audit: ``0 CRITICAL / 0 HIGH / 0 MEDIUM`` (two pre-existing LOW
  project-name/path matches).

The implementation is intentionally limited to vector metadata reconciliation.
It does not claim cross-storage transactionality, live graph projection, real
memory migration, or graph-aware default behavior.

I-C exit criteria:

- dry-run inventory accounts for every fixture row with stable reason codes;
- eligible mappings are deterministic, idempotent, and produce no invented IDs;
- dry-run and abort paths leave L1/vector/changelog fixtures byte-for-byte
  unchanged;
- apply tests prove stale-snapshot rejection and restoration of the previous
  vector snapshot;
- graph projection and vector metadata reconciliation have separate seams and
  are independently auditable;
- the full existing test suite passes and default `MemoryConfig()`/MCP
  behavior remains `legacy`.

#### Batch I remaining non-scope

- no default switch away from `legacy`;
- no destructive rewrite or physical deletion of L1, vector, or graph data;
- no direct access to production configuration or real user memory in tests;
- no external graph service or new mandatory dependency;
- no removal of the legacy recall/write paths.

#### Batch I-D — guarded semantic write-path assembly (complete)

I-D connected exactly one write entrance — MCP ``inject_knowledge_tool`` — to
the explicit semantic-write seam. The entrance remains L1/changelog-first and
authoritative; the graph block is additive and never claims a cross-storage
transaction. REST ``/knowledge`` CRUD, file-level CRUD, ``batch_inject`` and
``recall_knowledge`` remain deliberately outside this batch because their
identity/provenance contracts are different or they do not carry graph seed
metadata.

The caller may provide one serialized ``SemanticWriteRequest``. The request is
validated without repair and is the only source of graph identity, node
content, assertions, supersession, contradiction, or target IDs. Ordinary
``domain``, ``section``, Markdown content, filenames, and hashes never become
graph semantics by inference. Invalid requests are refused before the L1 write
and return bounded field/type labels only.

The mode behavior is intentionally asymmetric:

- ``legacy`` preserves the old response and performs zero graph operations;
- ``shadow`` validates and plans the explicit decision without rebuilding or
  mutating a graph store;
- ``graph_recall`` remains read-only for this write entrance;
- ``graph_write`` and ``graph`` refuse an otherwise valid request with
  ``graph_write_source_events_required`` because this tool does not possess a
  complete ``MemoryEvent`` source stream.

The last refusal is a safety boundary, not a missing convenience. The current
``GraphStore.rebuild(source_events)`` contract is destructive full replacement:
events outside the supplied stream do not survive. Rebuilding from only the
current request would therefore erase graph state produced by earlier writes.
I-D consequently performs no runtime seam call and no store operation on that
path; ``committed`` is false, while ``legacy_write_ok`` still reports whether
the authoritative L1 write succeeded. A graph failure or refusal never rolls
back L1, and a successful L1 write is never reported as a successful graph
commit.

I-D explicitly does not:

- infer claims/entities or automatically guess ``SUPERSEDES``/
  ``CONTRADICTS`` from Markdown;
- invent identity from filename, heading, domain, body text, or a new UUID;
- claim that ``MEMORY_CHANGELOG`` is a complete ``MemoryEvent`` stream;
- perform a real-memory backfill, default-mode switch, REST integration,
  physical deletion, or cross-storage transaction;
- make L1, vector metadata, or the graph a second editable ledger;
- open a live graph projection path until a later batch owns and supplies the
  complete source-event stream required by ``rebuild``.

Accepted in commit ``6846baa`` (``feat: add guarded semantic graph write
seam``):

- focused I-D/runtime/config tests: ``188 passed``;
- full suite: ``1640 passed, 2 skipped``;
- compile, diff, allowlist, and protected-path checks passed;
- security audit: ``0 CRITICAL / 0 HIGH / 0 MEDIUM`` (LOW results are existing
  project-name/path matches).

I-D exit criteria:

- default legacy calls remain graph-free and response-compatible;
- explicit requests are validated and bounded without leaking source content,
  private paths, or backend diagnostics;
- ``shadow`` is plan-only;
- write-enabled modes cannot reach ``run_semantic_graph_write`` or
  ``GraphStore.rebuild`` without the required source-event ownership;
- a pre-populated graph snapshot remains byte-identical on the refusal path;
- L1 success, L1 failure, no-change, and deferred outcomes are distinct from
  graph commit status;
- the full existing test suite passes and default ``MemoryConfig()`` remains
  ``legacy``.

### Batch J — measured shadow evaluation (complete)

Batch J adds an isolated, backend-neutral evaluation seam for running the
existing graph-assisted recall beside the legacy answer on explicitly supplied
fixture cases. It does not create a second recall path, read production
configuration, discover real memory data, or write a graph store. The evaluator
records:

- identity, order, count, and legacy-payload-preservation comparisons;
- returned graph result/node counts and fixture-grounded stale-node exposure;
- provenance coverage with an explicit denominator and missing node IDs;
- distinct graph-unavailable, graph-unused, and no-graph-material fallback
  reasons with corpus-level rates;
- per-case wall-clock latency and count/min/mean/p95/max summaries.

Evaluation output is measurement input rather than an acceptance verdict: it
does not embed thresholds, approval, or a cutover decision. Backend labels are
bounded and provenance warning parsing is explicit, so paths, diagnostics,
memory contents, and unknown warning fragments cannot enter the report.

Accepted in commit ``564cce3`` (``feat: add measured graph shadow
evaluation``):

- focused Batch J tests: ``57 passed``;
- full suite: ``1697 passed, 2 skipped``;
- compile, diff, AST boundary, protected-path, and no-``uv.lock`` checks
  passed;
- security audit: ``0 CRITICAL / 0 HIGH / 0 MEDIUM`` (LOW results are existing
  project-name/path matches).

Batch J deliberately leaves the default mode as ``legacy``. Acceptance
thresholds and any rollback decision remain a later governance step; no vendor
benchmark is treated as repository evidence.

### Batch K — controlled default transition (planned)

- Enable graph-aware recall only after Batch J passes its acceptance gates.
- Migrate write ownership only after one semantic write path can reproduce the
  existing L1/changelog/vector behavior and its rollback has been exercised.
- Remove obsolete paths only after an explicit equivalence or intentional
  behavior-change record.

## 13. Acceptance metrics

No backend switch or default cutover is justified by architecture preference
alone. Each phase must produce evidence for these metrics:

### Semantic correctness

- entity identity precision/recall on a neutral fixture;
- claim-to-evidence binding coverage;
- relation type accuracy;
- supersession and contradiction correctness;
- unresolved conflict visibility;
- temporal point-in-time query correctness.

### Retrieval quality

- seed recall with BM25/vector only;
- graph-assisted recall;
- multi-hop question recall;
- false-positive and stale-memory rate;
- evidence-path completeness;
- current vs historical answer correctness.

### Operational quality

- rebuild success rate;
- idempotent rebuild output;
- graph/index drift count;
- graph query p95;
- projection freshness lag;
- fallback behavior under graph failure;
- storage size and maintenance cost.

The first benchmark set should contain local memory-ledger cases plus selected
LongMemEval/LoCoMo-style temporal, updating, multi-hop, and conflict cases where
licensing and reproducibility permit. Published vendor benchmark scores are
references, not acceptance evidence for this repository.

## 14. Rollback strategy

### Code rollback

Each batch is committed separately. Revert the batch commit or switch back to
`main` without changing the pre-graph baseline.

### Data rollback

- Graph files are rebuildable projections.
- L1 writes retain version/changelog evidence.
- Superseded nodes remain available for history.
- Imported graph snapshots are disposable until the backend passes acceptance.
- Never point a new implementation at a real memory store without an explicit
  isolated configuration and backup/restore test.

### Runtime rollback

Feature flags control:

- graph import;
- graph-assisted recall;
- semantic write integration;
- projection rendering;
- default graph-aware injection.

Turning off graph-aware recall must leave vector/BM25 and L0/L1 fallback usable.

## 15. Security and privacy boundaries

- Evidence paths and quotes must respect the existing source privacy policy.
- Graph traversal must apply scope and permission filters before returning nodes.
- Logs must not print secrets, full private session bodies, or credentials.
- External graph backends require an explicit authentication and tenancy design;
  they are not enabled by merely adding a dependency.
- Test fixtures use neutral technical examples, not private business vocabulary.

## 16. Open decisions

The architecture is fixed enough to begin Batch B. These decisions are deferred
until the relevant evidence exists:

1. Embedded backend implementation: Kuzu or another backend with equivalent
   transactional/rebuild properties.
2. Whether the current vector store remains separate permanently or is later
   co-located with graph storage.
3. Exact entity-resolution strategy and whether model-assisted extraction is
   enabled in the write path.
4. Whether community summaries provide enough measured benefit to justify their
   indexing and maintenance cost.

None of these decisions may weaken the source-of-truth, provenance, rebuild, or
fallback rules above.

## 17. Upstream mechanisms used as references

This project adopts mechanisms, not wholesale runtimes:

- Graphiti/Zep: episodes, temporal relationships, provenance, and supersession.
- Mem0: scoped memory operations, history, and expiration semantics.
- Letta/MemGPT: durable memory blocks/files and explicit context boundaries.
- Hindsight: distinction between world facts, experience, observations, and
  evidence-bound reflection.
- A-MEM: explicit links between memory notes.
- Microsoft GraphRAG: local/global graph retrieval and derived community views.

These references do not override this repository's requirements for a single
semantic write path, source-backed state, rebuildability, local fallback, and
privacy boundaries.

## 18. References

- Graphiti: https://github.com/getzep/graphiti
- Graphiti episode/provenance concepts:
  https://help.getzep.com/graphiti/core-concepts/adding-episodes
- Microsoft GraphRAG: https://microsoft.github.io/graphrag
- Kuzu documentation: https://docs.kuzudb.com/
- Neo4j graph concepts:
  https://neo4j.com/docs/getting-started/appendix/graphdb-concepts/
- Mem0 search/update/expiration documentation:
  https://docs.mem0.ai/core-concepts/memory-operations/search
- Letta MemFS and memory blocks:
  https://docs.letta.com/concepts/memfs/index.md
  https://docs.letta.com/v1-sdk/memory/memory-blocks
- LongMemEval: https://arxiv.org/abs/2410.10813
- LoCoMo: https://snap-research.github.io/locomo
- Agent Memory Benchmark: https://github.com/vectorize-io/agent-memory-benchmark
