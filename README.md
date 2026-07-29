# Code Tree Exporter

Project chạy theo hai bước độc lập. Bước extract đọc source, tạo node/edge và
ghi vào `graph.sqlite`; bước export Markdown đọc lại database này để tạo
`knowledge/*.md`, `file-trees/*.md`, `codebase-memory/summaries/*.md` và
`SYSTEM_TREE.md`. Vì vậy có thể thay đổi projection hoặc export lại Markdown mà
không phải parse/link source lần nữa.

`graph.sqlite` là nguồn truth; `graph-index.json` và
`codebase-memory/*.jsonl` phục vụ tool/CLI. Comment, encoding và evidence vẫn
được lưu trong SQLite để truy ngược về source.

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

`combinedProjection`, `knowledgeChunking` và `maxTreeLines` được lưu vào
metadata SQLite làm mặc định cho bước export Markdown. `maxTreeLines` chỉ giới
hạn projection Markdown; `maxFileBytes` ghi
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
config dùng Angular hoặc .NET. Extractor `.NET` dùng `MSBuildWorkspace`, nên
SDK mà `global.json`/project yêu cầu cũng phải có trên máy. Nếu workspace không
load được project, extractor ghi `MSBUILD_WORKSPACE_DIAGNOSTIC` và chỉ dùng
fallback compilation cho các file bị ảnh hưởng. Lần chạy đầu build worker
Release; các lần sau chạy DLL trực tiếp nếu source worker không đổi. Trên
Windows, có thể đặt
`CODE_TREE_WINDOWS_WORKER` tới executable, Python script hoặc .NET DLL dùng
Visual Studio Build Tools/MSBuildWorkspace; nếu không đặt, .NET Framework chạy
syntax/config best effort. Nếu TypeScript semantic runtime không load được,
Angular dùng fallback chỉ giữ declaration/literal với confidence tối đa `0.5`
và ghi `SEMANTIC_TREE_UNAVAILABLE`.

## Chạy

### 1. Extract graph vào SQLite

```text
code-tree-exporter --config <absolute-config-path>
```

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
python3 scripts/export_markdown.py --database <dist>
```

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

## Output sau bước extract

```text
dist/
├── manifest.json
├── graph-index.json
├── graph.sqlite
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
