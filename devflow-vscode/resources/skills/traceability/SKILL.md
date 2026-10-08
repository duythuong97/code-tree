---
name: traceability
description: Cách lập ma trận traceability Requirement ↔ Solution ↔ Code ↔ Test case ↔ Regression và tiêu chí review.
---
## Ma trận
| Req ID | Màn hình | Solution (mục) | Code (file) | Test case (ID) | Regression (ID) | Trạng thái |
- Trạng thái: OK / Thiếu solution / Thiếu code / Thiếu test / Thiếu regression / Cần xác nhận.

## Tiêu chí
- Mỗi Req ID có ≥1 dòng solution, ≥1 file code (trừ requirement chỉ về tài liệu), ≥1 test case normal và các case abnormal tương ứng với validation.
- Mỗi màn hình impact (trực tiếp + gián tiếp mức High/Medium) có regression được chọn.
- Không có câu hỏi QA Open mà solution đã giả định mà không ghi chú.
- File test case không có lỗi validate.

## Mức độ vấn đề
- Blocker: requirement không được hiện thực / test sai với TOBE.
- Major: thiếu test abnormal/boundary, thiếu regression cho impact High.
- Minor: diễn đạt, format, thiếu reqRef.
