# SQLite Graph Contract

Generated output uses a single `graph.sqlite` database. Integer primary keys are
deterministic 63-bit hashes and are the published `node_id`, `edge_id`,
`evidence_id`, `comment_id`, and `issue_id` values. The former descriptive ID is
stored once as `stable_id` with a unique constraint and remains accepted by the
query CLI.

## Tables

- `nodes`: graph entities. `node_id` is the integer primary key; `stable_id` is
  the deterministic descriptive identity.
- `edges`: graph relationships. `source_node_id` and `target_node_id` are
  integer foreign keys to `nodes`.
- `evidence`: source locations for a node or edge. `target_type` selects which
  integer identity namespace `target_id` belongs to.
- `comments`: decoded source comments linked to `nodes.owner_node_id`.
- `issues`: diagnostics with an optional integer `source_node_id`.
- `sources`: decoded source-file metadata.
- `metadata`: database contract and generation metadata.

All graph tables include `package_key`; logical source partitions therefore
remain queryable without duplicating graph files. `properties_json` is a JSON
object. References inside known JSON values are rewritten to compact integer ID
strings during publication.

## Deduplication

Before compact IDs are assigned, the publisher collapses semantic duplicates
and rewrites all affected references. Nodes deduplicate by type, source scope,
and qualified name; inline SQL also includes normalized SQL text, and local
routine signatures remain distinct. Edges use their canonical identity tuple,
while evidence uses the canonical location key from the CSV interchange
contract. Duplicate comments and diagnostics are also collapsed after their
node owners are resolved. Complementary JSON properties are merged, with the
highest confidence, strongest graph role, and highest severity retained.

## Global reference resolution

Extractor packages may reference nodes that are unavailable while an individual
source is being scanned. They retain a deferred node, relationship, and evidence
instead of dropping the relationship based on the source-local catalog. After
every source package is merged, the publisher builds global indexes for APIs,
database objects, columns, and routines, then resolves all unique matches in one
graph-wide pass. Results are independent of source order, stale source-local
missing-object issues are removed, and embedded node references are rewritten.

If an endpoint is still absent after global resolution, it becomes an explicit
`UNRESOLVED_REFERENCE` node. This keeps partial or very large extractions
publishable with valid foreign keys while preserving the missing dependency for
later analysis. Resolution uses indexed lookups and batched edge/evidence
rewrites rather than rescanning the graph once per missing node.

## Integrity

- Node and edge references use SQLite foreign keys.
- Evidence target triggers enforce the conditional node/edge reference selected
  by `target_type`.
- Every descriptive `stable_id` is unique within its table.
- Query indexes cover node type/name, edge source/target/type, evidence target,
  comment owner, issue source/package/type, and logical package scope.
- `PRAGMA user_version` is `2` and `PRAGMA foreign_key_check` must return no rows.

Extractor subprocesses may still exchange temporary CSV package files with the
pipeline. Those files are internal staging data and are not published output.
