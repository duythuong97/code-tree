---
name: testcase-writing
description: Kỹ thuật thiết kế test case (normal/abnormal/boundary, equivalence, decision table), cách viết steps/expected và field JSON.
---
## Độ phủ tối thiểu cho mỗi thay đổi
- Normal: luồng chính thành công.
- Abnormal: mỗi validation / mỗi message lỗi có ít nhất 1 case.
- Boundary: min, max, min-1, max+1 cho độ dài và giá trị số/ngày.
- Equivalence: mỗi nhóm giá trị (vd: trạng thái A/B/C) 1 case.
- Decision table khi có nhiều điều kiện kết hợp (role × trạng thái × flag).
- Hiển thị: giá trị ban đầu, sau khi lưu, sau khi reload, khi dữ liệu cũ có NULL.
- Phân quyền: role được phép / không được phép.
- Không đổi (unchanged): 1-2 case xác nhận phần ASIS không bị ảnh hưởng.

## Cách viết
- title: "<Chức năng> - <điều kiện> - <kết quả mong muốn>", ngắn và duy nhất.
- precondition: dữ liệu, user/role, màn hình bắt đầu.
- steps: mảng, mỗi phần tử là 1 hành động ("Nhập 'abc' vào field Tên khách hàng").
- testData: giá trị cụ thể, không ghi "dữ liệu hợp lệ".
- expected: kiểm chứng được: message ID/nội dung chính xác, giá trị hiển thị, dữ liệu trong DB, trạng thái nút.
- reqRef: Req ID trong sheet Requirement (hoặc "TOBE row N").
- category / priority: chỉ dùng giá trị được phép.
- Để trống id: tool tự sinh theo idFormat.

## Ví dụ case object
```json
{
  "screenId": "SCR001",
  "reqRef": "REQ-03",
  "category": "Abnormal",
  "title": "Tìm kiếm - SĐT 12 ký tự - hiển thị lỗi độ dài",
  "precondition": "Đăng nhập role Operator, mở màn hình SCR001",
  "steps": ["Nhập '090123456789' vào Số điện thoại", "Bấm nút Tìm kiếm"],
  "testData": "SĐT = 090123456789 (12 ký tự)",
  "expected": "Hiển thị message E0012 'Số điện thoại tối đa 11 ký tự'. Không thực hiện tìm kiếm.",
  "priority": "Medium"
}
```
