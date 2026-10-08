# DevFlow Agent – VS Code extension cho GitHub Copilot (không cần MCP)

Extension tạo bộ **agents + skills + tools** cho quy trình:

```
Requirement (Excel) → Phân tích → Impact → Solution → Code → Test case (Excel) → Regression (Excel) → Review
```

Extension **không dùng MCP**. Tool được đăng ký bằng API của chính VS Code (`vscode.lm.registerTool`), agent là một Chat Participant (`@devflow`). Mọi thứ chạy trong process VS Code và dùng model Copilot bạn đang chọn.

## 1. Cài đặt

```bash
cd devflow-vscode
npm install
npm test          # unit test phần xử lý Excel / screen index / agent
npm run package   # tạo devflow-agent-0.1.0.vsix
```

Trong VS Code: `Extensions` → `...` → **Install from VSIX...** → chọn file `.vsix`.
Yêu cầu: VS Code ≥ 1.99 và đã cài GitHub Copilot Chat.

## 2. Khởi tạo cho dự án

Command Palette → **DevFlow: Initialize workspace**. Lệnh này tạo:

| File | Ý nghĩa |
|---|---|
| `.devflow/config.json` | Tên sheet requirement, mapping cột của template test case / regression / QA, thư mục output |
| `.devflow/screens.json` | **Screen index**: mã màn hình ↔ spec ↔ code ↔ file regression |
| `.devflow/templates/testcase_template.xlsx` | Template mẫu (thay bằng template thật của bạn) |
| `.devflow/agents/*.md`, `.devflow/skills/*/SKILL.md` | Bản copy để tuỳ chỉnh (ghi đè bản mặc định) |
| `.github/agents/*.agent.md` | Cùng các agent đó, để dùng trong **Copilot Agent mode** |

### 2.1 Cấu hình template test case (quan trọng nhất)

Mở template Excel thật của bạn, xem sheet nào, header ở dòng nào, data bắt đầu dòng nào, mỗi field nằm ở cột nào, rồi sửa `.devflow/config.json`:

```json
"testcase": {
  "template": "docs/templates/TestCase_Template.xlsx",
  "sheet": "TestCase",
  "headerRow": 7,
  "startRow": 8,
  "columns": { "id": "B", "screenId": "C", "reqRef": "D", "category": "E", "title": "F",
               "precondition": "G", "steps": "H", "testData": "I", "expected": "J", "priority": "K" },
  "required": ["id", "title", "steps", "expected"],
  "allowed": { "category": ["Normal", "Abnormal", "Boundary"] },
  "idField": "id",
  "idFormat": "TC-{screenId}-{seq:3}"
}
```

- Tên field (bên trái) tuỳ bạn đặt. Agent sẽ dùng đúng các tên này.
- Style, border và dropdown của dòng `startRow` (hoặc `styleRow`) trong template được copy sang mọi dòng mới.
- Giá trị dropdown (data validation) trong template được tự động đọc và dùng để validate.
- `regression` cấu hình giống `testcase`, áp dụng cho các file regression của từng màn hình.
- `requirement.sheets` là tên 4 sheet. Tool khớp không phân biệt hoa thường.
- `requirement.qa` là mapping cột của sheet QA, dùng khi agent ghi câu hỏi.

### 2.2 Screen index

```json
{ "screens": [ {
  "id": "SCR001", "name": "Customer search",
  "spec": "docs/specs/SCR001_CustomerSearch.xlsx",
  "code": ["src/main/java/**/customer/search/**", "src/main/webapp/customer/search*.jsp"],
  "regression": { "file": "docs/regression/SCR001_regression.xlsx", "sheet": "Regression" },
  "related": ["SCR002"],
  "tags": ["API:/api/customers", "TABLE:M_CUSTOMER"]
} ] }
```

Đây là "bản đồ" để agent phân tích impact. Mapping càng đầy đủ thì phân tích impact và chọn regression càng chính xác. Có thể viết script để sinh file này từ danh sách màn hình có sẵn.

## 3. Sử dụng

Trong Copilot Chat:

```
@devflow /analyze ticket: CR-123 #file:docs/req/CR-123_requirement.xlsx
@devflow /impact      (tự lấy ticket từ hội thoại)
@devflow /solution
@devflow /code        (sau khi bạn duyệt 03_solution.md)
@devflow /testcase
@devflow /regression
@devflow /review
@devflow              (không lệnh: xem ticket đang ở bước nào)
```

Sau mỗi bước sẽ có nút follow-up chuyển sang agent tiếp theo. Kết quả lưu ở `work/<ticket>/`:

```
01_analysis.md  02_impact.md  03_solution.md  04_code_changes.md
testcases_<ticket>.xlsx  06_regression.md  07_review.md
```

Các tool ghi hoặc sửa file quan trọng (sheet QA, regression, code) đều hiện hộp xác nhận trước khi chạy. Trước khi sửa file regression, extension backup file vào `work/_backup/`. File requirement gốc không bị sửa: câu hỏi QA được ghi vào bản copy `*.qa-updated.xlsx`.

### Dùng trong Copilot Agent mode

Các tool cũng dùng trực tiếp được trong Agent mode: chọn trong danh sách tools, hoặc gõ `#readRequirement`, `#writeTestcases`... Các file `.github/agents/*.agent.md` cho phép chọn agent (analyze, impact, testcase...) trong dropdown của Copilot Chat. Agent `code` ở mode này còn có thêm tool sửa file và chạy lệnh có sẵn của Copilot.

> Nếu tool trong `.agent.md` không được nhận (tên tool tham chiếu khác nhau giữa các bản VS Code), mở file và chọn lại bằng nút *Configure Tools*.

## 4. Agents

| Lệnh | Agent | Output |
|---|---|---|
| (none) | Orchestrator | Trạng thái ticket, bước tiếp theo |
| `/analyze` | Requirement Analyst | `01_analysis.md`, câu hỏi QA |
| `/impact` | Impact Analyzer | `02_impact.md` |
| `/solution` | Solution Designer | `03_solution.md` |
| `/code` | Developer | Code + `04_code_changes.md` |
| `/testcase` | Test Designer | `testcases_<ticket>.xlsx` |
| `/regression` | Regression Manager | Regression case mới trong file regression, `06_regression.md` |
| `/review` | Reviewer | `07_review.md` (traceability matrix) |

## 5. Skills

`requirement-reading`, `impact-analysis`, `solution-writing`, `coding-standards`, `testcase-writing`, `regression-update`, `traceability`

Mỗi agent tự nạp sẵn skill khai báo trong front-matter (`skills:`). Các skill khác agent nạp khi cần bằng tool `devflow_loadSkill`. Để tuỳ chỉnh, sửa file trong `.devflow/skills/<name>/SKILL.md`. Nên làm việc này trước tiên với **coding-standards** và **testcase-writing**.

## 6. Tools

| Tool | Chức năng |
|---|---|
| `devflow_readRequirement` | Đọc 4 sheet requirement thành Markdown (có số dòng) |
| `devflow_readExcel` | Đọc Excel bất kỳ |
| `devflow_appendQaQuestions` | Ghi câu hỏi vào sheet QA (bản copy) |
| `devflow_findScreens` | Tra cứu theo mã màn hình, đường dẫn code hoặc từ khoá |
| `devflow_getScreenSpec` | Đọc spec màn hình (.md / .xlsx) |
| `devflow_getRegressionTests` | Đọc regression test hiện có của màn hình |
| `devflow_describeTestcaseTemplate` | Field, header, cột bắt buộc, dropdown, format ID |
| `devflow_writeTestcases` | Điền test case vào bản copy của template |
| `devflow_appendRegressionTests` | Bổ sung regression case (có backup, chống trùng) |
| `devflow_validateTestcases` | Kiểm tra cột bắt buộc, dropdown, ID trùng |
| `devflow_searchCode` / `devflow_listFiles` / `devflow_readFile` | Tìm kiếm và đọc code |
| `devflow_gitDiff` | Xem thay đổi |
| `devflow_writeWorkFile` / `devflow_readWorkFile` | Lưu và đọc deliverable của ticket |
| `devflow_createFile` / `devflow_replaceInFile` | Sửa code (cần xác nhận) |
| `devflow_loadSkill` | Nạp skill |

## 7. Giới hạn cần biết

- Chỉ hỗ trợ `.xlsx`. File `.xls` cần lưu lại thành `.xlsx`. File `.xlsm` đọc được, nhưng khi ghi sẽ mất macro.
- Ghi Excel bằng thư viện ExcelJS: giữ được style, merge, dropdown, công thức. **Hình ảnh, shape và chart trong template có thể bị mất** ở file output. Template gốc không bao giờ bị sửa. Với file regression (được sửa trực tiếp), hãy kiểm tra bản backup ở lần chạy đầu.
- Tool chỉ đọc được text. Nếu ASIS/TOBE chủ yếu là ảnh chụp màn hình, cần mô tả bằng text hoặc đính kèm ảnh vào chat.
- Tool ghi file bị chặn với đường dẫn nằm ngoài workspace.

## 8. Phát triển

```
src/core/      logic thuần (Excel, screen index, agent/skill loader, prompt), có unit test
src/vscode/    tools, chat participant (vòng lặp tool-calling), commands
resources/     agents/*.md, skills/*/SKILL.md mặc định
```

`npm run watch` rồi nhấn F5 để chạy Extension Development Host.
