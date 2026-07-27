# Code Tree Exporter

Pipeline thống nhất: scan nhiều source unit, chạy extractor hiện có, hợp nhất graph CSV, giữ comment/encoding tiếng Nhật, sinh file tree và system tree.

## Config

```json
{
  "name": "order-system",
  "root": "${SOURCE_ROOT}",
  "output": "${EXPORT_OUTPUT}",
  "defaultEncoding": "auto",
  "inputData": "${CATALOG_DATA}",
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

`root`, `output`, `inputData` nhận đường dẫn tuyệt đối hoặc tương đối với file config. Dùng đường dẫn tương đối hoặc biến môi trường để cùng một config chạy trên máy khác. `inputData` có thể bỏ nếu không có catalog. `folders` là đường dẫn tương đối; chấp nhận cả `/` và `\\`, nhưng `/` được khuyến nghị cho macOS/Windows.

Runtime tùy extractor: Python 3.10+, Node.js + TypeScript cho Angular, .NET SDK 9 cho Roslyn. Có thể chỉ định executable không nằm trong `PATH` bằng `CODE_TREE_NODE` và `CODE_TREE_DOTNET`. Thiếu Node.js, Angular tự dùng Python fallback với độ chính xác thấp hơn.

Sao chép `.env.example` thành `.env` trên mỗi máy rồi sửa path. CLI tự đọc `.env` cạnh file config; nếu không có thì đọc `.env` tại thư mục chạy. Biến đã export trong process có ưu tiên cao hơn `.env`. `.env` bị Git bỏ qua; chỉ `.env.example` được commit để liệt kê cấu hình cần thiết.

`defaultEncoding: "auto"` nhận diện theo thứ tự: BOM, khai báo `coding`/`charset`/`encoding` trong file, UTF-8 strict, UTF-16 heuristic, rồi CP932/EUC-JP strict. Kết quả legacy mơ hồ tạo `ENCODING_CONFLICT`; không dùng ký tự replacement nên không âm thầm làm hỏng tiếng Nhật. `encoding` và `encodingOverrides` vẫn dùng để khóa encoding cho source/file đặc biệt.

## Cài đặt

- macOS: `python3 -m pip install -r requirements.txt`
- Windows: `py -m pip install -r requirements.txt`
- Cài CLI từ source: `python3 -m pip install .` hoặc `py -m pip install .`

Node.js + TypeScript và .NET SDK 9 là runtime ngoài Python; chỉ cần cài khi config dùng Angular hoặc .NET.

## Chạy

Sau khi cài package, cả macOS và Windows:

```text
code-tree-exporter --config <absolute-config-path>
```

Chạy trực tiếp từ source:

- macOS: `python3 -m code_tree_exporter --config /path/extractor-config.json`
- Windows: `py -m code_tree_exporter --config C:\\path\\extractor-config.json`

`python -m cli` vẫn được giữ làm lệnh tương thích.

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
├── nodes.csv
├── edges.csv
├── evidence.csv
├── comments.csv
├── issues.csv
├── file-trees/
└── SYSTEM_TREE.md
```

Output được dựng trong staging rồi thay atomic. Decode strict; lỗi encoding tạo issue, file lỗi không được parse.
