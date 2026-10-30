# Deploy lên Render

## Trước khi deploy
- Đẩy toàn bộ thư mục này lên một GitHub repository riêng tư (private).
- Không tải lên token thật hoặc `tele_shop_config.json`.
- Gói `render.yaml` tạo một Docker Web Service và một persistent disk tại `/var/data`.

## Tạo dịch vụ
1. Đăng nhập Render → New → Blueprint.
2. Kết nối repository chứa `render.yaml`.
3. Review cấu hình. Blueprint dùng gói Starter và persistent disk 1 GB vì shop dùng SQLite để giữ số dư, đơn hàng, kho key và lịch sử; không chọn Free cho dữ liệu giao dịch.
4. Sau khi dịch vụ tạo xong, mở Environment và thêm:
   - `BOT_TOKEN`: token bot từ @BotFather.
   - `ADMIN_ID`: Telegram numeric user ID của admin.
   - `VIETQR_BANK_NAME`: tên ngân hàng nhận tiền (ví dụ `MB Bank`).
   - `VIETQR_BANK_ID`: mã VietQR (ví dụ `MB`).
   - `VIETQR_STK`: số tài khoản nhận tiền.
   - `VIETQR_CTK`: tên chủ tài khoản.
   - `BANK_V3_NAME` và `BANK_V3_TOKEN`: thông tin API lịch sử ngân hàng mà bot đang dùng để đối soát tự động.
5. Lấy domain Render của dịch vụ, ví dụ `https://shop-hoang-quan-miniapp.onrender.com`, rồi thêm:
   - `SHOP_MINIAPP_URL=https://shop-hoang-quan-miniapp.onrender.com/miniapp`
6. Lưu Environment và redeploy. Khi bot khởi động, code sẽ đặt menu button `Mua Key` và `/start` sẽ có nút `MỞ SHOP (MINI APP)`.
7. Truy cập `https://TEN-DICH-VU.onrender.com/miniapp` từ nút trong Telegram. Trang `/` là dashboard quản trị; đăng nhập bằng Basic Auth với `DASHBOARD_USER` và `DASHBOARD_PASSWORD` trong Render Environment.

## Kiểm tra sau deploy
- `https://TEN-DICH-VU.onrender.com/health` phải trả JSON `{"ok":true,...}`.
- Mở Mini App từ chính Telegram để có `initData`; mở URL trực tiếp bằng Safari sẽ không xác thực.
- Tạo một sản phẩm thử, nạp thử bằng số tiền nhỏ, xác minh bank poller cập nhật số dư trước khi mở bán thật.

## Quan trọng về miễn phí và dữ liệu
Render Free web services có filesystem tạm: file SQLite sẽ mất khi restart/redeploy/spin-down và dịch vụ tự sleep khi không có request. Persistent disk chỉ có trên gói trả phí. Vì shop lưu số dư và kho trong SQLite, không nên chạy shop giao dịch thật trên Render Free. Xem giới hạn hiện tại ở tài liệu Render: https://render.com/docs/free và https://render.com/docs/disks.

## Chuyển dữ liệu shop hiện tại
Gói source không chứa `tele_shop.sqlite3` hoặc `tele_shop_config.json` của bạn. Trước khi bật dịch vụ thật, hãy sao lưu database từ máy đang chạy bot và đưa file đó vào persistent disk tại `/var/data/tele_shop.sqlite3`; nếu không, server mới sẽ tạo database trống. Giữ cấu hình bank/token ở Render Environment hoặc lưu file config trong `/var/data/tele_shop_config.json`. Không gửi file chứa token qua chat công khai.
