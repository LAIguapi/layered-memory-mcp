# Layered Memory MCP — Layered Memory Graph v1 Architecture

> Status: IMPLEMENTATION BASELINE — Batch L complete; production population and cutover remain open
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

- Freeze the pre-graph baseline before implementation begins.
- Keep all test data isolated from real memory stores.
- Record current test output and current index/store health.
- Do not rewrite legacy Markdown.

Exit criteria:

- the implementation boundary is frozen;
- test data and runtime data are isolated;
- the full test suite passes;
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

- Freeze the pre-graph baseline before implementation begins.
- Keep all test data isolated from real memory stores.

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
- The rollout default is now `graph`, with explicit `legacy` rollback. Preserve
  the original L1 result on graph failure, missing mappings, unavailable stores,
  or invalid graph metadata.
- Add additive graph recall output and an explicit semantic graph-write seam;
  do not silently turn the existing injector into a second write path.
- Add runtime/config/version regression tests and verify that the explicit
  `legacy` path performs no graph operation.
- Accepted after the reversible graph runtime integration: `1441 passed,
  2 skipped`; focused Batch H tests `117 passed`.

Batch H itself was integration scaffolding, not production cutover. It did not
construct a persistent graph store, generate graph node IDs for real vector
rows, run a legacy backfill, enable graph-aware defaults, or publish graph
quality metrics. The later graph rollout default does not imply real production
migration, a service restart, or production cutover.

### Batch I — production assembly and migration readiness

Batch I is deliberately split so that runtime assembly, retrieval wiring, and
legacy data reconciliation cannot silently become one irreversible migration.
The rollout default is now `graph`, while explicit `legacy` remains the
rollback/compatibility mode. No real user memory store is read or written by
the Batch I test suite.

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

Accepted after assembling the persistent graph runtime: focused I-A tests
`88 passed`; full suite `1504 passed, 2 skipped`.

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

Accepted after wiring graph recall into semantic search: focused I-B tests
`102 passed`; full suite `1518 passed, 2 skipped`.

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

I-C was accepted after adding reversible graph identity backfill:

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
- the full existing test suite passes, the `MemoryConfig()`/MCP rollout default
  is `graph`, and explicit `legacy` compatibility remains tested.

#### Batch I remaining non-scope

- the graph rollout default does not imply production migration or cutover;
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

Accepted after adding the guarded semantic graph write seam:

- focused I-D/runtime/config tests: ``188 passed``;
- full suite: ``1640 passed, 2 skipped``;
- compile, diff, allowlist, and protected-path checks passed;
- security audit: ``0 CRITICAL / 0 HIGH / 0 MEDIUM`` (LOW results are existing
  project-name/path matches).

I-D exit criteria:

- explicit legacy calls remain graph-free and response-compatible;
- explicit requests are validated and bounded without leaking source content,
  private paths, or backend diagnostics;
- ``shadow`` is plan-only;
- write-enabled modes cannot reach ``run_semantic_graph_write`` or
  ``GraphStore.rebuild`` without the required source-event ownership;
- a pre-populated graph snapshot remains byte-identical on the refusal path;
- L1 success, L1 failure, no-change, and deferred outcomes are distinct from
  graph commit status;
- the full existing test suite passes, the ``MemoryConfig()`` rollout default
  is ``graph``, and explicit ``legacy`` compatibility remains tested.

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

Accepted after adding measured graph shadow evaluation:

- focused Batch J tests: ``57 passed``;
- full suite: ``1697 passed, 2 skipped``;
- compile, diff, AST boundary, protected-path, and no-``uv.lock`` checks
  passed;
- security audit: ``0 CRITICAL / 0 HIGH / 0 MEDIUM`` (LOW results are existing
  project-name/path matches).

The rollout default is now ``graph``, with explicit ``legacy`` rollback; Batch J
measurement alone does not establish production cutover. Acceptance thresholds
and any production rollback decision remain a later governance step; no vendor
benchmark is treated as repository evidence.

### Batch K — separated evaluation and persistent parity (complete)

Batch K is split into evidence collection and a later governance decision. It
must not turn a mixed corpus of healthy and degraded cases into one misleading
quality number, and it must not treat an in-memory seam as evidence that the
persistent runtime is ready.

Accepted after separating graph evaluation and SQLite parity:

- focused Batch K tests: ``77 passed``;
- full suite: ``1774 passed, 2 skipped``;
- compile, diff, AST boundary, protected-path, no-``uv.lock``, and explicit
  default-mode checks passed;
- security audit: ``0 CRITICAL / 0 HIGH / 0 MEDIUM`` for both new files (LOW
  results are existing project-name/path matches).

The implementation is an isolated measurement helper. It accepts only explicit
classified fixture cases and explicit database targets; it does not read
configuration, environment variables, MCP state, or real memory data. It
produces observations and comparisons, not a verdict or a default-mode change.

#### K-A: separated shadow evaluation

The evaluation corpus is partitioned into two explicit classes:

1. **Normal-path cases** — a healthy graph, valid seed metadata, current and
   in-scope nodes, and resolvable provenance. These cases measure graph-assisted
   material, stale exposure, provenance coverage, legacy identity/order/payload
   preservation, and latency.
2. **Failure/fallback cases** — absent, unhealthy, raising, unmapped, or
   no-material graph conditions. These cases measure that the legacy answer is
   preserved, the fallback reason is specific and bounded, no graph write is
   reached, and no diagnostic/path/body leaks into the report.

The two classes are reported separately. A fallback rate is an operational
reliability input, not a retrieval-quality score; provenance coverage is
meaningful only over normal-path cases whose provenance denominator is supplied.
The corpus must contain positive and negative controls for stale exposure and
provenance gaps, rather than relying on an all-clean fixture.

The delivered evaluator keeps the normal and fallback aggregates separate while
preserving per-case measurements and declared-vs-measured disagreements. On the
neutral fixture used for acceptance, the normal class contains 2 cases: 3
graph-assisted results, 0 fallback rate, 0 stale exposure cases, and 4/4
provenance coverage. The fallback class contains 4 cases: 4/4 fallback rate,
with reasons ``graph_unavailable`` (2), ``graph_unused`` (1), and
``no_graph_material`` (1). Legacy identity/order/payload preservation is
measured independently in both classes; these fixture numbers are evidence
inputs, not universal quality claims.

#### K-B: persistent-runtime shadow verification

The same explicit neutral cases are rerun against a temporary
``SQLiteGraphStore`` database, closed and reopened before recall. This verifies
the actual persistent adapter together with the graph-recall seam without
opening a production path. The verification must compare the SQLite result to
the in-memory reference for node IDs, edge IDs, statuses, provenance,
fallback behavior, and bounded output. It must also verify that the runtime
assembly reaches the persistent store only in a graph-enabled mode with explicit
store configuration. The ``MemoryConfig()`` rollout default is ``graph``, while
explicit ``legacy`` compatibility remains tested and performs zero graph calls.

The delivered parity helper supports explicit nodes/edges or an explicit
``MemoryEvent`` stream, writes a temporary SQLite fixture, closes the writer,
reopens the same database, and invokes the existing graph-recall seam. On the
acceptance fixture, the stores matched on every compared bounded field, both
snapshots remained unchanged during recall, backend labels were bounded, and
the writer-close/reopen evidence was true. Backend label and latency are
recorded but deliberately excluded from equality because adapters and storage
media differ. The helper refuses blank or ``:memory:`` targets, implicit modes,
and ambiguous fixture sources.

#### K-C: cutover gate and rollback record

K-A and K-B now produce reproducible fixture evidence. Only after this evidence
has been independently expanded and reviewed may a separate review set
the cutover gates. The gate must state, with denominators:

- normal-path legacy identity/order/payload preservation;
- normal-path stale exposure and provenance coverage;
- failure-path legacy preservation and bounded fallback reason coverage;
- persistent-vs-reference semantic parity;
- p95 latency separately for in-memory and SQLite fixtures;
- write-path readiness, including ownership of the complete ``MemoryEvent``
  stream (currently **not met**), as a separate gate for ``graph_write`` and
  ``graph``. Read-only ``graph_recall`` does not require this event stream;
  it requires an explicitly populated, compatible graph snapshot instead.

No threshold is invented from the current six-case K-A fixture or the single
K-B parity fixture. K-C remains open: the repository still needs a broader,
reviewed corpus, explicit threshold decisions, a latency aggregate across
repeated in-memory and SQLite runs, and ownership of the complete
``MemoryEvent`` stream for graph writes. The rollout default is now ``graph``,
with explicit ``legacy`` rollback; this does not satisfy the K-C gates or change
production configuration. If a future production opt-in is enabled, the rollback
record must name the configuration change, the previous mode, the graph-store
snapshot/backup boundary, and the verification that explicit ``legacy`` restores
legacy recall. Removing obsolete paths remains out of scope until an explicit
equivalence or intentional behavior-change record.

### Batch L — stable projection identity and request-scoped graph assembly (complete)

The post-K source audit found two runtime gaps, independently checked on neutral
in-memory inputs: ``injector._vector_entries`` minted random IDs for ordinary
Markdown sections on every projection, and ``server.py`` had no caller of the
persistent-store assembly. Same-input projections changed both section IDs and
their derived graph seed IDs; an explicit frontmatter ID was preserved as a
control. These are runtime prerequisites, not grounds for a default cutover.

#### L-A: deterministic ordinary-Markdown projection identity

- Add one pure identity helper, consumed by ``_vector_entries`` only. Use a
  versioned, unambiguous canonical serialization of the exact domain, exact H2
  section label, and zero-based occurrence among identical labels. Hash that
  serialization with SHA-256 under a dedicated projection-ID prefix. Do not
  include body, summary, timestamps, or document-wide position.
- Preserve case, punctuation, dates, versions, and Unicode distinctions. The
  labels emitted by ``_bodyful_sections`` are the canonical parser input;
  reject blank/multiline helper inputs rather than silently repairing them.
  Do not use ``embedding_heading`` or ``heading_skeleton``: both deliberately
  erase distinctions that identity must retain.
- Preserve the authoritative ID of frontmatter-backed entries. Do not change
  standalone ``sync_to_vector_store(replace_domain=False)`` behavior or the
  canonical entry-ID-to-graph-node mapping.
- Repeated projection, body edits, and insertion/reordering of differently named
  sections preserve an existing section's ID. A heading or domain rename changes
  identity. Duplicate identical headings use positional occurrence within that
  label: insertion/deletion/reordering within this duplicate group cannot track
  the authored object reliably. This is an explicit limitation, not semantic
  entity resolution; do not infer identity from body similarity.
- This changes future ordinary-Markdown vector row IDs in legacy mode too. No
  existing database is rewritten in this batch. Any later production reindex
  needs a separately approved coordinated vector/graph rebuild and backup. Old
  row IDs and graph seed references are not silently aliased or backfilled.

#### L-B: explicit opt-in, request-scoped persistent recall

- Retain ``set_graph_store`` as a caller-owned injection seam. A non-None
  injected store takes precedence and is never closed by the server. Clearing
  it removes the override; graph-enabled requests may then use the explicit
  configured path, while unconfigured requests retain the old no-store fallback.
- Only after ``GraphModeDecision.observes_recall`` is true, and only without an
  injected store, use existing ``graph_store_session(config, require=True)``
  around the existing recall seam. Reuse its open/use/finally-close lifecycle;
  add no pool, singleton connection, startup hook, auto-import, or inferred path.
- Missing paths, failed opens, and query errors preserve legacy results and
  return bounded graph fallback diagnostics without paths, bodies, or exception
  messages. Missing parent directories are not created. SQLite initialization
  at an explicitly configured path remains the existing adapter's behavior;
  this is request-scoped use, not an OS-level read-only SQLite connection.
- Legacy mode returns before assembly, seed projection, or graph operations.
  ``graph_write``/``graph`` still refuse commits without complete source events;
  this batch does not wire assembly into the semantic write seam.

#### Scope, verification, and rollback

Implementation allowlist: new ``src/layered_memory_mcp/graph_entry_identity.py``,
``injector.py`` (ordinary-Markdown ``_vector_entries`` projection only),
``server.py`` (recall-store selection/lifecycle and related comments only), new
``tests/test_v360_graph_entry_identity.py``, and new
``tests/test_v360_graph_recall_path.py``. Design/status updates stay in this
document and ``CHANGELOG.md`` as separate documentation changes.

The user additionally approved the single assertion/comment correction in
``tests/test_vector_maintenance.py:629``: an unchanged H2 projection now keeps
its derived ID across rebuilds. All other assertions in that existing test were
retained, including frontmatter ID drift repair and maintenance health checks.

Require tests for repeat/body-edit/section-insertion identity, exact-label
collision controls, duplicate-heading boundaries, frontmatter preservation,
temporary vector replacement and importer/seed/traversal identity agreement.
Use deterministic fixture embeddings, never a model download. Exercise the
existing MCP semantic-search entry point with a temporary populated SQLite
graph, verify the session closes on success/error, injected stores remain
caller-owned, legacy performs zero opens/calls, failed assembly preserves the
legacy payload, and semantic write refusal leaves a prepopulated graph intact.
Run focused tests, full regression, compile checks, diff and privacy review.

No production/user-memory access, migration, event journal, dependency, new MCP
tool, REST wiring, default switch, or automatic filling of the graph is allowed.
The populated graph is an explicit fixture in this batch; production population
remains a separate open target. Imported snapshots must not be combined with a
partial event-stream ``rebuild``: that operation replaces the complete graph.

Code rollback is a separate batch revert; no persistent production change is
made to reverse here. A later production migration must restore vector and graph
backups together, rather than claiming a reindex recovers old random row IDs.
Disabling graph recall restores legacy responses without opening the graph.

#### Delivered evidence

Implementation checkpoint: focused identity, request-lifecycle, and
vector-maintenance tests **115 passed**; full regression **1830 passed,
2 skipped**; compilation and tracked/untracked whitespace checks passed. Security scans covered all six changed files with **0 CRITICAL,
0 HIGH, 0 MEDIUM**; LOW informational findings remain. AST comparison confirmed
the injector change is confined to ``_vector_entries`` and server function changes
to recall-store selection/lifecycle and injection documentation. The rollout
default is now ``graph``, with explicit ``legacy`` rollback; this batch introduced
no dependencies, lockfile, startup hook, migration, production configuration, or
production graph population.

The identity is ``mdsec1_`` plus the first 32 hexadecimal SHA-256 characters
(128 digest bits), with the version and length-prefixed exact labels in the
hashed payload. Temporary fixtures verify importer/seed/traversal agreement,
actual MCP semantic search over a populated SQLite graph, one open/close per
request, injected-store precedence, safe fallbacks, and unchanged write refusal.
This is isolated runtime evidence, not production retrieval-quality evidence.
Production population, a complete semantic event stream, broader measured
acceptance, and an explicitly approved production cutover remain open targets.

### Framework completion and migration delivery contract (framework delivered; cutover pending)

The next delivery is a working framework, followed by an executable migration
tool, not another fixture-only seam. The user authorized repository changes on
the existing branch. Production data access, installed-service changes, default
cutover, and execution against real user memory remain explicitly excluded.

#### Authority and write transaction

Persist validated `MemoryEvent` records in an append-only SQLite journal with
canonical payloads, stable request identity, ordered positions and explicit
projection checkpoints. The journal records explicit caller semantic decisions;
it is not a second independently editable knowledge database. The old changelog
remains historical evidence, not a substitute semantic event stream. Existing
import snapshots must be represented in the journal before a replacing rebuild.
Preserve source evidence and do not manufacture historical events or certainty.

Serialize concurrent commits; reject changed payloads under an existing request
or event identity. Validate a proposed complete replay before accepting an
invalid transition. Repeated identical requests must neither append duplicates
nor lose previous graph material. Replaying from the journal after process
restart must recover equivalent semantic state. Publish checkpoints only after
their actual projection succeeds; report an accepted event with pending
projections honestly instead of claiming an all-store transaction or discarding
an accepted decision. Retry must converge and never overwrite a newer projection.

Graph-write-enabled entry points must validate and commit through this authority
before projection side effects. An entrance not supporting a semantic operation
must refuse it before mutation, naming the supported alternative. Audit injection,
batch injection, whole-file CRUD, review, cleanup/consolidation, REST CRUD,
maintenance and task mutations for bypasses. The rollout default is `graph`;
retain explicit `legacy` rollback and legacy/shadow compatibility. Graph mode must
work using configured persistent stores without injected test handles or a
manually supplied event list.

#### Projections and read paths

Use one application layer to coordinate graph, readable L1, vector/BM25 and L0
projections with stable identity, observable watermarks and recoverable failures.
Do not silently merge legacy dedup/fusion decisions into explicit graph semantics.
Caller Agent decides entity/claim relationships and lifecycle changes; the
framework validates and applies them without text heuristics or model downloads.
Document supported semantic node-to-surface mappings and refusal of unsupported
mappings. External Skill files and externally owned tasks are not silently adopted
or overwritten: expose explicit ownership and mutation boundaries. Framework-owned
tasks must either follow the journal or reject incompatible writes before effects.

Unify graph use for MCP `search_semantic`, MCP `recall_knowledge`, and REST
`/knowledge/search`. Carry stable entry/seed identity from the actual producer,
including ordinary Markdown, rather than guessing from text. A healthy graph's
temporal/lifecycle/scope filters must affect the returned primary result, not just
an optional sidecar. Preserve documented legacy results on safe unavailable-graph
fallback; do not expose stale projections as current after accepted but unfinished
writes. Request-owned handles close on success and error; caller-owned handles
remain open. Keep policy in the shared layer instead of copying it into transports.

#### Framework acceptance before migration implementation

Exercise real MCP and HTTP entry points on temporary configured persistent stores
with neutral evidence and deterministic fake embeddings. Verify two sequential
commits retain earlier unrelated nodes, duplicate retry, conflicting retry,
concurrent writers, restart/replay, supersession, contradiction, temporal/scope
filtering, projection failure/recovery, and every mutating-entry bypass guard.
Include negative controls and actual returned payload assertions. Run the complete
existing regression suite, compilation, diff/scope checks and security review.
Update obsolete assertions only where an intentional contract change explains
the exact replacement behavior; never delete/skip or weaken meaningful tests.

#### Migration tool (only after framework acceptance)

Provide one operator CLI with explicit source and destination, read-only dry-run
by default, explicit apply/verify/rollback, structured inventory and reports.
Never auto-discover a user's store or switch their service/configuration. Inventory
L1/frontmatter, vector rows and available historical evidence; account for every
row as mapped, existing, missing, orphaned, conflicting or malformed. Reuse the
versioned identity projector and legacy importer. Preserve explicit IDs, original
source bytes and historical attribution; never infer old random identities using
fuzzy text, file names or new UUIDs. Unresolvable identity is a blocking/quarantined
finding, not a silent dropped row.

Build a complete journal plus graph and aligned retrieval/readable projections in
an isolated destination. Prefer a single staged generation and explicit publish
boundary over pretending multiple SQLite files and Markdown share a transaction.
Freeze source fingerprints, refuse drift before publication, validate every output
and graph seed, retain backups/manifests, and make identical reruns no-ops. Refuse
overlapping/symlink-escaped paths and unknown target contents. Recovery/rollback
must not overwrite post-migration changes without explicit refusal. Dry-run must
not write source or destination. Never physically delete old Markdown/vector rows.

Independently execute dry-run, apply, verify, duplicate apply and rollback using a
genuine old-format neutral fixture. Inject malformed inputs, conflicts, source
drift, publication failures and changed-target rollback attempts. Report verified
local capability separately from production migration and retrieval-quality gates.
Maintain code/documentation commit separation; no production cutover or real-data
migration is implied by framework or CLI acceptance.

#### Delivery checkpoint and migration implementation boundary

The framework implementation passed an independent full regression on the
system interpreter: 2094 tests passed, with no skipped tests. The working tree
was clean before migration work began.
This establishes the framework acceptance prerequisite, not production readiness.

The next implementation is a separate `graph_migration` operator module (runnable
with `python -m layered_memory_mcp.graph_migration`), not a replacement for the
existing `domain_keywords` migration command. Its inputs and destination are
explicit. Build and validate a private staged generation, then publish that
generation with one filesystem boundary; leave the source byte-for-byte intact.
Retain source backups, journal events, projection manifests and rollback evidence.
Rollback withdraws only an unchanged migration-owned generation and retains its
artifacts; it must refuse changed or unknown target contents.

Reuse the legacy importer only on a disposable in-memory planning store, convert
its validated snapshot into deterministic bootstrap MemoryEvents, and rebuild
the destination from the persisted journal. Never publish the importer's partial
per-record writes as the authority. Preserve explicit frontmatter identities;
ordinary Markdown uses the versioned section identity projector. Unresolvable
legacy vector identities remain accounted for and block publication unless an
explicit, validated identity mapping or full coordinated reprojection resolves
them. No body-similarity identity guesses or silent loss are permitted.

Tests first establish dry-run zero writes and successful staged publication,
then cover replay/duplicate apply, full readback, rollback, malformed/conflicting
inputs, source drift, symlink/overlap refusal, publication failure and target
drift. Use only neutral temporary fixtures and deterministic injected embeddings;
do not access real stores, install/restart anything, or change the default mode.
The CLI and scope/security boundaries are exercised independently before
marking migration acceptance complete. Documentation and code remain separate
changes. Real migration, publication and production cutover are separate
actions.

#### Runtime generation and update delivery contract (authorized; in progress)

The isolated migration operator includes source-backup provenance verification.
Its publication success does not mean a server is using the generation. The
next boundary is serving, not another importer or a second identity
implementation.

The runtime adapter consumes one explicitly supplied generation root or active
pointer. A never-served generation requires full publication verification before
activation. An activated generation is writable: publication-time database and
readable-file hashes are no longer a valid health test after an accepted write.
The pointer therefore binds the manifest verified at activation; reopening checks
that binding, path containment, read-only store health and journal/graph
consistency. Reports must distinguish publication integrity from attested runtime
health. Dry-run changes nothing; explicit activation atomically replaces only
the pointer and retains the previous attested target. Restore changes the pointer,
not the contents of either generation. Changed runtime generations must never be
silently discarded or overwritten by a repeat migration.

The first adapter batch leaves the normal `MemoryConfig` startup path unchanged.
The migration/adapter focused suite (201 passed) and complete regression
(2295 passed, no skipped tests) passed; compilation and tracked diff checks also
passed. That complete run used the normal launcher environment, not
a fresh offline home. A subsequent fresh-HOME offline run returned 2275 passed,
16 skipped and four failures in legacy vector tests requiring an uncached
embedding model. The offline complete gate is NOT_PASSED. Neither run establishes
that a production service consumed the pointer.

Earlier integration checks identified concurrency and pointer-lifecycle hazards
around generation activation, restoration, confirmation, and rollback. Those
behaviors are now covered by the generation runtime and concurrency regression
contracts; production cutover and publication remain separate operational gates.

Pointer-changing operations must serialize their complete read/validate/modify/
write/confirm interval across processes, not merely make the final replacement
atomic. Confirmation must match the requested target and manifest attestation,
not just find some usable generation. Preview must remain zero-write, including
lock and registration files. A durable, conservative activation ownership guard
must prevent migration rollback from moving any generation admitted to runtime,
even when a pointer is outside its destination or currently retains it only for
restore. Activation and withdrawal must coordinate through a shared generation
guard, so a preflight cannot race the rename. Runtime restore switches pointers;
it does not withdraw or erase a served generation. A missing explicit pointer
argument alone cannot prove that a generation is unreferenced. Retain the source,
generation and prior pointer on refusal; do not discover arbitrary user paths to
look for references. Any additional control-plane artifacts must have explicit
ownership, containment and validation contracts in the existing destination
inventory, without weakening unknown-file or symlink rejection.

Before changing the rollout default, the subsequent integration batch must:

1. Bind the explicitly configured active pointer through the normal configuration
   path used by MCP and HTTP, without replacing the complete service config with
   the adapter's narrower projection config. Resolve graph, journal, readable L1,
   vector and L0 paths together; retain unrelated configuration and external
   ownership boundaries. Invalid explicit pointers must not silently redirect
   writes to a legacy store or create an empty replacement generation.
2. Exercise real service entry points under an isolated home: migration, explicit
   activation, normal configuration loading, semantic write, close/reopen, recall
   and restore. Assert actual returned content and the locations of all changed
   projections, plus unchanged source bytes. Keep explicit legacy and shadow
   compatibility. Mode-enabled predicates must not depend on the rollout default.
3. Supply a project-owned post-update entry point and operator documentation.
   Do not modify an agent product's updater. The entry point runs after the
   operator has obtained the release; source, destination and pointer are explicit,
   dry-run is the default, and apply is opt-in. Reuse the existing migration and
   activation implementations rather than duplicating their policy. A failed
   migration or health preflight must leave the prior pointer and source intact.
   Repeat update must preserve accepted post-activation writes.
4. Change the default to the graph framework only after the normal startup/write/
   reopen chain has passed. Document fresh-store bootstrap separately from legacy
   migration: existing legacy data must not be treated as an empty graph merely
   because the default changed. Explicit configuration can override a default;
   repository tests alone do not establish the installed service's active mode.
5. Synchronize release versions, entry-point documentation and changelog, then
   rerun complete regression, negative-path checks, privacy review and
   wheel/sdist checks before pushing or publishing. No CI or package publication
   is implied merely by a git push or tag. Only after the release is verified may
   the operator pull/install that release and run its migration/update entry point.

##### Normal configuration binding and post-update implementation boundaries

Read-only call-path inspection identified the following next-batch plan. These
items are NOT_IMPLEMENTED and are not acceptance evidence. Implement and verify
the adapter concurrency/withdrawal repairs before entering this batch.

- Extend the complete `MemoryConfig` with `graph_active_pointer`, resolved by
  constructor, `LAYERED_MEMORY_GRAPH_ACTIVE_POINTER`, `graph.active_pointer` in
  the selected home's YAML, then unset. Resolve an explicit pointer after
  namespace handling but before any directory creation. Use the existing runtime
  resolver and bind graph, journal, knowledge, vector and L0 as one unit, without
  replacing unrelated service settings with the adapter's narrower config.
- Expose one `vector_db_path` on the config, with the existing home/data vector
  location as the unbound fallback. Do not relocate `data_dir`: review, TODO,
  access-log and other auxiliary stores retain their original ownership. Trace
  and adapt every config-driven vector reader/writer and parameterized helper,
  including MCP, HTTP, dashboard, semantic projection, injector, compaction and
  promotion. Do not implement a second vector publisher. Existing helper callers
  without a bound config retain their explicit legacy-path contract.
- Invalid, missing or unhealthy explicit pointers fail before mkdir, store/model
  construction or writes. Do not fall back to a legacy or empty store. Explicit
  legacy/shadow without a pointer and direct graph/journal paths remain supported.
  Reject a pointer combined with a non-authoritative mode or incompatible layout;
  do not silently override a namespace or claim that L0 format/tag compatibility
  is attested when the manifest does not encode it. Reject conflicting explicit
  graph/journal targets rather than selecting a mixed-generation binding.
- Exercise normal MCP and HTTP config/store factories, not only the standalone
  resolver: migration, activation, actual semantic write, handle close/reopen,
  recall and restore. Verify returned content, every projection's real path,
  unchanged source bytes and auxiliary-store locations. Keep the current default
  until this complete chain and the fresh-store contract have passed.

Fresh initialization is a separate explicit bootstrap operation, not migration
with invented source history. Default to preview; require explicit inspected
home/source, destination, pointer and apply. Existing legacy data blocks fresh
bootstrap. Build a staged empty journal, graph, vector, knowledge and L0 through
the existing stores/publishers. Distinguish fresh from migration in manifest and
verification: zero events and zero head/checkpoint are valid fresh authority, but
must not weaken migration source-backup or bootstrap-event verification. No
implicit bootstrap during normal startup; missing declared authority fails
without creating a replacement store.

Add a project-owned `layered-memory-update` entry point without repurposing the
existing legacy `layered-memory-migrate` command. Its post-install contract takes
explicit service home, source, destination and pointer; preview is the default.
It does not fetch code, install packages, restart a service or alter an agent
product's updater. Reuse migration plan/apply/verify and runtime activation APIs.
A pure configuration-resolution seam must allow update preflight before normal
config would create directories or reject a not-yet-activated pointer.

Provide an explicit prepare-binding step for that service home and pointer.
Apply may atomically update only required graph keys in the selected YAML,
retaining other fields and a recoverable original; duplicate or malformed graph
configuration is refused. Report binding preparation and activation separately:
there is no claimed transaction spanning config, generation and pointer files.
The run step must verify that effective service configuration selects this exact
pointer and authoritative mode; environment overrides or a missing binding block
execution. Operator success must not depend on an unperformed manual config edit.

An already activated, matching generation is checked through attested runtime
health and remains a no-op, including after accepted semantic writes; do not run
publication-time database hashes as its health gate. Explicit source-drift
preflight must refuse a new migration/cutover without changing the prior pointer.
Tests must distinguish first migration, no-op repeat, post-write repeat, refusal,
restore and partial preparation recovery. Only legacy-specific fixtures may opt
into explicit legacy mode after the default changes; tests of the rollout default
must assert the new default, never hide it behind a fixture override.

The acceptance evidence and operator commands belong in
`docs/GRAPH_MIGRATION_VERIFY.md`; this architecture section owns the integration
contract. Production execution, installed-service activation and remote release
must each be reported with their own read-back evidence, never inferred from a
successful isolated migration.

##### Completion batch — the deltas that close this contract

Status: implemented and verified locally. Nothing below is a
production claim; a separate release gate remains required, and no release,
installed-service activation or real-data migration is implied.

The five deltas, and what each one actually changed:

1. **Source classification** (`graph_migration_snapshot.py`). One policy decides
   every member. The known-exclusion table gained the operational state this
   project itself writes under a home — `.gitignore`, `.last_auto_compact`,
   `.last_auto_compact_attempt`, `.last_l1_dedup`, `write_guard_state.json` —
   each typed and classified from metadata alone. A root member this project
   does *not* own is covered only by an explicit per-run declaration
   (`--exclude NAME`, repeatable), validated as one literal, non-reserved,
   non-patterned name that is actually present; a declaration that matches
   nothing is a finding, so a stale list cannot pre-authorise a future unknown.
   A knowledge namespace subdirectory is accepted only once it has been *proved*
   empty by listing its names, and is recorded in the report and manifest as
   `empty_namespaces`; a nonempty namespace remains a coverage blocker and is
   never traversed or read. No installation-specific name is in the source.
2. **Rollout default and its tests** (`tests/`). `DEFAULT_GRAPH_MODE` stays
   `graph`. The suite's legacy-shape tests now request an opt-in
   `legacy_graph_mode` fixture one test, class or feature at a time, each with a
   written reason and a pointer to the graph-mode counterpart. There is no
   autouse fixture, no global env override and no blanket default patch, so the
   default remains the mode the rest of the suite exercises.
3. **Service-home binding** (`graph_home_binding.py`, wired into
   `graph_update.py`). `--home` now prepares and proves a binding instead of
   being informational. `plan_home_binding` writes nothing at all;
   `apply_home_binding` rewrites only the `graph:` block, preserving every other
   line byte-for-byte, after retaining the original and validating the *parsed*
   result; `verify_effective_binding` builds the complete `MemoryConfig` the
   normal startup path would build and checks it resolves this exact pointer,
   the authoritative mode and each projection. A duplicated or malformed graph
   section, an unknown graph key, a conflicting explicit target or a relevant
   `LAYERED_MEMORY_GRAPH_*` override is a refusal, not a fallback. An update is
   reported as applied only after that proof passes.
4. **Fresh bootstrap** (`graph_bootstrap.py`, `build_fresh_generation`).
   A separate explicit operator CLI, preview by default, requiring an inspected
   home, a destination and a pointer. Any legacy input under the home — knowledge
   Markdown, a vector database, an L0 index, a graph or journal file — blocks it,
   naming all of them. The staged generation is built through the existing
   publishers and declares `generation_type: fresh`; zero events, a zero head and
   zero checkpoints are its complete authority. Verification is split by declared
   type: a fresh generation must be *empty* (no backup, no source claim, no seed,
   no surface), and a migration generation must have authority — a zero-event
   journal is now an explicit `migration_generation_has_no_authority` failure
   rather than a vacuously satisfied manifest. A manifest with no declared type
   is read as a migration, the stricter of the two.
   An existing generation is proved with the gate its lifecycle calls for, via
   one shared `generation_is_served` seam: a served generation is writable, so
   it is checked through attested runtime health, and a never-served one is
   proved as a publication in full. Both are reported with an explicit
   `integrity` field. This was a defect in the first cut of this batch — the
   publication hashes were run against a served generation, which reported a
   correctly-written store as corrupt and made a repeat bootstrap fail because
   the service had been used.
5. **Bounded embedding batch** (`storage/vector_store.py`). One
   `EMBED_BATCH_SIZE = 16` at the single `model.embed` call site. Vectors, their
   order and the set of embedded texts are unchanged; only the arena the ONNX
   runtime retains is. Included as a rollout prerequisite, not as graph work.

##### Primary-entry acceptance matrix after cutover

The acceptance matrix below records the supported, refused-with-equivalent, and
suspended outcomes established by isolated integration checks and the regression
suite. These outcomes are deliberately not collapsed into a generic "safe" label.

| Entrance | Outcome |
|---|---|
| `inject_knowledge_tool` / `batch_inject_knowledge_tool` with `semantic_write_request` | **Supported.** Commits through the journal and publishes L1, vector and L0 inside the bound generation |
| `inject_knowledge_tool` without a decision | **Refused**, `graph_write_decision_required` — the agent-supplied decision is the design, not a gap |
| `recall_knowledge`, `search_semantic`, `get_l0_index`, `get_knowledge_*`, REST `GET /knowledge/search`, `/knowledge/{id}`, `/domains` | **Supported.** Read from the bound generation |
| `recover_graph_projections`, `graph_memory_status`, `get_memory_surface_capabilities` | **Supported** |
| whole-file L1 CRUD (MCP and REST), derived index/vector rebuilds, cleanup/reconcile, `compact_memory(dry_run=False)`, agent-owned surfaces | **Refused with an equivalent.** Each names a call that can actually do the job; dry-run and read-only forms are never refused |
| background jobs `auto_maintain`, `vector_maintenance`, `write_guard_refresh`, `watcher_l0_sync` | **Suspended and reported.** Each is the unattended twin of a refused-with-equivalent entrance |
| task mutations (`add_todo` / `update_todo` / `delete_todo`, dashboard `POST /update-todo`) | **Supported.** Each commits a typed task decision through the journal; `list_todos` / `get_todo_stats` / dashboard `GET /todos` read the replayable `todo` projection |
| `apply_semantic_lifecycle` (retract / restore / approve) | **Supported.** A target-only semantic decision; no replacement node is invented |
| review verdicts (`approve_knowledge` / `reject_knowledge`, REST and dashboard forms) | **Supported.** The graph path commits a target-only lifecycle event in the `review` namespace, carrying reviewer/note as authoritative payload; the review queue remains readable and legacy behavior is unchanged |

Review verdicts reuse the unified semantic lifecycle rather than writing an
unrelated claim or introducing a second ledger. `tests/test_v361_graph_review_decisions.py`
pins approve/reject payloads, idempotency and legacy compatibility, while
`tests/test_v361_graph_capability_matrix.py` pins the entrance classification.

Auxiliary ownership is unchanged for the stores that are still auxiliary: the
access log and the review queue remain the service home's own. The TODO database
is **no longer one of them** — it is the `todo` projection of the journal (see
the core-closure batch below).

Its *location* is unchanged, and that is a deliberate boundary with a stated
consequence. The task view resolves at `<home>/data/todos.db`, which under a
live `MemoryConfig` is the service home (as the graph, journal, knowledge,
vector and L0 paths resolve into the activated generation) and under a
generation-scoped runtime config is that generation's own root, exactly like its
vector database. Relocating operator task data was not in scope. So activating a
*different* generation, or rolling back to an earlier one, leaves a task view
whose rows the newly bound journal may have no authority for — and the honest
outcome of that is the one the adoption guard already gives: the task entrances
refuse, naming `seed_task_authority`, rather than rebuilding those rows away.

##### Core-closure batch — the four deltas that close the pre-cutover blockers

Status: implemented and verified by the repository's isolated regression
contracts. These changes do not constitute a production migration or cutover
claim.

1. **A task is a semantic decision, and the TODO view is a projection**
   (`graph_task.py`, `graph_journal.py`, `graph_projection.py`,
   `graph_application.py`, `server.py`, `dashboard_plugin/plugin_api.py`).

   A task mutation changes semantic state, so it is an event in the one journal
   — not a second ledger, and not direct SQL smuggled past the guard. A task is
   a `TASK` node whose payload carries the operational fields (`task_id`,
   `domain`, `priority`, task `status`, `blocked_by`, `notes`, the terminal
   timestamps); an **update is a new version plus `SUPERSEDES`**, exactly as a
   revised claim is, and a delete is a pure `RETRACT` of the current version.
   `task_id` is stable across every version, which is what an operator holds.

   `todo` joins `graph`, `l1`, `vector` and `l0` in `PROJECTION_NAMES`, so it
   has a checkpoint, it is covered by `recover_graph_projections()`, and it is
   rebuilt from the replayed stream rather than edited in place. The projection
   writes the same `todos` table the read entrances already read, so
   `list_todos`, `get_todo_stats` and the dashboard's `GET /todos` are
   journal-backed without a second response envelope.

   Four properties the implementation owes, and where they live:

   * **Transitions own the terminal timestamps.** `completed_at` / `cancelled_at`
     are set and cleared by the status transition alone, never by a caller.
     Reopening a completed task *clears* `completed_at` rather than leaving a
     finish time on a pending row.
   * **Replay uses event time.** Every timestamp a task row carries is stamped
     into the event payload when the decision is made, so replaying the journal
     reproduces the same rows rather than re-dating them to the replay clock.
   * **Pre-existing tasks are migrated, never rebuilt away.** `publish_todo`
     refuses a store holding rows the journal has no authority for
     (`todo_store_not_adopted:<count>`) instead of replacing them with a
     partial stream. `seed_task_authority` is the explicit migration that
     appends one bootstrap request carrying every existing row verbatim, with
     `derivation.method = migrated`.
   * **One authority.** There is no hidden legacy fallback: in a graph write
     mode the only way a task changes is a committed decision, and in `legacy`
     mode the store remains authoritative exactly as before.

   Deliberately *not* done: `blocked_by` stays a preserved payload field and is
   not minted into `BLOCKS` edges. An edge needs a projected node at both ends,
   and a legacy `blocked_by` entry is a free-form string, so converting it would
   turn unverifiable data into a replay-invalidating dangling edge.

2. **Pure lifecycle mutation** (`graph_write.SemanticLifecycleRequest`,
   `graph_application.commit_semantic_lifecycle`, `server.apply_semantic_lifecycle`).

   Deleting or restoring a memory no longer requires inventing a superseding
   node with fabricated content. The request carries a target, an action, a
   timestamp and a request key — and **no node**. The action is one of the
   existing target-only event kinds (`retract`, `restore`, `approve`);
   `supersede` is absent on purpose, because a supersession needs the
   replacement it declares. Validation is fail-closed before the journal: an
   unknown target, a scope that disagrees with the target's, and a transition
   the target's current status cannot take are refusals, not warnings. The
   §8.2 transition table is now one table in `graph_models`, read by both the
   projection and this validation, so the two cannot drift. A `restore` that
   would leave two current owners of one readable L1 surface is refused
   (`surface_owned_by:<node>`); history is preserved in every case.

3. **Recoverable L1 publication** (`graph_journal.py`, `graph_projection.py`,
   `graph_application.py`).

   The observed defect: `publish_l1` recorded a surface's body hash *before*
   writing the file, so a failed write left the registry describing bytes that
   never reached disk, and every later recovery attempt compared the file
   against that phantom hash and reported `surface_drift` — our own crash,
   permanently misclassified as an external edit, with `vector` and `l0` queued
   behind it forever.

   The fix is a two-phase publication protocol, not a relaxed comparison (the
   comparison is what legitimately protects a human's edit):

   * **Intent.** Before any byte is written, the desired state of every surface
     the publication will touch is recorded as a *pending intent* — coordination
     state, explicitly not a second semantic truth. The last published hash is
     left untouched.
   * **Write.** The document is replaced atomically: a temporary file beside the
     target, then one `os.replace`. A target file is never truncated in place
     and a retirement never deletes the file.
   * **Finalize.** Only after the replacement lands does the surface registry
     adopt the new hash (or mark the surface retired) and the intent clear.

   Both crash windows then recover without `force=True`. A crash *before* the
   replacement leaves the registry on the last published body, which is still
   what the file holds, so the retry is an ordinary republication. A crash
   *after* the replacement but before finalization leaves a file that matches
   the recorded *intent*, which `surface_refusal` recognises as our own
   unfinished write and finalizes. A body matching neither the last published
   hash nor the intent is still `surface_drift`, and still refused.

   The chain is also dependency-aware rather than strictly sequential: `l1`
   depends on `graph`, `vector` and `l0` depend on `l1`, and `todo` depends on
   `graph` alone — so a refused readable surface no longer holds the task
   projection hostage. A commit whose L1 write failed reports
   `l1_projection_pending:<entry id>` beside its `committed: true`, so a surface
   whose bytes never landed is not indistinguishable from one that published.

4. **Section-precise retrieval** (`recall.py`, `graph_search.py`).

   A keyword result is one *file* carrying several matched sections. The graph
   kept the whole file payload whenever any one seed survived, so a retracted,
   expired or out-of-namespace section rode along inside the payload of a
   surviving sibling in the same file. The producer now declares, per displayed
   section, the H2 surface identity that owns it; the graph layer screens each
   section against its own owning node and **rebuilds** the main payload —
   `sections`, `section_entries` and `matched_sections` — from the survivors.
   A file keeps its rank and its place. A displayed fragment whose owning
   surface cannot be identified is dropped from a file that had an excluded
   section and reported as `section_payload_unmappable:<count>`: with a known
   forbidden section in the same file, "we cannot prove this fragment is not
   part of it" must fail closed. Files with nothing excluded are returned
   byte-for-byte as before.

### Production snapshot adaptation — approved staged implementation

Status: Batch 1 implemented and independently accepted (183 focused tests plus
zero-write/copy checks); Batch 2 independently accepted (383 focused tests and
36 identity checks).
The full isolated offline run retains four model-cache-dependent failures;
neither batch establishes production cutover or release readiness. The strict
migration source layout remains unchanged. A separate, explicit snapshot
adapter prepares a bundle with `source/` (migration inputs), `evidence/`
(original L0 if present), and a versioned manifest outside `source/`.

Batch 1 accepts only operator-supplied source and new snapshot roots. Its
default plan inventories without creating directories. Explicit creation copies
top-level L1 Markdown, the vector database, and optional changelog into a private
staging bundle, compares deterministic per-file membership, size, mtime and
SHA-256 before and after copying, verifies copied payloads, and publishes by a
single same-filesystem rename. Source changes or copy faults never publish a
ready snapshot; staging debris is reported and retained. Source files are never
rewritten, checkpointed, locked or deleted by the adapter.

Configuration, credentials, model caches, repository metadata, auxiliary stores,
backups, locks and maintenance state are excluded through one centralized policy.
Exclusions are recorded by relative path/category, without opening their contents
or traversing excluded subtrees. Unexpected knowledge subtrees are explicit
coverage blockers, not silently discarded memory. Symlinks, special files,
overlapping roots, existing targets, missing required inputs and nonempty SQLite
WAL/journal files fail closed before publication. No default-home discovery,
configuration loading, service probing or automatic writer suspension is allowed.

An unchanged-copy result is observational stability, not proof of cross-file
transactional consistency. Reports must distinguish `observed_stable` from an
externally established write barrier; this batch does not certify a live store
as production-cutover-ready. SQLite native backup and coordinated writer fencing
remain separate work where necessary. Repeat creation refuses an existing target
without writing rather than overwriting or silently recopying it.

Batch 2 bridges legacy vector identities using unique exact domain, declared
section and projection-text correspondence, producing an auditable derived map
to existing canonical graph identities. It must preserve row IDs, text and
embedding bytes, reject ambiguous/stale maps and retain unresolved rows. Missing
graph metadata alone does not prove a row orphaned; identity resolution must
trace the row ID and current L1 producer. No fuzzy or embedding matching.

#### Batch 2 identity bridge contract

The migration inventory owns legacy-identity adaptation; the existing backfill
planner continues to own row-ID-based resolution, metadata validation and atomic
metadata-only publication. Do not change the canonical entry-ID producer or
create another graph identity algorithm. The bridge consumes only an explicitly
supplied immutable migration source, after the existing layout/reader gates.

For an otherwise valid unresolved row, automatic correspondence requires exact
domain, a declared metadata section and UTF-8 projection text equality, with one
candidate in that section. No whitespace, case, heading or body normalization is
allowed by the bridge. The graph target comes from the existing entry identity
function. Validate metadata and stored graph identities before accepting any
correspondence; malformed or conflicting identities cannot be overwritten.

A narrowly defined compatibility exception covers the known legacy embedding
projection wrapper: after the domain, declared section and unique-candidate gates
pass, the stored UTF-8 projection may equal exactly
`embedding_heading(section) + "\\n" + canonical_body`. The prefix must be generated
from that candidate's current section by the existing `embedding_heading` function;
no arbitrary prefix, suffix, trimming, case folding, Unicode normalization,
substring containment, similarity score or semantic inference is permitted. The
canonical body must occur at the exact end of the stored projection, and the
compatibility verdict must remain auditable as a distinct derived mapping reason.
L0 remains evidence-only and is not converted into a legacy knowledge input by this
exception.

Reserve targets already claimed by resolved rows or validated operator mappings.
Compute automatic proposals as a group: multiple rows proposing one target are
all unresolved, not first-row-wins. A duplicate-section candidate set is ambiguous
even when only one body matches. Operator mappings remain explicit declarations,
not semantic guesses; keep their existing validation and additionally prevent
collisions with automatic claims. Unresolved rows remain in the source and block
publication; this batch does not publish a partial or orphan-dropping generation.

Report every row's outcome and bounded reason, candidate count, mapping origin
and canonical target identity without echoing bodies, headings or arbitrary
metadata. Derived mapping evidence is recomputed on every plan/apply from the
current source fingerprint; do not cache or silently reuse a stale report. Both
automatic and explicit metadata updates must use the existing staged backfill
transaction, preserve unrelated metadata and leave row IDs, text and embeddings
unchanged. Manifest and verification must account for both mapping origins.

Acceptance uses neutral random-ID fixtures, exact success, absent/wrong section,
same-text wrong domain, whitespace mismatch, duplicate headings, duplicate claims,
malformed/conflicting metadata, explicit-map interaction, source drift, and a
end-to-end plan/apply/verify/no-op/rollback checks. Assert tree
metadata and hashes before any apply to prove dry-run zero writes. No production
data, default-mode changes, update hook, dependency change or service operation
belongs to this batch.

Batch 3 diagnoses projection/canonical reader disagreements without printing
memory bodies or headings. Parser defects are fixed generically with neutral
fixtures; external ownership is never invented merely to bypass a failed gate.
Production documents are not modified by this implementation batch.

Acceptance proceeds snapshot → identity report → plan → staged apply → verify
→ isolated normal read paths → retained rollback. Independent checks must cover
dry-run zero writes before apply, source drift, tamper, WAL refusal,
exclusion non-access, identity ambiguity, preserved payloads and no-op repeats.
Default mode, installed services, update integration and remote release remain
outside these batches until separately accepted.

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

Each batch is independently revertible. Restore a prior implementation boundary
without changing the pre-graph baseline.

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

The framework and isolated migration operator are implemented. SQLite is the
implemented embedded graph/journal backend; a different backend would be a
separately justified adapter, not a prerequisite for the current delivery.
These decisions remain deferred until the relevant evidence exists:

1. Whether an additional embedded or server backend offers enough measured
   benefit to justify its transaction, operational and rebuild costs.
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
