---
name: coding-standards
description: Quy tắc code của dự án (TÙY CHỈNH file này trong .devflow/skills/coding-standards/SKILL.md cho dự án của bạn).
---
> File mẫu. Hãy thay bằng coding convention thật của dự án.

- Làm theo convention của file xung quanh: đặt tên, indent, cách xử lý exception, cách log.
- Message hiển thị lấy từ file message/resource, không hard-code.
- Validation phải có ở cả client và server.
- Không đổi format/thứ tự import của dòng không liên quan (giữ diff nhỏ).
- Thêm/sửa unit test cho logic mới.
- Comment chỉ khi giải thích "tại sao"; có thể ghi mã ticket ở comment theo quy định dự án.
- SQL: dùng parameter binding, không nối chuỗi.
