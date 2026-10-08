---
name: impact-analysis
description: Checklist và kỹ thuật tìm impact (UI, logic, DB, API, batch, report, phân quyền, dữ liệu cũ) có bằng chứng.
---
## Kỹ thuật tìm kiếm
1. Bắt đầu từ màn hình thay đổi → code globs trong screen index.
2. Từ mỗi "điểm thay đổi" (field, cột DB, API, method, message ID, constant, enum), devflow_searchCode để tìm mọi nơi sử dụng.
3. Mỗi file tìm thấy → devflow_findScreens(codePath) để quy về màn hình. File không thuộc màn hình nào → có thể là batch/common/API: ghi riêng.
4. Lặp lại 1 tầng cho component dùng chung (common service, util, base class, shared JS/CSS, include JSP/partial view).

## Checklist
- [ ] UI: layout, field mới/xoá, thứ tự tab, độ rộng, responsive, label, i18n/message file
- [ ] Validation: client-side và server-side đều phải sửa
- [ ] Business logic: tính toán, trạng thái, điều kiện rẽ nhánh
- [ ] DB: cột/bảng mới, index, constraint, giá trị mặc định, dữ liệu hiện có (NULL?), migration script
- [ ] Query: SELECT * / mapping ORM / DTO, các query khác cùng bảng
- [ ] API/Interface: request/response, hệ thống ngoài, backward compatibility
- [ ] Batch/Job dùng cùng bảng hoặc logic
- [ ] Report / CSV / PDF export / import
- [ ] Phân quyền, role, menu
- [ ] Log, audit, lịch sử thay đổi
- [ ] Màn hình khác dùng chung component / bảng (related trong screen index)
- [ ] Performance: query trên bảng lớn, N+1
- [ ] Unit test / test data hiện có bị ảnh hưởng

## Mức độ
- High: thay đổi logic/dữ liệu được dùng ở nơi khác; thay đổi DB; interface ngoài.
- Medium: component dùng chung nhưng hành vi không đổi.
- Low: chỉ hiển thị, label, layout cục bộ.
