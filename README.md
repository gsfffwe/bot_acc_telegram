# Bot Telegram bán tài khoản

Bot hiển thị các sản phẩm đang mở bán trên website, cho khách nạp tiền bằng QR và tự động nhận tài khoản sau khi thanh toán thành công.

Dữ liệu Telegram được lưu riêng trên Firebase dưới nhánh `telegramBot/`:

- `telegramBot/users` — ví và thông tin hoạt động của khách Telegram.
- `telegramBot/deposits` — yêu cầu nạp tiền của bot.
- `telegramBot/orders` — đơn hàng và tài khoản đã cấp trong bot.

Các nhánh `users`, `orders` và `deposit_requests` của website không được dùng cho dữ liệu mới của Telegram. Giao diện website cũng lọc các bản ghi Telegram cũ còn sót lại khỏi danh sách người dùng, đơn hàng và duyệt nạp tiền.

## Cấu hình website

Trong biến môi trường Production của Netlify, giữ nguyên các biến đang có và thêm:

```text
TELEGRAM_BOT_SHARED_SECRET=chuoi-bi-mat-it-nhat-32-ky-tu
```

Sau khi đổi biến môi trường, cần deploy lại website để Netlify Function nhận giá trị mới.

Mỗi yêu cầu nạp tiền tạo một nội dung chuyển khoản riêng theo dạng `Chuyentien_12345` (5 chữ số). Bot sẽ tự nhận diện nội dung này từ webhook thanh toán và cộng đúng vào ví Telegram tương ứng.

## Cấu hình bot

Tạo file `.env` từ `.env.example` trong cùng thư mục với `bot.py`:

```env
BOT_TOKEN=token-lay-tu-BotFather
ADMIN_TELEGRAM_ID=123456789
WEB_API_BASE_URL=https://ten-mien-website-cua-ban/api
TELEGRAM_BOT_SHARED_SECRET=chuoi-giong-tren-Netlify-it-nhat-32-ky-tu
PORT=8000
```

`BOT_TOKEN` và secret không được đưa lên GitHub hoặc gửi công khai. Nếu token đã xuất hiện trong ảnh/chat, hãy thu hồi và tạo token mới trong BotFather.

## Chạy local

Mở CMD tại thư mục bot và chạy:

```cmd
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe bot.py
```

Sau đó gửi `/start` cho bot. Khi chạy local, bot nhận tin nhắn Telegram nhưng webhook SePay không thể gọi vào `localhost` nếu chưa có địa chỉ public.

## Quản trị

Admin dùng đúng số Telegram ID (không phải username) trong `ADMIN_TELEGRAM_ID` rồi gửi `/admin` để xem. Bot cũng hỗ trợ thêm nhiều ID bằng `ADMIN_TELEGRAM_IDS`, ngăn cách bằng dấu phẩy:

- số khách Telegram;
- tổng đơn, đơn hoàn tất và doanh số;
- lượt nạp, tổng tiền đã nạp và số dư khách;
- danh sách khách hoạt động gần đây.

Mỗi đơn hoàn tất được gửi cho khách và bot đồng thời gửi đầy đủ thông tin đơn cùng tài khoản đã cấp cho admin.

Bot hiển thị thông tin hỗ trợ tại `@tai_khoan_xin` (có thể đổi bằng biến `SUPPORT_TELEGRAM`).

## Giá riêng cho Telegram

Trong trang quản trị website, mỗi sản phẩm có thêm trường **Giá riêng trên Telegram (VNĐ)**.
Bỏ trống trường này để bot dùng giá web. Khi có giá riêng, bot Telegram dùng giá đó cả lúc hiển thị và lúc trừ số dư; giá web và tồn kho vẫn giữ độc lập.

## Mã đơn hàng

Đơn mua từ bot dùng mã ngắn dạng `DH-XXXXXXXX`, không chứa Telegram ID của khách.

## Deploy worker

Bot chạy polling nên cần deploy như một worker/service bằng `Procfile`. Vì bot có thêm endpoint `/sepay/webhook`, nền tảng deploy cần cấp HTTPS public URL. Cấu hình webhook SePay tới:

```text
https://dia-chi-public-cua-bot/sepay/webhook
```

Có thể mở đường dẫn trên bằng trình duyệt để kiểm tra bot đã chạy bản hỗ trợ mã `Chuyentien_12345` hay chưa. Kết quả cần có `memoFormat` và `acceptsLegacyMemo`.

Nếu dùng chung một tài khoản ngân hàng cho bot bán tài khoản và bot OTP, SePay chỉ cần trỏ tới một URL. Trên bot nhận webhook chính, đặt `SEPAY_FORWARD_URL` bằng URL public của bot còn lại. Hai bot phải dùng cùng `SEPAY_WEBHOOK_TOKEN` nếu biến này được bật. Mỗi bot chỉ xử lý đúng loại đơn của mình, giao dịch chưa khớp sẽ được chuyển tiếp một lần.

Chỉ dùng bot để phân phối tài khoản mà bạn có quyền bán hoặc phân phối.
