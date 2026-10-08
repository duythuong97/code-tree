---
name: solution-writing
description: Template tài liệu solution (03_solution.md) gồm phương án, thiết kế chi tiết, impact area, rủi ro, test approach.
---
## Template 03_solution.md
```
# <ticket> – Solution
## 1. Tổng quan
- Mục tiêu:
- Phạm vi: (màn hình / chức năng)
- Ngoài phạm vi:
## 2. Phương án
- Phương án chọn: ...
- Phương án khác đã cân nhắc và lý do không chọn:
## 3. Thay đổi chi tiết
### 3.1 <SCR-ID> <Tên màn hình>
| # | File | Class/Method/Element | Thay đổi | Req ID |
- Validation / message:
- Xử lý lỗi:
### 3.2 DB
- DDL / migration / dữ liệu cũ:
### 3.3 API / Batch / Khác
## 4. Impact area
| Khu vực | Ảnh hưởng | Mức độ | Biện pháp (test/regression) |
## 5. Rủi ro & giả định
| # | Rủi ro/giả định | Xác nhận bởi | Trạng thái |
## 6. Test approach
- Unit test:
- Test case chức năng (tham chiếu file test case):
- Regression: màn hình …
## 7. Ước lượng (tuỳ chọn)
```
## Nguyên tắc
- Mỗi dòng "Thay đổi chi tiết" phải truy về một Req ID.
- Tái sử dụng logic có sẵn; nêu rõ file/method được tái sử dụng.
- Ghi rõ giả định chưa được BA xác nhận (liên kết tới câu hỏi QA).
