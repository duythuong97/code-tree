# Internal CSV Extractor Interchange Contract

This format is used only between extractor subprocesses and the pipeline. Final
generated output uses `graph.sqlite`; see `sqlite-contract.md`.

UTF-8, comma-delimited, RFC 4180 quoting. Header names and order are exact. Empty optional values are empty fields. IDs are deterministic and case-sensitive. `confidence` is a decimal in `0..1`. `properties_json` is a JSON object; use `{}` when empty. Paths are repository-relative `/` paths without `.` or `..` segments.

## `nodes.csv`

```text
node_id,node_type,technical_name,qualified_name,default_display_name,system_key,database_key,repository_key,graph_role,confidence,properties_json
```

`node_type` must occur in `node-types.json`. `graph_role` is `MAIN`, `TECHNICAL`, or `EVIDENCE`. Display/localized names, timestamps, schema, source lines, and full-file hashes never affect identity.

## `edges.csv`

```text
edge_id,source_node_id,target_node_id,edge_type,graph_layer,raw_operation,confidence,properties_json
```

`edge_type` and `graph_layer` must occur in `edge-types.json`. Extractors emit only `STRUCTURAL` or `TECHNICAL`; the materializer alone emits `DATA_FLOW`. Edge identity is the canonical tuple `source_node_id|edge_type|target_node_id|raw_operation|graph_layer`.

## `evidence.csv`

```text
evidence_id,target_type,target_id,source_path,start_line,end_line,start_column,end_column,evidence_kind,extractor_name,confidence,snippet,properties_json
```

`target_type` is `NODE` or `EDGE`. Lines and columns are positive when present; end positions cannot precede start positions. Evidence may be plural and must deduplicate by its canonical key. Evidence location, snippet, locale, and timestamp never alter node/edge identity.

Evidence canonical key is:

```text
target_type|target_id|source_path|start_line|end_line|start_column|end_column|evidence_kind|extractor_name
```

`source_path` must resolve to an existing repository-relative file during import validation. Empty ranges are allowed only when both start and end are empty; otherwise line and column bounds must be positive and ordered.

## `comments.csv`

```text
comment_id,source_path,owner_node_id,comment_kind,start_line,end_line,start_column,end_column,raw_text,normalized_text,language,encoding,properties_json
```

`raw_text` preserves decoded source text exactly. `normalized_text` is search-only. `owner_node_id` references the nearest following declaration when evidence permits; otherwise the containing `FILE` node. Classification is stored in `properties_json`.

## `issues.csv`

```text
issue_id,issue_type,severity,source_node_id,raw_reference,database_key,source_path,start_line,message,properties_json
```

`severity` is `INFO`, `WARNING`, or `ERROR`. `issue_type` additionally supports `ENCODING_ERROR`, `ENCODING_CONFLICT`, and `MERGE_CONFLICT` for unified-package extraction.

## `localized_texts.csv` (optional)

```text
target_type,target_id,field_name,locale,value,source_kind,review_status,author_name,created_at,updated_at
```

Packages may omit this file entirely. When present, `files.localized_texts`, `statistics.localized_texts`, and checksum entries for every declared chunk must describe it. `target_type` is a node type such as `TABLE`, `COLUMN`, `SCREEN`, or `API_OPERATION`; `EDGE` may be used for edge-localized text. `target_id`, `field_name`, and `locale` are unique together. Empty localization rows are not generated; missing English text is valid and display fallback is handled by the application.

## Manifest

Extractor staging manifests use `contractVersion: "1.0"` and map each table name
to one CSV filename. The public `json-schemas/manifest.schema.json` describes the
final SQLite output and does not validate these temporary manifests.

Validator scope required by sections 6-9 and 21:

- Exact UTF-8 CSV headers.
- Supported contract version.
- Node/edge/issue/evidence enums.
- Stable node ID prefix and route/path canonicalization.
- `LOCAL_ROUTINE` may use `local-routine:*` or signature-preserving `procedure:*`/`function:*` identity, because section 7.5 defines routine ID formats but no separate local-routine format.
- Canonical edge ID equals `edge:<sha256(source|edge_type|target|raw_operation|graph_layer)>`.
- Edge/evidence/issue references resolve to package or authorized external IDs.
- No source package declares authoritative `TABLE`, `COLUMN`, `JOB`, or `JOB_NETWORK` nodes.
- Duplicate node IDs, edge IDs, evidence IDs, issue IDs, or evidence canonical keys are rejected.
- Repository-relative source paths only; no absolute paths or traversal.
- Confidence is numeric in `0..1` and every `properties_json` is a JSON object.
- Optional localization rows reference existing package or authorized external node IDs, or existing package edge IDs.
