# Linux 主機部署指南（PostgreSQL 版）

## 架構概覽

```
Internet → Nginx（反向代理 + SSL）→ Gunicorn（WSGI）→ Flask app → PostgreSQL
```

---

## 環境需求

- Ubuntu 20.04 / 22.04 LTS（推薦）
- Python 3.10+
- PostgreSQL 14+
- 已申請網域並指向主機 IP（LINE Webhook 必須使用 HTTPS）

---

## 第一步：安裝系統套件

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3 python3-pip python3-venv nginx git certbot python3-certbot-nginx -y

# 安裝 PostgreSQL
sudo apt install postgresql postgresql-contrib -y
```

---

## 第二步：建立 PostgreSQL 資料庫與使用者

```bash
# 切換到 postgres 系統使用者
sudo -u postgres psql
```

進入 psql 後執行（請自行替換密碼）：

```sql
-- 建立資料庫使用者
CREATE USER seat_booking_user WITH PASSWORD '請設定強密碼';

-- 建立資料庫
CREATE DATABASE seat_booking_db OWNER seat_booking_user;

-- 授予權限
GRANT ALL PRIVILEGES ON DATABASE seat_booking_db TO seat_booking_user;

-- 離開
\q
```

測試連線是否正常：

```bash
psql -U seat_booking_user -d seat_booking_db -h localhost
# 輸入密碼後能進入即表示成功，輸入 \q 離開
```

---

## 第三步：部署程式碼

```bash
cd /var/www
sudo git clone https://github.com/你的帳號/seat_booking.git
sudo chown -R $USER:$USER /var/www/seat_booking
cd /var/www/seat_booking

# 建立虛擬環境並安裝依賴
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
pip install gunicorn
```

確認 `psycopg[binary]` 已安裝（requirements.txt 已包含）：

```bash
pip show psycopg
```

---

## 第四步：設定環境變數

建立專屬環境變數檔案（比 `/etc/environment` 更安全）：

```bash
sudo nano /etc/seat_booking.env
```

填入以下內容（參照 Render 上的設定）：

```env
SECRET_KEY=請設定一組隨機長字串
DATABASE_URL=postgresql+psycopg://seat_booking_user:你的密碼@localhost:5432/seat_booking_db
LINE_CHANNEL_SECRET=你的LINE_CHANNEL_SECRET
LINE_CHANNEL_ACCESS_TOKEN=你的LINE_CHANNEL_ACCESS_TOKEN
LIFF_ID=你的LIFF_ID
SITE_URL=https://你的網域
GMAIL_USER=exo881222@gmail.com
GMAIL_APP_PASS=你的App_Password
OPENAI_API_KEY=你的OPENAI_API_KEY
```

設定檔案權限（只有 root 能讀）：

```bash
sudo chmod 600 /etc/seat_booking.env
```

---

## 第五步：設定 Systemd 服務

```bash
sudo nano /etc/systemd/system/seat_booking.service
```

```ini
[Unit]
Description=Seat Booking Flask App
After=network.target postgresql.service

[Service]
User=www-data
Group=www-data
WorkingDirectory=/var/www/seat_booking
EnvironmentFile=/etc/seat_booking.env
ExecStart=/var/www/seat_booking/venv/bin/gunicorn \
    --workers 2 \
    --bind 127.0.0.1:8000 \
    --timeout 120 \
    --access-logfile /var/log/seat_booking/access.log \
    --error-logfile /var/log/seat_booking/error.log \
    app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

建立 log 目錄並設定權限：

```bash
sudo mkdir -p /var/log/seat_booking
sudo chown www-data:www-data /var/log/seat_booking
sudo chown -R www-data:www-data /var/www/seat_booking
```

啟動服務：

```bash
sudo systemctl daemon-reload
sudo systemctl enable seat_booking
sudo systemctl start seat_booking
sudo systemctl status seat_booking
```

出現 `Active: active (running)` 即表示成功。

---

## 第六步：設定 Nginx

```bash
sudo nano /etc/nginx/sites-available/seat_booking
```

```nginx
server {
    listen 80;
    server_name 你的網域;

    # 靜態檔案直接由 Nginx 處理（效能較好）
    location /static/ {
        alias /var/www/seat_booking/static/;
        expires 7d;
    }

    # 其他請求轉發給 Gunicorn
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

啟用設定：

```bash
sudo ln -s /etc/nginx/sites-available/seat_booking /etc/nginx/sites-enabled/
sudo nginx -t        # 測試設定語法
sudo systemctl restart nginx
```

---

## 第七步：申請 SSL 憑證（HTTPS）

LINE Webhook 必須使用 HTTPS，請確認網域已正確指向主機 IP 後執行：

```bash
sudo certbot --nginx -d 你的網域
```

Certbot 會自動修改 Nginx 設定並加入 HTTPS。憑證 90 天自動續期，可測試：

```bash
sudo certbot renew --dry-run
```

---

## 第八步：資料庫初始化

app 啟動時會自動執行 `db.create_all()` 建立所有資料表，確認正常：

```bash
sudo journalctl -u seat_booking -n 50
```

若需要從 Render 的 PostgreSQL 匯出資料並移入：

```bash
# 在 Render 主機或本機執行（匯出）
pg_dump postgresql://使用者:密碼@render的host/資料庫名稱 > backup.sql

# 在新主機匯入
psql -U seat_booking_user -d seat_booking_db -h localhost < backup.sql
```

---

## 常用維運指令

| 動作 | 指令 |
|---|---|
| 查看即時 log | `sudo journalctl -u seat_booking -f` |
| 重啟 app | `sudo systemctl restart seat_booking` |
| 更新程式碼 | `cd /var/www/seat_booking && sudo git pull && sudo chown -R www-data:www-data . && sudo systemctl restart seat_booking` |
| 查看 app log | `sudo tail -f /var/log/seat_booking/error.log` |
| 查看 Nginx log | `sudo tail -f /var/log/nginx/access.log` |
| 進入資料庫 | `sudo -u postgres psql -d seat_booking_db` |
| 備份資料庫 | `pg_dump -U seat_booking_user -d seat_booking_db -h localhost > backup_$(date +%Y%m%d).sql` |
| 還原資料庫 | `psql -U seat_booking_user -d seat_booking_db -h localhost < backup.sql` |

---

## 防火牆設定（ufw）

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
sudo ufw enable
sudo ufw status
```

---

## 常見問題排查

**app 啟動失敗**
```bash
sudo journalctl -u seat_booking -n 100 --no-pager
```

**Nginx 502 Bad Gateway**
```bash
# 確認 Gunicorn 是否在跑
sudo systemctl status seat_booking
# 確認 port 8000 有在監聽
sudo ss -tlnp | grep 8000
```

**PostgreSQL 連線失敗**
```bash
# 確認 PostgreSQL 服務狀態
sudo systemctl status postgresql
# 測試連線
psql -U seat_booking_user -d seat_booking_db -h localhost
```

**資料庫權限錯誤**
```bash
sudo -u postgres psql
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO seat_booking_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO seat_booking_user;
\q
```
