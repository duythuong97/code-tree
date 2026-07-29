# Code Tree Exporter

Project chạy theo hai bước độc lập. Bước extract đọc source, tạo node/edge và
ghi vào `graph.sqlite`; bước export Markdown đọc lại database này để tạo
`knowledge/*.md`, `file-trees/*.md`, `codebase-memory/summaries/*.md` và
`SYSTEM_TREE.md`. Vì vậy có thể thay đổi projection hoặc export lại Markdown mà
không phải parse/link source lần nữa.

`graph.sqlite` là nguồn truth; `graph-index.json` và
`codebase-memory/*.jsonl` phục vụ tool/CLI. Comment, encoding và evidence vẫn
được lưu trong SQLite để truy ngược về source.

V3 bổ sung catalog auto-import, hierarchy từ system xuống database/application,
input/output, materialized flow, resolution candidates và quality metrics mà
không đổi stable/numeric ID của graph V2. Config chạy mẫu là
[`demo-config.json`](demo-config.json); bốn CSV bootstrap nằm trong
[`examples/v3/catalog`](examples/v3/catalog).

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
    "folder": "${CODE_TREE_CATALOG}",
    "strict": true
  }
}
```

`autoImport=true`, `duplicatePolicy=error`, `encoding=auto` và giới hạn flow đã
có default; chỉ khai báo khi cần override.

Mỗi CSV có structure khác phải có JSON profile trong `catalog/profiles`. Profile
map header nguồn sang schema chuẩn và có thể compile thành `jobnet.csv`,
`executable-mappings.csv` hoặc file legacy khác; importer không tự đoán schema.

## Config

```json
{
  "name": "order-system",
  "root": "${CODE_MAP_DEMO_ROOT}",
  "output": "${CODE_TREE_OUTPUT}",
  "inputData": "${CODE_MAP_INPUT_DATA}",
  "catalog": {
    "folder": "${CODE_TREE_CATALOG}",
    "strict": true
  },
  "sources": [
    {
      "name": "order-api",
      "type": "dotnet-api",
      "system": "order-system",
      "repository": "order-api",
      "folders": ["api"],
      "database": "DB1",
      "schema": "APP",
      "sqlDialect": "auto"
    }
  ]
}
```

`root`, `output`, `inputData` và `catalog.folder` nhận đường dẫn tuyệt đối hoặc
tương đối với file config. `inputData` chỉ cần cho file legacy chưa chuyển sang
catalog profile. Mọi input phải nằm ngoài `output` và `output.previous`.
`folders` là đường dẫn tương đối; `/` được khuyến nghị. Với `.NET`, `folders`
có thể trỏ tới `.cs`, `.csproj`, `.sln`; pipeline tự stage project/solution
reference closure. XML mapper trong cùng source `.NET` được đọc tự động.
`sqlDialect` của `dotnet-api`/`dotnet-batch` nhận `auto`, `oracle` hoặc `none`.
`auto` chỉ parse embedded SQL khi file có dấu hiệu dùng Oracle; đặt `oracle`
khi source dùng SQL Oracle qua wrapper nội bộ, và `none` để tắt hoàn toàn.

Mặc định `allowPartialExtraction=false`: nếu một extractor lỗi, pipeline không
publish graph mới và output hợp lệ trước đó vẫn được giữ. Chỉ đặt
`allowPartialExtraction=true` khi chấp nhận graph thiếu source và các issue đi
kèm.

Output local nên đặt dưới `.artifacts/code-tree/<tên-lần-chạy>` thay vì tạo các
thư mục `output-*` ở repository root. `.env.example` đã dùng
`.artifacts/code-tree/demo`; toàn bộ `.artifacts/` được Git bỏ qua.

`outputMode` vẫn được giữ để tương thích config cũ. Graph luôn nằm trong một
`graph.sqlite`; logical source partition được lưu bằng `package_key` như
`sources/<source>` hoặc `global`, không tạo lại node/edge ở nhiều file.

Với `oracle-plsql`, `semanticDetail: "summary"` là mặc định: chỉ giữ call, tác
động database và control flow chứa các hành vi đó; các phép gán, `RETURN`, raw
expression và call arguments bị lược bỏ khỏi semantic tree. Dùng
`semanticDetail: "full"` khi cần projection theo từng statement.

`combinedProjection`, `knowledgeChunking` và `maxTreeLines` được lưu vào
metadata SQLite làm mặc định cho bước export Markdown. `maxTreeLines` chỉ giới
hạn projection Markdown; `maxFileBytes` ghi
`FILE_TOO_LARGE` và bỏ riêng file đó. `extractorTimeoutSeconds` giới hạn toàn bộ
worker; `projectTimeoutSeconds` giới hạn từng lần Roslyn mở solution/project.

Runtime tùy extractor: Python 3.10+, Node.js + TypeScript cho Angular, .NET SDK
9+ cho Roslyn. Có thể chỉ định executable không nằm trong `PATH` bằng
`CODE_TREE_NODE` và `CODE_TREE_DOTNET`. Runner tự suy ra `DOTNET_ROOT`, host
path, SDK resolver và runtime roll-forward từ executable đã chọn. Thiếu
Node.js/TypeScript hoặc primary parser lỗi, Angular dùng Python fallback, đồng
thời ghi `SEMANTIC_TREE_UNAVAILABLE`; fallback không bảo toàn đầy đủ nested
behavior.

Sao chép `.env.example` thành `.env` trên mỗi máy rồi sửa path. CLI tự đọc `.env` cạnh file config; nếu không có thì đọc `.env` tại thư mục chạy. Biến đã export trong process có ưu tiên cao hơn `.env`. `.env` bị Git bỏ qua; chỉ `.env.example` được commit để liệt kê cấu hình cần thiết.

`defaultEncoding: "auto"` nhận diện theo thứ tự: BOM, khai báo encoding ở header
Python/XML/HTML hoặc comment header, UTF-8 strict, UTF-16 heuristic, rồi
CP932/EUC-JP strict. Kết quả legacy mơ hồ tạo `ENCODING_CONFLICT`; không dùng ký
tự replacement nên không âm thầm làm hỏng tiếng Nhật. `encoding` và
`encodingOverrides` vẫn dùng để khóa encoding cho source/file đặc biệt.

## Cài đặt

- macOS: `python3 -m pip install -r requirements.txt`
- Windows: `py -m pip install -r requirements.txt`
- Cài CLI từ source: `python3 -m pip install .` hoặc `py -m pip install .`

Node.js + TypeScript và .NET SDK 9 là runtime ngoài Python; chỉ cần cài khi
config dùng Angular hoặc .NET. Extractor `.NET` dùng `MSBuildWorkspace`, nên
SDK mà `global.json`/project yêu cầu cũng phải có trên máy. Nếu workspace không
load được project, extractor ghi `MSBUILD_WORKSPACE_DIAGNOSTIC` và chỉ dùng
fallback compilation cho các file bị ảnh hưởng. Lần chạy đầu build worker
Release; các lần sau chạy DLL trực tiếp nếu source worker không đổi. Runner tự
cấu hình môi trường .NET từ `CODE_TREE_DOTNET` hoặc executable trong `PATH`. Nếu
TypeScript semantic runtime không load được, Angular dùng fallback chỉ giữ
declaration/literal với confidence tối đa `0.5` và ghi
`SEMANTIC_TREE_UNAVAILABLE`.

## Chạy

### 1. Extract graph vào SQLite

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

Bước này tạo `graph.sqlite` và các index/memory dạng machine-readable, không tạo
file Markdown.

### 2. Export Markdown từ SQLite

Ghi Markdown cạnh database:

```text
code-tree-export-markdown --database <dist/graph.sqlite>
```

Hoặc ghi sang thư mục riêng:

```text
code-tree-export-markdown --database <dist> --output <markdown-dist>
```

`--database` nhận cả file `graph.sqlite` lẫn thư mục chứa file đó. Có thể override
config đã lưu bằng `--max-tree-lines`, `--combined-projection` hoặc
`--no-combined-projection`.

Chạy trực tiếp từ source:

```text
python3 -m code_tree_exporter.markdown_export --database <dist>
```

## Query knowledge

Sau khi generate output, query layer đọc manifest và query trực tiếp
`graph.sqlite` qua các index SQLite. `graph-index.json` chỉ được nạp lazy khi
cần locator của codebase memory:

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
gồm answer, graph IDs, evidence, source location, confidence và issues. Trace
được chặn ở 5.000 node, 10.000 edge và 5.000 evidence; kiểm tra
`data.truncated` và dùng `graph.sqlite` khi cần duyệt toàn bộ graph rất lớn.

## Cấu trúc source

```text
code_tree_exporter/     # package, contract, extractor runtimes
scripts/                # tiện ích repository, không cài thành CLI
cli.py                  # compatibility shim
pyproject.toml          # build, dependency, package resources
```

## Output sau bước extract

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
│   └── manifest.json
```

## Output sau bước export Markdown

Nếu không truyền `--output`, các file sau được thêm vào `dist/`:

```text
dist/
├── markdown-manifest.json
├── codebase-memory/
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

Output extract được dựng trong staging rồi thay atomic. Rerun giữ snapshot trước
tại `<output>.previous`. Markdown exporter chỉ thay các projection do tool quản
lý và từ chối ghi đè output không có manifest hợp lệ. Decode strict; lỗi
encoding tạo issue, file lỗi không được parse.
