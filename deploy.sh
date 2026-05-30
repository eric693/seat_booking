#!/bin/bash
# 部署腳本：seat-booking.crownai.ink
# 執行前請確認：DNS A record 已指向此伺服器 IP

set -e
DOMAIN="seat-booking.crownai.ink"
APP_DIR="/root/seat-booking/seat_booking"

echo "=== 建立 virtualenv 並安裝相依套件 ==="
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "=== 建立 uploads 目錄 ==="
mkdir -p "$APP_DIR/static/uploads"

echo "=== 複製 systemd service ==="
cp "$APP_DIR/seat-booking.service" /etc/systemd/system/seat-booking.service
systemctl daemon-reload
systemctl enable seat-booking
systemctl restart seat-booking
echo "gunicorn 狀態：$(systemctl is-active seat-booking)"

echo "=== 安裝 Nginx ==="
apt-get install -y nginx

echo "=== 先用 HTTP-only 設定取得憑證 ==="
cat > /etc/nginx/sites-available/$DOMAIN <<'NGINXTMP'
server {
    listen 80;
    server_name seat-booking.crownai.ink;
    location / { proxy_pass http://127.0.0.1:8000; }
}
NGINXTMP
ln -sf /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/$DOMAIN
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

echo "=== 取得 Let's Encrypt SSL 憑證 ==="
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d $DOMAIN --non-interactive --agree-tos -m peiyuhong0988@gmail.com

echo "=== 套用正式 Nginx 設定（HTTPS）==="
cp "$APP_DIR/seat-booking.nginx.conf" /etc/nginx/sites-available/$DOMAIN
nginx -t && systemctl reload nginx

echo ""
echo "✓ 部署完成！請訪問 https://$DOMAIN"
echo ""
echo "重要：請編輯 /etc/systemd/system/seat-booking.service"
echo "      填入正確的 DATABASE_URL、LINE_CHANNEL_ACCESS_TOKEN 等環境變數"
echo "      修改後執行：systemctl daemon-reload && systemctl restart seat-booking"
