# Extractor V3 Plan

## 1. Mục tiêu

V3 tập trung vào độ sâu và khả năng truy vết của extraction, không làm UI. Hệ
thống đích có 2 Oracle DB, khoảng 10 Angular UI, 4 .NET API và hàng nghìn batch.
Kết quả phải trả lời được:

- Thành phần này thuộc system, application, project, database hoặc job network nào?
- Entry point nhận input gì, tạo output gì và đọc/ghi table/column nào?
- Một flow từ UI/API/job đi qua các thành phần nào trước khi đến DB hoặc external system?
- Link nào được extract trực tiếp, link nào suy luận, link nào chưa resolve?
- Dữ liệu nào bị thiếu, file nào import lỗi và extraction có đủ tin cậy để sử dụng không?

V3 không extract local variable, assignment, literal hoặc expression nhỏ không
ảnh hưởng đến I/O, call graph, data flow hay control flow quan trọng.

## 2. Nguyên tắc tương thích V2

- Giữ nguyên extractor V2 và thứ tự merge/link hiện có.
- Không đổi công thức stable ID và numeric ID của node/edge V2.
- V3 chỉ thêm catalog chuẩn hóa, coarse hierarchy, I/O, candidate resolution,
  materialized flow và quality metrics.
- CSV V3 được compile về `tables.csv`, column CSV, `jobnet.csv` hoặc file legacy
  tương ứng trước khi extractor cũ chạy.
- Không tự đoán cấu trúc CSV lạ. Mỗi cấu trúc phải có profile JSON rõ ràng.

Pipeline V3:

```text
Catalog incoming CSV
  -> validate/profile mapping
  -> normalized legacy inputData
  -> existing source extractors
  -> existing global linker
  -> V3 hierarchy enrichment
  -> existing graph writer
  -> V3 SQLite enrichment + quality reports
```

## 3. Cấp độ extraction

```text
SYSTEM
├── DATABASE
│   ├── DATABASE_SCHEMA
│   │   └── TABLE / VIEW / MATERIALIZED_VIEW
│   │       └── COLUMN
│   └── PLSQL_PACKAGE / SQL_FILE / XML_SQL_MAPPER / LOADER_CONTROL
│       └── PROCEDURE / FUNCTION / TRIGGER / SQL_STATEMENT
└── APPLICATION ROOT
    ├── ANGULAR_PROJECT
    │   └── ROUTE / COMPONENT / SERVICE / UI_ACTION / API_CLIENT_CALL
    ├── API_APPLICATION
    │   └── SOLUTION / PROJECT / CONTROLLER / API_OPERATION / SERVICE / REPOSITORY
    └── DOTNET_PROJECT / JOB_NETWORK
        └── JOB / EXECUTABLE / COMMAND_MODE / ENTRY_POINT / METHOD
```

Không bắt buộc mọi source có đủ tất cả tầng. V3 thêm link structural còn thiếu,
nhưng không tạo hierarchy giả khi không có evidence hoặc stable scope phù hợp.

## 4. Input và output theo tầng

| Tầng | Input chính | Output bắt buộc |
|---|---|---|
| Bootstrap catalog | 2 table CSV + 2 column CSV | DATABASE, SCHEMA, TABLE/VIEW, COLUMN, provenance |
| Source inventory | Config `sources`, folders, system/repository/database | SYSTEM, application/project/database roots, FILE |
| Angular | Route, component, service, HttpClient call | UI entry, request input, API call reference |
| .NET API | Solution/project, controller/minimal API, DI, repository | API operation, route/body/query input, response output, call/data flow |
| .NET batch | Project/executable, jobnet, command mode | Job/entry point, parameters/files, predecessor, executable mapping, DB I/O |
| PL/SQL/SQL/XML | Package/routine/statement/mapper/loader | Routine input/output, table/column reads/writes, file I/O |
| Global linker | Tất cả package output + authoritative catalog | Exact link, unresolved reference, ranked candidates, confidence |
| Flow publisher | Entry nodes + selected semantic/data edges | Bounded UI/API/BATCH-to-target flows và flow steps |
| Quality | Catalog reports + graph + issues | Metrics trong SQLite, JSON report và Markdown report |

## 5. Bốn CSV khởi tạo

Folder chuẩn:

```text
catalog/
├── incoming/
│   ├── database-tables__DB1.csv
│   ├── database-columns__DB1.csv
│   ├── database-tables__DB2.csv
│   └── database-columns__DB2.csv
└── profiles/
```

`database-tables__<DATABASE>.csv`:

```csv
database_key,schema_name,object_name,object_type,status,comment
DB1,APP,ORDERS,TABLE,VALID,Order header
```

Trường bắt buộc: `database_key`, `schema_name`, `object_name`. `object_type`
nhận `TABLE`, `VIEW`, `MATERIALIZED_VIEW`; mặc định là `TABLE`.

`database-columns__<DATABASE>.csv`:

```csv
database_key,schema_name,object_name,column_name,ordinal_position,data_type,data_length,data_precision,data_scale,nullable,default_value,primary_key,comment
DB1,APP,ORDERS,ORDER_ID,1,NUMBER,,18,0,N,,Y,Order identifier
```

Trường bắt buộc: `database_key`, `schema_name`, `object_name`, `column_name`.
Column tham chiếu table không có trong table CSV sẽ bị loại và ghi issue.
`database_key` trong row phải khớp database ở tên file.

## 6. CSV có structure khác nhau

CSV lạ chỉ được import qua profile trong `catalog/profiles/*.json`. Profile định
nghĩa cách nhận diện file, mapping field, transform, file chuẩn đầu ra và identity
dùng xử lý duplicate.

```json
{
  "name": "scheduler-jobnet",
  "catalogType": "batch-jobs",
  "match": {
    "filename": "scheduler-*.csv",
    "requiredHeaders": ["network", "step", "program", "previous"]
  },
  "fields": {
    "jobnet_id": "network",
    "job_id": "step",
    "predecessor_job_id": "previous",
    "executable_name": "program"
  },
  "transforms": {
    "jobnet_id": ["trim", "upper"],
    "job_id": ["trim", "upper"]
  },
  "output": {
    "filename": "jobnet.csv",
    "fields": [
      "jobnet_id",
      "job_id",
      "predecessor_job_id",
      "executable_name"
    ],
    "identity": ["jobnet_id", "job_id"]
  }
}
```

Transform hiện hỗ trợ `trim`, `upper`, `lower`, `bool`. Output phải là đường dẫn
CSV tương đối an toàn. Profile có thể normalize jobnet, executable mapping,
connection mapping hoặc API mapping về đúng file mà extractor hiện tại đọc.

## 7. Những giá trị phải define trong config

- `name`: stable system/export name.
- `root`: root chứa tất cả source được scan.
- `output`: folder output, nằm ngoài `root` và catalog.
- `catalog.folder`: folder chứa `incoming` và `profiles`.
- `catalog.autoImport`: mặc định `true`.
- `catalog.strict`: `true` ở CI/production để fail khi có file/row rejected.
- `catalog.duplicatePolicy`: `error`, `first-wins` hoặc `last-wins`.
- `sources[].name`: unique source key.
- `sources[].type`: `angular`, `dotnet-api`, `dotnet-batch`, `oracle-plsql`,
  `sql-files`, `sql-loader` hoặc `xml-sql`.
- `sources[].system`: cùng một stable key nếu các source thuộc cùng system.
- `sources[].repository`: stable repository/application key.
- `sources[].database` và `schema`: authoritative DB context khi source dùng DB.
- `sources[].folders`: phạm vi source thật sự cần scan.
- `sources[].inputData`: dữ liệu legacy riêng của source; catalog normalized sẽ
  được overlay mà không xóa file legacy khác.
- `enrichment.flowMaxDepth`: mặc định `8`.
- `enrichment.maxFlowTargetsPerEntry`: mặc định `100`.
- `enrichment.minimumResolutionConfidence`: mặc định `0.85`.

## 8. Trình tự chạy

1. Copy bốn CSV mẫu, thay DB/schema/table/column bằng dữ liệu thật.
2. Với CSV structure khác, inspect header rồi tạo profile:

```text
code-tree-exporter catalog inspect <csv-path>
```

3. Validate config và catalog, chưa chạy source extractor:

```text
code-tree-exporter validate --config <config-path>
```

4. Chạy toàn bộ extraction:

```text
code-tree-exporter extract --config <config-path>
```

Lệnh V2 `code-tree-exporter --config <config-path>` vẫn hoạt động.

5. Kiểm tra chất lượng và flow:

```text
code-tree-query --output <output> catalog-status
code-tree-query --output <output> health
code-tree-query --output <output> unresolved
code-tree-query --output <output> input-output --node-id <id>
code-tree-query --output <output> trace-flow --node-id <id>
```

## 9. Output V3

V3 giữ nguyên `nodes`, `edges`, `evidence`, `comments`, `issues`, `sources` và
thêm các table SQLite:

- `catalog_files`: file, checksum, profile, số row import/reject.
- `io_items`: input/output của API, job, routine, method, SQL owner.
- `io_links`: I/O map tới table, column, file, API hoặc job target.
- `resolution_candidates`: tối đa 10 candidate cho reference chưa resolve.
- `flows`, `flow_steps`, `flow_io`: flow đã materialize với confidence/completeness.
- `quality_metrics`: global và per-source metrics.

File report: `quality-report.json` và `QUALITY_REPORT.md`; cả hai có checksum
trong `manifest.json`.

## 10. Quality gate đề xuất

Lần chạy đầu dùng để lập baseline, chưa đặt threshold cứng. Sau baseline:

- Catalog rejected file = 0.
- Catalog rejected row = 0, trừ exception được phê duyệt.
- Không giảm V2 stable ID hoặc numeric ID đối với cùng input/config.
- Unresolved reference và ambiguous issue không tăng ngoài ngưỡng baseline.
- Mọi API operation và batch entry quan trọng có ít nhất một input/output hoặc
  có issue giải thích vì sao không có.
- Flow bị giới hạn depth/target phải được đánh dấu incomplete hoặc theo dõi qua metric.

## 11. Kế hoạch rollout từ lớn đến chi tiết

### Phase A - System inventory

Chốt stable `system`, `repository`, `database`, `schema`, source ownership và
folder scope. Output là inventory source/config, chưa cần scan chi tiết.

### Phase B - Authoritative database

Import 4 CSV, tạo DB/schema/table/column graph, reject orphan/conflict và compile
catalog cho extractor cũ. Đây là nền để mọi source resolve cùng một identity.

### Phase C - Application extraction

Chạy theo nhóm để dễ kiểm soát: Oracle/SQL/XML, 4 API, batch, rồi Angular UI.
Mỗi nhóm phải đạt node/edge/evidence/issue baseline trước khi thêm nhóm tiếp theo.

### Phase D - Cross-source linking

Link UI -> API, API/batch -> method/routine, routine/SQL -> table/column,
job -> executable và predecessor. Link không chắc chắn được giữ candidate thay
vì tự động gắn sai.

### Phase E - I/O và flow

Materialize input/output và bounded flows cho API operation, job, command mode,
executable entry và UI entry. Không mở rộng xuống local variable/expression.

### Phase F - Quality và incremental operation

Thiết lập baseline, threshold, snapshot ID và lịch chạy theo nhóm source. Với
hàng nghìn batch, chia source config theo repository/job domain nhưng publish về
cùng một graph để global linker vẫn thấy toàn hệ thống.

## 12. Merge V2 và V3 sau này

V2 tiếp tục ở worktree chính; V3 ở branch `codex/extractor-v3`. Chưa merge khi
V2 còn thay đổi chưa commit. Khi V2 ổn định:

1. Commit V2 trên branch chính.
2. Merge branch chính vào `codex/extractor-v3`.
3. Resolve conflict chủ yếu tại pipeline, CLI, graph/contract, .NET catalog
   reader và docs; không lấy lại code cũ bằng thao tác destructive.
4. Chạy full Python tests, compileall, hai .NET builds và extraction snapshot.
5. So sánh V2 node/edge stable ID + numeric ID trước/sau; chỉ cho phép V3 thêm
   node/edge/table, không cho phép mất identity cũ.
6. Sau khi đạt gate mới merge V3 về branch chính.
