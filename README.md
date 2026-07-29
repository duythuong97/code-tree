# Code Tree Exporter

Pipeline tạo codebase knowledge từ nhiều source unit. `graph.sqlite` là nguồn truth;
`graph-index.json` và `codebase-memory/*.jsonl` phục vụ tool/CLI; `knowledge/*.md`
là projection nhỏ cho Copilot Studio/RAG. Pipeline vẫn giữ comment, encoding,
evidence và file tree để truy ngược về source.

V3 bổ sung catalog auto-import, hierarchy từ system xuống database/application,
input/output, materialized flow, resolution candidates và quality metrics mà
không đổi stable/numeric ID của graph V2. Plan đầy đủ nằm tại
[`docs/V3_PLAN.md`](docs/V3_PLAN.md); config và bốn CSV bootstrap nằm trong
[`examples/v3`](examples/v3).

## V3 bootstrap

Khởi tạo catalog bằng bốn file:

```text
catalog/incoming/database-tables__DB1.csv
catalog/incoming/database-columns__DB1.csv
catalog/incoming/database-tables__DB2.csv
catalog/incoming/database-columns__DB2.csv
```

Thêm vào config:

```json
{
  "catalog": {
    "folder": "./catalog",
    "autoImport": true,
    "strict": true,
    "duplicatePolicy": "error"
  },
  "enrichment": {
    "flowMaxDepth": 8,
    "maxFlowTargetsPerEntry": 100,
    "minimumResolutionConfidence": 0.85
  }
}
```

Mỗi CSV có structure khác phải có JSON profile trong `catalog/profiles`. Profile
map header nguồn sang schema chuẩn và có thể compile thành `jobnet.csv`,
`executable-mappings.csv` hoặc file legacy khác; importer không tự đoán schema.

## Config

```json
{
  "name": "order-system",
  "root": "${SOURCE_ROOT}",
  "output": "${EXPORT_OUTPUT}",
  "defaultEncoding": "auto",
  "inputData": "${CATALOG_DATA}",
  "outputMode": "partitioned",
  "combinedProjection": false,
  "knowledgeChunking": {
    "enabled": true,
    "maxMarkdownBytes": 1048576,
    "splitBy": ["source", "nodeType"]
  },
  "limits": {
    "maxTreeLines": 20000,
    "maxFileBytes": 10485760,
    "maxEvidenceSnippetChars": 500,
    "maxIssuesPerTypePerFile": 20,
    "extractorTimeoutSeconds": 300,
    "projectTimeoutSeconds": 900,
    "maxWorkerProcesses": 4
  },
  "sources": [
    { "name": "order-ui", "type": "angular", "folders": ["ui"] },
    {
      "name": "order-api",
      "type": "dotnet",
      "folders": ["api"],
      "database": "ORDERDB"
    },
    {
      "name": "order-db",
      "type": "oracle-plsql",
      "folders": ["db/plsql"],
      "database": "ORDERDB",
      "semanticDetail": "summary",
      "encoding": "cp932"
    },
    {
      "name": "order-sql",
      "type": "sql-files",
      "folders": ["db/sql"],
      "database": "ORDERDB",
      "encoding": "cp932"
    },
    {
      "name": "order-loader",
      "type": "sql-loader",
      "folders": ["db/loader"],
      "database": "ORDERDB",
      "encoding": "shift_jis"
    },
    {
      "name": "order-mappers",
      "type": "xml-sql",
      "folders": ["db/mappers"],
      "database": "ORDERDB"
    }
  ]
}
```

`root`, `output`, `inputData` nhận đường dẫn tuyệt đối hoặc tương đối với file config. Dùng đường dẫn tương đối hoặc biến môi trường để cùng một config chạy trên máy khác. `inputData` có thể bỏ nếu không có catalog, nhưng phải nằm ngoài `output` và `output.previous` để rerun không xóa input. `folders` là đường dẫn tương đối; chấp nhận cả `/` và `\\`, nhưng `/` được khuyến nghị cho macOS/Windows. Với `.NET`, `folders` có thể trỏ thẳng tới `.cs`, `.csproj`, `.sln`; pipeline tự stage project/solution reference closure. XML mapper có `mapper namespace` và statement SQL trong cùng source `.NET` được đọc tự động; XML cấu hình thông thường bị bỏ qua an toàn.

`outputMode` vẫn được giữ để tương thích config cũ. Graph luôn nằm trong một
`graph.sqlite`; logical source partition được lưu bằng `package_key` như
`sources/<source>` hoặc `global`, không tạo lại node/edge ở nhiều file.

Với `oracle-plsql`, `semanticDetail: "summary"` là mặc định: chỉ giữ call, tác
động database và control flow chứa các hành vi đó; các phép gán, `RETURN`, raw
expression và call arguments bị lược bỏ khỏi semantic tree. Dùng
`semanticDetail: "full"` khi cần projection theo từng statement.

`maxTreeLines` chỉ giới hạn projection Markdown; `maxFileBytes` ghi
`FILE_TOO_LARGE` và bỏ riêng file đó; timeout chỉ làm hỏng extractor/source đang
chạy, không xóa graph của source khác.

Runtime tùy extractor: Python 3.10+, Node.js + TypeScript cho Angular, .NET SDK 9 cho Roslyn. Có thể chỉ định executable không nằm trong `PATH` bằng `CODE_TREE_NODE` và `CODE_TREE_DOTNET`. Thiếu Node.js/TypeScript hoặc primary parser lỗi, Angular dùng Python fallback, đồng thời ghi `SEMANTIC_TREE_UNAVAILABLE`; fallback không bảo toàn đầy đủ nested behavior.

Sao chép `.env.example` thành `.env` trên mỗi máy rồi sửa path. CLI tự đọc `.env` cạnh file config; nếu không có thì đọc `.env` tại thư mục chạy. Biến đã export trong process có ưu tiên cao hơn `.env`. `.env` bị Git bỏ qua; chỉ `.env.example` được commit để liệt kê cấu hình cần thiết.

`defaultEncoding: "auto"` nhận diện theo thứ tự: BOM, khai báo `coding`/`charset`/`encoding` trong file, UTF-8 strict, UTF-16 heuristic, rồi CP932/EUC-JP strict. Kết quả legacy mơ hồ tạo `ENCODING_CONFLICT`; không dùng ký tự replacement nên không âm thầm làm hỏng tiếng Nhật. `encoding` và `encodingOverrides` vẫn dùng để khóa encoding cho source/file đặc biệt.

## Cài đặt

- macOS: `python3 -m pip install -r requirements.txt`
- Windows: `py -m pip install -r requirements.txt`
- Cài CLI từ source: `python3 -m pip install .` hoặc `py -m pip install .`

Node.js + TypeScript và .NET SDK 9 là runtime ngoài Python; chỉ cần cài khi
config dùng Angular hoặc .NET. Trên Windows, có thể đặt
`CODE_TREE_WINDOWS_WORKER` tới executable, Python script hoặc .NET DLL dùng
Visual Studio Build Tools/MSBuildWorkspace; nếu không đặt, .NET Framework chạy
syntax/config best effort. Nếu TypeScript semantic runtime không load được,
Angular dùng fallback chỉ giữ declaration/literal với confidence tối đa `0.5`
và ghi `SEMANTIC_TREE_UNAVAILABLE`.

## Chạy

Sau khi cài package, cả macOS và Windows:

```text
code-tree-exporter validate --config <absolute-config-path>
code-tree-exporter extract --config <absolute-config-path>
```

`code-tree-exporter --config <absolute-config-path>` vẫn được giữ để tương thích V2.
Kiểm tra một CSV lạ bằng `code-tree-exporter catalog inspect <csv-path>` trước
khi viết profile.

Chạy trực tiếp từ source:

- macOS: `python3 -m code_tree_exporter --config /path/extractor-config.json`
- Windows: `py -m code_tree_exporter --config C:\\path\\extractor-config.json`

`python -m cli` vẫn được giữ làm lệnh tương thích.

## Query knowledge

Sau khi generate output, query layer đọc manifest/index và query trực tiếp
`graph.sqlite` qua các index SQLite:

```text
code-tree-query --output <dist> find-node --qualified-name <name>
code-tree-query --output <dist> find-edges --source <node-id>
code-tree-query --output <dist> impact-api --method GET --path /orders/42
code-tree-query --output <dist> impact-table --database DB --schema APP --table ORDERS
code-tree-query --output <dist> trace-ui-to-db --query "GET /orders/{id}"
code-tree-query --output <dist> explain-node --node-id <node-id>
code-tree-query --output <dist> open-source --evidence-id <evidence-id>
code-tree-query --output <dist> list-issues --source order-api
code-tree-query --output <dist> search-memory --text orders
code-tree-query --output <dist> catalog-status
code-tree-query --output <dist> health
code-tree-query --output <dist> unresolved
code-tree-query --output <dist> input-output --node-id <node-id>
code-tree-query --output <dist> trace-flow --node-id <node-id>
```

Chạy trực tiếp từ source bằng
`python -m code_tree_exporter.query --output <dist> ...`. Response là JSON ngắn
gồm answer, graph IDs, evidence, source location, confidence và issues.

## Cấu trúc source

```text
code_tree_exporter/     # package, contract, extractor runtimes
scripts/                # tiện ích repository, không cài thành CLI
cli.py                  # compatibility shim
pyproject.toml          # build, dependency, package resources
```

## Output

```text
dist/
├── manifest.json
├── graph-index.json
├── graph.sqlite
├── quality-report.json
├── QUALITY_REPORT.md
├── codebase-memory/
│   ├── entities/*.jsonl
│   ├── relationships/*.jsonl
│   └── summaries/*.md
├── knowledge/
│   ├── manifest.json
│   └── APIs*.md, Flows*.md, Databases*.md, Jobs*.md, CrossSystem*.md
├── file-trees/
└── SYSTEM_TREE.md (khi combinedProjection=true)
```

Trong SQLite, `node_id`/`edge_id` là số nguyên 63-bit deterministic. ID mô tả
dài trước đây chỉ được lưu một lần ở cột `stable_id`; query CLI chấp nhận cả ID
số mới và stable ID cũ. Edge/evidence/comment/issue tham chiếu bằng khóa số nên
không lặp lại chuỗi ID dài. JSON response biểu diễn ID số dưới dạng decimal
string để không mất precision trên JavaScript.

Output được dựng trong staging rồi thay atomic. Rerun giữ snapshot trước tại `<output>.previous`. Chỉ thư mục có manifest `managedBy: code-tree-exporter` mới được thay/xóa; output không được quản lý bị từ chối để giữ dữ liệu người dùng. Decode strict; lỗi encoding tạo issue, file lỗi không được parse.
