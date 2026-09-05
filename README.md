# Bot Telegram bán tài khoản qua API website

Bot hoạt động độc lập với tài khoản đăng nhập website. Khách dùng Telegram ID để có ví riêng, xem các sản phẩm đang liên kết nguồn API trên web, nạp tiền bằng VietQR/SePay và nhận tài khoản tự động.

API key của các nhà cung cấp không nằm trong bot. Việc kiểm tra tồn kho, trừ tiền và mua từ nguồn vẫn chạy ở Netlify Function của website.

## Chuẩn bị backend website

Đã bổ sung các endpoint cho bot trong `web_test/netlify/functions/api.js`:

- `GET /api/user/catalog` — danh mục sản phẩm provider.
- `GET /api/user/balance` — số dư gắn với Telegram ID.
- `GET /api/user/orders` — lịch sử đơn tóm tắt.
- `POST /api/telegram/deposit` — tạo yêu cầu nạp và QR.
- `GET /api/telegram/deposit/:memo` — kiểm tra trạng thái nạp.
- `POST /api/telegram/deposit/confirm` — SePay xác nhận giao dịch, chống cộng tiền trùng.
- `POST /api/provider/checkout` — kiểm tra tồn kho, trừ số dư, mua từ nguồn API.
- `GET /api/provider/order/:id` — lấy thông tin tài khoản đã giao đúng chủ Telegram.

Đặt thêm `TELEGRAM_BOT_SHARED_SECRET` trên Netlify, cùng giá trị với bot, dài tối thiểu 32 ký tự. Kiểm tra `PROVIDER_MASTER_KEY`, `FIREBASE_DATABASE_URL`, `FIREBASE_SECRET` và các API key provider vẫn đang được cấu hình trên Netlify.

## Chạy local

PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
# Mở file .env và điền token, Telegram ID admin, URL API và shared secret.
python bot.py
```

`BOT_TOKEN` là token của bot lấy từ BotFather. `ADMIN_TELEGRAM_ID` chỉ dùng để bot gửi thông báo cho admin, không phải mật khẩu hay API key. Có thể lấy ID cá nhân bằng `@userinfobot`.

Sau khi chạy, cấu hình webhook SePay trỏ đến `https://ten-may-chu-cua-ban/sepay/webhook` của bot mới. File `BOT OTP/test_otp.py` đã được chỉnh để bỏ qua các yêu cầu có `source: telegram`, tránh hai worker cùng cộng tiền; webhook cũ vẫn tiếp tục xử lý yêu cầu nạp của website.

Luồng khách: `/start` → `📦 Sản phẩm` → chọn tài khoản → nếu thiếu tiền chọn `💳 Nạp tiền` → quét QR và chuyển khoản đúng nội dung → SePay tự cộng ví → mua hàng.

## Deploy worker

Có thể chạy như một worker polling với `Procfile` ở trên. Vì bot đồng thời nhận webhook SePay, nền tảng deploy phải cho phép mở HTTP port và chuyển tiếp HTTPS đến port đó. Đặt `BOT_DATA_DIR` vào volume bền vững nếu muốn giữ thống kê Telegram cục bộ.

Chỉ sử dụng bot để phân phối những tài khoản bạn có quyền bán/phân phối và tuân thủ điều khoản của nguồn API cũng như nền tảng liên quan.
