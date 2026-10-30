# SHOP HOANG QUAN — Telegram Mini App

## Có gì trong gói này
- Telegram Mini App giao diện mobile tông xanh đen/neon như ảnh mẫu.
- Xác thực `Telegram.WebApp.initData` phía máy chủ bằng bot token; không tin `user_id` do client tự gửi.
- Đọc danh mục, sản phẩm, số dư, lịch sử, top nạp từ SQLite hiện có.
- Mua sản phẩm bằng số dư với transaction SQLite, khóa ghi và kiểm tra tồn kho/số dư trước khi tạo đơn.
- Trang nạp hiển thị đúng thông tin ngân hàng cấu hình và link VietQR theo user ID; hệ thống chỉ cộng tiền sau khi bank poller hiện có ghi nhận giao dịch.
- Nút mở Mini App trong menu `/start` chỉ hoạt động sau khi đặt `SHOP_MINIAPP_URL` thành URL HTTPS công khai.

## Cấu hình
1. Tạo bot bằng @BotFather và lấy token. Không gửi token cho người khác.
2. Tạo file cấu hình `tele_shop_config.json` cạnh `bot_app.py` theo mẫu:

```json
{
  "admin_id": 123456789,
  "bot_token": "YOUR_BOT_TOKEN",
  "log_group_id": 0,
  "vietqr_bank_name": "MB Bank",
  "vietqr_bank_id": "MB",
  "vietqr_stk": "YOUR_BANK_ACCOUNT",
  "vietqr_ctk": "YOUR_ACCOUNT_NAME",
  "vietqr_template": "compact2",
  "bank_v3_name": "MB Bank",
  "bank_v3_token": "YOUR_BANK_HISTORY_API_TOKEN",
  "poll_interval": 15,
  "auto_bank_api_text": ""
}
```

Hoặc đặt `BOT_TOKEN` và `ADMIN_ID` trong biến môi trường; `BOT_TOKEN` sẽ được dùng nếu config chưa có token.

3. Chọn máy chủ có HTTPS và **persistent disk/volume**. SQLite phải nằm trên ổ bền vững; nếu không, dữ liệu shop có thể mất khi host restart/redeploy. Đặt `SHOP_DB_FILE=/var/data/tele_shop.sqlite3` và mount volume tại `/var/data`.
4. Đặt `SHOP_MINIAPP_URL=https://TEN-MIEN-CUA-BAN/miniapp` sau khi deploy.
5. Chạy container. Dịch vụ sẽ phục vụ dashboard `/`, Mini App `/miniapp`, và bot polling nếu bật bằng dashboard.
6. Trong @BotFather, vào Bot Settings → Menu Button → Configure Menu Button; đặt URL HTTPS của `/miniapp` để có nút menu. Nút inline Mini App cũng xuất hiện trong `/start` khi `SHOP_MINIAPP_URL` hợp lệ.

## Lưu ý vận hành
- Không commit `tele_shop_config.json`, `.env`, bot token, bank token hoặc database lên GitHub.
- Chỉ test mua hàng với sản phẩm thử/giá nhỏ trước khi mở bán.
- Nạp tiền không được xác nhận từ việc mở trang QR; chỉ bank poller của shop ghi nhận chuyển khoản mới cộng số dư.
- Mẫu giao diện hiện hỗ trợ danh mục, hàng hoá, mua bằng số dư, profile, lịch sử, top nạp và hướng dẫn nạp. Các luồng nghiệp vụ bổ sung như voucher/checkout nhiều bước cần tích hợp tiếp nếu shop đang dùng.

## Bảo mật và deploy
- `/miniapp`, `/api/miniapp/*`, `/health` là các endpoint công khai; API yêu cầu `X-Telegram-Init-Data` hợp lệ được HMAC xác thực bằng bot token và giới hạn tuổi dữ liệu 24 giờ. Cách xác thực tuân theo hướng dẫn Telegram Mini Apps. (https://core.telegram.org/bots/webapps)
- Dashboard quản trị `/` và các route quản trị dùng HTTP Basic Auth. Luôn đặt `DASHBOARD_USER` và `DASHBOARD_PASSWORD` riêng, mạnh.
- Có sẵn `Dockerfile`, `render.yaml` và `DEPLOY_RENDER.md`. Render blueprint dùng gói trả phí có persistent disk để không mất SQLite. Render Free filesystem không bền vững, và web service free spin-down sau 15 phút không có traffic; không dùng cho shop đang xử lý giao dịch.
