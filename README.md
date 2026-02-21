# 🏢 會議室預約系統

企業級會議室線上預約管理系統，支援多種會議室類型、照片上傳、前台文字自訂，以及完整的後台管理功能。

---

## ✨ 功能特色

- **六種會議室類型**：腦力激盪、洽談室、簡報廳、視訊會議、行政套房、培訓教室
- **即時時段衝突偵測**：自動標示已預約時段，防止重複預約
- **後台照片上傳**：拖曳或點擊上傳，照片即時同步至前台
- **前台文字自訂**：所有說明文字、聯絡資訊、注意事項皆可從後台編輯
- **預約查詢**：使用者可透過預約編號＋手機號碼查詢預約狀態
- **完整管理後台**：預約管理、會議室管理、統計儀表板

---

## 🚀 快速啟動

### 1. 安裝套件

```bash
pip install -r requirements.txt
```

### 2. 啟動伺服器

```bash
python app.py
```

### 3. 開啟瀏覽器

| 頁面 | 網址 |
|------|------|
| 前台預約 | http://localhost:5000 |
| 管理員登入 | http://localhost:5000/admin |
| 管理後台 | http://localhost:5000/dashboard |

**預設管理員密碼：** `admin123`

---

## 📁 檔案結構

```
meeting_room_system/
├── app.py                      # Flask 後端主程式
├── requirements.txt            # Python 套件清單
├── README.md                   # 本說明文件
└── static/
    ├── index.html              # 前台預約頁面
    ├── admin_login.html        # 管理員登入頁
    ├── admin_dashboard.html    # 管理後台儀表板
    └── uploads/                # 上傳照片存放目錄（自動建立）
```

資料庫檔案 `meeting_rooms.db` 會在首次啟動時自動建立於專案根目錄。

---

## 🏛 預設會議室

系統初始化時自動建立以下六間會議室：

| 會議室名稱 | 類型 | 容量 | 時薪（NT$） | 樓層 |
|------------|------|------|------------|------|
| 創意腦力激盪室 | 腦力激盪 | 8 人 | 600 | 3F |
| 精緻洽談室 A | 洽談室 | 4 人 | 400 | 2F |
| 大型簡報廳 | 簡報廳 | 50 人 | 2,000 | 1F |
| 視訊會議中心 | 視訊會議 | 12 人 | 1,000 | 4F |
| 主管行政套房 | 行政套房 | 6 人 | 1,500 | 12F |
| 多功能培訓教室 | 培訓教室 | 30 人 | 1,200 | 5F |

---

## 🖥 管理後台功能

### 儀表板
- 統計卡片：有效預約數、今日預約、可用會議室、累計營收、取消數、完成數
- 最新預約列表

### 📅 預約管理
- 依日期、狀態、會議室篩選
- 將預約標記為「已完成」或「已取消」

### 🏛 會議室管理
- 新增／編輯會議室（名稱、類型、容量、費率、樓層、說明、設施）
- 上傳會議室封面照片
- 啟用／停用會議室

### 📷 照片管理
- 集中管理所有會議室的照片
- 支援拖曳上傳
- 支援格式：JPG、PNG、GIF、WEBP（最大 16MB）
- 照片上傳後即時同步至前台

### ✏️ 文字內容編輯
可編輯以下前台顯示文字，儲存後立即生效：

| 欄位 | 說明 |
|------|------|
| 網站標題 | 瀏覽器標籤與頁首名稱 |
| Hero 標語標籤 | 頁首徽章文字 |
| 頁面描述 | 主視覺區說明文字 |
| 服務時間 | 顯示於頁首資訊欄 |
| 聯絡電話 | 顯示於頁首右上角 |
| 聯絡 Email | 顯示於頁首資訊欄 |
| 須知 1–5 | 顯示於預約側欄注意事項 |
| 頁尾文字 | 網站底部版權文字 |

---

## 🔌 API 端點

### 公開 API

| 方法 | 路徑 | 說明 |
|------|------|------|
| GET | `/` | 前台預約頁面 |
| GET | `/api/site-content` | 取得所有前台文字設定 |
| GET | `/api/rooms` | 取得所有啟用中的會議室 |
| GET | `/api/rooms/:id/availability?date=YYYY-MM-DD` | 查詢指定日期的已預約時段 |
| POST | `/api/book` | 建立預約 |
| GET | `/api/bookings/check?number=&phone=` | 查詢預約狀態 |

### 管理 API（需 Header：`X-Admin-Password`）

| 方法 | 路徑 | 說明 |
|------|------|------|
| POST | `/admin/api/login` | 管理員登入 |
| GET | `/admin/api/stats` | 統計數據 |
| GET | `/admin/api/bookings` | 查看預約（支援篩選） |
| POST | `/admin/api/bookings/:id/cancel` | 取消預約 |
| POST | `/admin/api/bookings/:id/complete` | 完成預約 |
| GET | `/admin/api/rooms` | 取得所有會議室 |
| POST | `/admin/api/rooms` | 新增會議室 |
| PUT | `/admin/api/rooms/:id` | 更新會議室 |
| POST | `/admin/api/upload-photo` | 上傳照片（multipart/form-data） |
| GET | `/admin/api/site-content` | 取得前台文字設定 |
| POST | `/admin/api/site-content` | 更新前台文字設定 |

---

## 🗄 資料庫結構

### Room（會議室）
| 欄位 | 型別 | 說明 |
|------|------|------|
| id | Integer | 主鍵 |
| name | String | 會議室名稱 |
| room_type | String | 類型（六種） |
| capacity | Integer | 容納人數 |
| hourly_rate | Integer | 每小時費率（NT$） |
| description | Text | 說明文字 |
| amenities | Text | 設施清單（JSON 陣列） |
| photo_url | String | 照片路徑 |
| is_active | Boolean | 是否啟用 |
| floor | String | 樓層 |

### Booking（預約記錄）
| 欄位 | 型別 | 說明 |
|------|------|------|
| booking_number | String | 預約編號（MR + 日期 + 序號） |
| room_id | Integer | 關聯會議室 |
| customer_name | String | 聯絡人姓名 |
| customer_phone | String | 手機號碼 |
| customer_email | String | Email |
| department | String | 部門／公司 |
| date | String | 預約日期（YYYY-MM-DD） |
| start_time | String | 開始時間（HH:MM） |
| end_time | String | 結束時間（HH:MM） |
| duration | Float | 使用時長（小時） |
| total_price | Integer | 總費用（NT$） |
| attendees | Integer | 出席人數 |
| purpose | Text | 會議類型 |
| status | String | confirmed / cancelled / completed |

### SiteContent（前台設定）
| 欄位 | 型別 | 說明 |
|------|------|------|
| key | String | 設定名稱（唯一） |
| value | Text | 設定內容 |

---

## ☁️ 部署至 Render

### 1. 上傳至 GitHub

```bash
git init
git add .
git commit -m "初始化會議室預約系統"
git push origin main
```

### 2. Render 設定

- **Build Command：** `pip install -r requirements.txt`
- **Start Command：** `gunicorn app:app --bind 0.0.0.0:$PORT`

### 3. 環境變數

| 變數名稱 | 說明 | 預設值 |
|----------|------|--------|
| `ADMIN_PASSWORD` | 管理員密碼 | `admin123` |
| `SECRET_KEY` | Flask Session 金鑰 | `meeting-room-booking-2026` |

> **注意：** Render 免費方案的磁碟為暫存性，重新部署後上傳的照片會消失。建議搭配 Cloudinary 或 AWS S3 儲存照片。

---

## ⚙️ 環境變數

```bash
export ADMIN_PASSWORD="your-secure-password"
export SECRET_KEY="your-random-secret-key"
```

---

## 🛠 技術堆疊

| 層級 | 技術 |
|------|------|
| 後端框架 | Python 3.x + Flask |
| 資料庫 | SQLite + SQLAlchemy ORM |
| 跨域支援 | Flask-CORS |
| 檔案上傳 | Werkzeug |
| 前端 | 原生 HTML5 + CSS3 + JavaScript（無框架） |
| 字型 | Google Fonts（DM Serif Display + DM Sans） |
| 部署 | Gunicorn + Render |

---

## 📋 注意事項

- 預設服務時間為每日 08:00–22:00，時段以 30 分鐘為單位
- 費用計算：（結束時間 − 開始時間）× 時薪
- 照片上傳後儲存於 `static/uploads/` 目錄
- 管理員密碼以明文比對，正式環境建議改為雜湊驗證
- SQLite 適合中小型使用；高併發場景建議改用 PostgreSQL

---

## 📄 授權

本專案供內部使用，版權所有。