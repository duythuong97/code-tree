---
name: regression-update
description: Quy tắc chọn regression test cần chạy, khi nào bổ sung regression case mới, khi nào sửa case cũ, tránh trùng lặp.
---
## Chọn case chạy lại
- Màn hình thay đổi trực tiếp: chạy toàn bộ case thuộc chức năng bị sửa + case smoke (mở màn hình, luồng chính).
- Màn hình ảnh hưởng gián tiếp: case liên quan tới component/dữ liệu dùng chung.
- Dùng devflow_getRegressionTests(screenId, keyword) với keyword là tên field/chức năng để lọc.

## Khi nào bổ sung regression case mới
Đưa test case của ticket vào regression lâu dài khi:
- Là hành vi mới có giá trị nghiệp vụ lâu dài (field mới, rule mới, message mới).
- Bảo vệ một bug/rủi ro đã từng xảy ra.
- Phủ phần dùng chung mà bộ regression hiện tại chưa có.
Không đưa vào: case chỉ kiểm tra tạm thời cho migration, case trùng ý nghĩa với case có sẵn.

## Khi nào sửa case cũ
- Expected cũ không còn đúng với TOBE → đề xuất sửa (không xoá), ghi ID và nội dung mới.
- Case cũ kiểm tra hành vi đã bị bỏ → đề xuất đánh dấu obsolete theo quy định của dự án.

## Tránh trùng
- So sánh ý nghĩa (điều kiện + kết quả), không chỉ so tiêu đề. Tool chỉ chặn trùng tiêu đề chính xác.
- Tiêu đề regression nên tổng quát, không ghi mã ticket; truy vết ticket bằng field sourceTicket.
