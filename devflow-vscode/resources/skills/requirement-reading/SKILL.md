---
name: requirement-reading
description: Cách đọc file requirement Excel 4 sheet (Requirement, ASIS, TOBE, QA) và cách viết câu hỏi QA tốt.
---
## Cấu trúc file requirement
- **Requirement**: danh sách yêu cầu (ID, màn hình, mô tả, độ ưu tiên...). Là nguồn chính để đánh reqRef.
- **ASIS**: hành vi/màn hình hiện tại. Thường có ảnh chụp: tool chỉ đọc được text, nếu nội dung chính nằm trong ảnh hãy báo người dùng.
- **TOBE**: hành vi mong muốn sau thay đổi. Mọi khác biệt ASIS→TOBE đều phải có trong solution và test case.
- **QA**: câu hỏi và trả lời giữa dev và BA. Câu trả lời trong QA có giá trị như requirement và ghi đè mô tả cũ nếu mâu thuẫn.

## Cách đọc
- Đọc theo số dòng Excel (cột Row) để trích dẫn: "TOBE dòng 12".
- Ô merge chỉ hiện ở ô đầu, các ô còn lại để trống: không phải dữ liệu thiếu.
- Chú ý các từ: "giống như", "tương tự màn hình X", "hiện tại" → cần đọc spec màn hình liên quan.
- Với mỗi field trên màn hình, kiểm tra đủ: kiểu dữ liệu, độ dài, bắt buộc, giá trị mặc định, format, message lỗi, quyền.

## Câu hỏi QA tốt
- Một câu hỏi = một điểm. Nêu vị trí (sheet/dòng hoặc màn hình/field).
- Đưa ra phương án đề xuất để BA chỉ cần chọn: "Đề xuất: A. ... / B. ...".
- Ghi rủi ro nếu giả định sai.
- Không hỏi những gì đã có câu trả lời trong sheet QA hoặc spec.
- Field cho devflow_appendQaQuestions mặc định: screenId, category (Spec/Business/UI/Data/Other), question, raisedBy.
