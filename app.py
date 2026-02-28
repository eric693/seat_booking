# -*- coding: utf-8 -*-
import os
import json
import uuid
import hashlib
import hmac
import base64
import requests as http_requests
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory, session
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import func
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'meeting-room-booking-2026')

# 資料庫：優先用環境變數 DATABASE_URL（PostgreSQL），否則本地 SQLite
import sys
_default_db = 'sqlite:///meeting_rooms.db'
DATABASE_URL = os.environ.get('DATABASE_URL', _default_db)
# Render PostgreSQL URL 開頭是 postgres://，SQLAlchemy 2.x 要求 postgresql://
# 使用 psycopg3 (psycopg)，dialect 為 postgresql+psycopg
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql+psycopg://', 1)
elif DATABASE_URL.startswith('postgresql://') and '+' not in DATABASE_URL.split('://')[0]:
    DATABASE_URL = DATABASE_URL.replace('postgresql://', 'postgresql+psycopg://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = DATABASE_URL
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

CORS(app)
db = SQLAlchemy(app)

ADMIN_PASSWORD            = os.environ.get('ADMIN_PASSWORD', 'admin123')
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET       = os.environ.get('LINE_CHANNEL_SECRET', '')
LINE_PUSH_URL  = 'https://api.line.me/v2/bot/message/push'
LINE_REPLY_URL = 'https://api.line.me/v2/bot/message/reply'
SITE_URL       = os.environ.get('SITE_URL', 'https://seat-booking-rlf2.onrender.com')
LIFF_URL       = os.environ.get('LIFF_URL', 'https://liff.line.me/2009193434-BpOSKuw9')
LIFF_ID        = os.environ.get('LIFF_ID', '')

# ── Email 通知（Gmail API OAuth2 優先，SendGrid 次之）──────
SENDGRID_API_KEY     = os.environ.get('SENDGRID_API_KEY', '')
GMAIL_USER           = os.environ.get('GMAIL_USER', '')
GMAIL_APP_PASS       = os.environ.get('GMAIL_APP_PASS', '')
MAIL_FROM            = os.environ.get('MAIL_FROM', GMAIL_USER)
# Gmail API OAuth2
GOOGLE_CLIENT_ID     = os.environ.get('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET', '')
GOOGLE_REFRESH_TOKEN = os.environ.get('GOOGLE_REFRESH_TOKEN', '')
USE_GMAIL_API  = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN)
USE_SENDGRID   = bool(SENDGRID_API_KEY) and not USE_GMAIL_API
USE_GMAIL      = bool(GMAIL_USER and GMAIL_APP_PASS) and not USE_GMAIL_API and not USE_SENDGRID
USE_EMAIL      = USE_GMAIL_API or USE_SENDGRID or USE_GMAIL

# ── Twilio SMS（選用）───────────────────────────
TWILIO_SID    = os.environ.get('TWILIO_SID', '')
TWILIO_TOKEN  = os.environ.get('TWILIO_TOKEN', '')
TWILIO_FROM   = os.environ.get('TWILIO_FROM', '')       # 例：+15005550006
USE_TWILIO    = bool(TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM)

# ── Cloudinary（選用）────────────────────────
# 設定後照片上傳至 Cloudinary，Render 重啟也不會消失
# 未設定則 fallback 存本地 static/uploads/
CLOUDINARY_CLOUD_NAME = os.environ.get('CLOUDINARY_CLOUD_NAME', '')
CLOUDINARY_API_KEY    = os.environ.get('CLOUDINARY_API_KEY', '')
CLOUDINARY_API_SECRET = os.environ.get('CLOUDINARY_API_SECRET', '')
USE_CLOUDINARY = all([CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET])


# ─────────────────────────────────────────────
# LINE Helpers
# ─────────────────────────────────────────────

def _line_headers():
    return {'Content-Type': 'application/json',
            'Authorization': f'Bearer {LINE_CHANNEL_ACCESS_TOKEN}'}


def verify_line_signature(body: bytes, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET:
        return True  # 本地開發略過驗證
    digest = hmac.new(LINE_CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


def push_line(user_id: str, messages: list):
    if not LINE_CHANNEL_ACCESS_TOKEN or not user_id:
        return
    try:
        http_requests.post(LINE_PUSH_URL,
                           headers=_line_headers(),
                           json={'to': user_id, 'messages': messages},
                           timeout=10)
    except Exception as e:
        print(f'[LINE push error] {e}')


def reply_line(reply_token: str, messages: list):
    if not LINE_CHANNEL_ACCESS_TOKEN or not reply_token:
        return
    try:
        http_requests.post(LINE_REPLY_URL,
                           headers=_line_headers(),
                           json={'replyToken': reply_token, 'messages': messages},
                           timeout=10)
    except Exception as e:
        print(f'[LINE reply error] {e}')

# ─────────────────────────────────────────────
# Gmail + SMS Helpers
# ─────────────────────────────────────────────

def send_email(to_addr: str, subject: str, body_html: str):
    """寄送 HTML 信件：Gmail API > SendGrid > Gmail SMTP"""
    if not to_addr:
        return
    if USE_GMAIL_API:
        _send_via_gmail_api(to_addr, subject, body_html)
    elif USE_SENDGRID:
        _send_via_sendgrid(to_addr, subject, body_html)
    elif USE_GMAIL:
        _send_via_gmail(to_addr, subject, body_html)
    else:
        print('[Email] 未設定任何 Email 服務，略過寄信')


def _send_via_gmail_api(to_addr: str, subject: str, body_html: str):
    """透過 Gmail API（OAuth2 Refresh Token）寄信 — 100% 不進垃圾桶"""
    import base64
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    try:
        # Step 1: 用 refresh token 換 access token
        token_resp = http_requests.post(
            'https://oauth2.googleapis.com/token',
            data={
                'client_id':     GOOGLE_CLIENT_ID,
                'client_secret': GOOGLE_CLIENT_SECRET,
                'refresh_token': GOOGLE_REFRESH_TOKEN,
                'grant_type':    'refresh_token',
            },
            timeout=10
        )
        token_data = token_resp.json()
        access_token = token_data.get('access_token')
        if not access_token:
            print(f'[Gmail API] 取得 access token 失敗：{token_data}')
            return

        # Step 2: 組裝 MIME 郵件
        from_addr = MAIL_FROM or GMAIL_USER
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = from_addr
        msg['To']      = to_addr
        msg.attach(MIMEText(body_html, 'html', 'utf-8'))
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode('utf-8')

        # Step 3: 透過 Gmail API 發送
        resp = http_requests.post(
            'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',
            headers={
                'Authorization': f'Bearer {access_token}',
                'Content-Type':  'application/json',
            },
            json={'raw': raw},
            timeout=15
        )
        if resp.status_code == 200:
            print(f'[Gmail API] sent to {to_addr}')
        else:
            print(f'[Gmail API error] {resp.status_code}: {resp.text[:300]}')
    except Exception as e:
        print(f'[Gmail API error] {e}')


def _send_via_sendgrid(to_addr: str, subject: str, body_html: str):
    """透過 SendGrid API 寄信"""
    from_addr = MAIL_FROM or 'noreply@example.com'
    payload = {
        'personalizations': [{'to': [{'email': to_addr}]}],
        'from': {'email': from_addr},
        'subject': subject,
        'content': [{'type': 'text/html', 'value': body_html}],
    }
    try:
        resp = http_requests.post(
            'https://api.sendgrid.com/v3/mail/send',
            headers={
                'Authorization': f'Bearer {SENDGRID_API_KEY}',
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=15
        )
        if resp.status_code in (200, 202):
            print(f'[SendGrid] sent to {to_addr}')
        else:
            print(f'[SendGrid error] {resp.status_code}: {resp.text[:500]}')
            # 403 = sender not verified; 401 = wrong API key
            if resp.status_code == 403:
                print('[SendGrid] ★ 寄件人未驗證！請至 SendGrid → Settings → Sender Authentication 驗證寄件人')
            elif resp.status_code == 401:
                print('[SendGrid] ★ API Key 錯誤，請確認 SENDGRID_API_KEY 環境變數')
    except Exception as e:
        print(f'[SendGrid error] {e}')


def _send_via_gmail(to_addr: str, subject: str, body_html: str):
    """透過 Gmail SMTP SSL 寄信（備用）
    注意：Render 免費方案封鎖 outbound SMTP（port 465/587），此方法無法在 Render 上使用。
    請改用 SendGrid（HTTP API）。
    """
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from_addr = MAIL_FROM or GMAIL_USER
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From']    = from_addr
        msg['To']      = to_addr
        msg.attach(MIMEText(body_html, 'html', 'utf-8'))
        with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=15) as s:
            s.login(GMAIL_USER, GMAIL_APP_PASS)
            s.sendmail(from_addr, to_addr, msg.as_string())
        print(f'[Gmail] sent to {to_addr}')
    except Exception as e:
        print(f'[Gmail error] {e}')


def send_sms(to_phone: str, body: str):
    """透過 Twilio 發送 SMS，未設定則略過"""
    if not USE_TWILIO or not to_phone:
        return
    # 台灣 09xx → +886 9xx
    phone = to_phone.strip().replace('-', '').replace(' ', '')
    if phone.startswith('0'):
        phone = '+886' + phone[1:]
    elif not phone.startswith('+'):
        phone = '+886' + phone
    try:
        resp = http_requests.post(
            f'https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json',
            auth=(TWILIO_SID, TWILIO_TOKEN),
            data={'From': TWILIO_FROM, 'To': phone, 'Body': body},
            timeout=15
        )
        data = resp.json()
        if resp.status_code >= 400:
            print(f'[Twilio error] {data}')
        else:
            print(f'[Twilio] SMS sent to {phone}')
    except Exception as e:
        print(f'[Twilio error] {e}')


def _booking_email_html(booking) -> str:
    """預約確認 Email HTML 內容"""
    room = booking.room.name if booking.room else '—'
    from datetime import datetime as dt
    try:
        d = dt.strptime(booking.date, '%Y-%m-%d')
        weekdays = ['一','二','三','四','五','六','日']
        date_fmt = f'{d.year}/{d.month}/{d.day}（週{weekdays[d.weekday()]}）'
    except Exception:
        date_fmt = booking.date
    dur_str = _fmt_duration(booking.duration)
    price_str = f'NT$ {booking.total_price:,}'
    return f'''<!DOCTYPE html>
<html lang="zh-TW"><head><meta charset="UTF-8">
<style>
  body{{font-family:sans-serif;background:#f5f2ed;margin:0;padding:20px;}}
  .wrap{{max-width:540px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.1);}}
  .hd{{background:#1a3333;padding:28px 32px;}}
  .hd-chip{{display:inline-block;background:#2A6B6B;color:#fff;font-size:12px;font-weight:700;padding:4px 14px;border-radius:20px;margin-bottom:12px;}}
  .hd h1{{color:#fff;font-size:22px;margin:0 0 4px;}}
  .hd p{{color:rgba(255,255,255,.6);font-size:13px;margin:0;}}
  .bd{{padding:28px 32px;}}
  .row{{display:flex;justify-content:space-between;padding:14px 0;border-bottom:1px solid #f0f0f0;font-size:15px;}}
  .row:last-child{{border-bottom:none;}}
  .lbl{{color:#999;min-width:64px;}}
  .val{{color:#111;font-weight:600;text-align:right;}}
  .price{{background:#1a1a1a;border-radius:8px;padding:20px 24px;margin-top:16px;}}
  .price .pl{{color:#888;font-size:12px;margin-bottom:6px;display:block;}}
  .price .pv{{color:#B8965A;font-size:26px;font-weight:700;display:block;}}
  .ft{{text-align:center;padding:16px;color:#aaa;font-size:12px;background:#f8f8f8;}}
</style></head><body>
<div class="wrap">
  <div class="hd">
    <div class="hd-chip">預約已確認</div>
    <h1>{room}</h1>
    <p>預約編號：{booking.booking_number}</p>
  </div>
  <div class="bd">
    <div class="row"><span class="lbl">日期</span><span class="val">{date_fmt}</span></div>
    <div class="row"><span class="lbl">時段</span><span class="val">{_fmt_segments(booking)}</span></div>
    <div class="row"><span class="lbl">時長</span><span class="val">{dur_str}</span></div>
    <div class="row"><span class="lbl">聯絡人</span><span class="val">{booking.customer_name}</span></div>
    <div class="row"><span class="lbl">手機</span><span class="val">{booking.customer_phone}</span></div>
    <div class="row"><span class="lbl">目的</span><span class="val">{booking.purpose or '—'}</span></div>
    <div class="price">
      <span class="pl">總費用</span>
      <span class="pv">{price_str}</span>
    </div>
  </div>
  <div class="ft">如需取消請提前 2 小時聯繫，謝謝您的預約。</div>
</div>
</body></html>'''


def _booking_sms_body(booking) -> str:
    """預約確認 SMS 內文"""
    room = booking.room.name if booking.room else '—'
    return (f'【預約確認】{room}\n'
            f'日期：{booking.date} {booking.start_time}–{booking.end_time}\n'
            f'編號：{booking.booking_number}\n'
            f'如需取消請提前 2 小時告知。')


def _cancel_sms_body(booking) -> str:
    room = booking.room.name if booking.room else '—'
    return (f'【預約取消】{room}\n'
            f'日期：{booking.date} {booking.start_time}–{booking.end_time}\n'
            f'編號：{booking.booking_number}\n'
            f'預約已取消，如有疑問請聯繫管理員。')


def _cancel_email_html(booking) -> str:
    room = booking.room.name if booking.room else '—'
    return f'''<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8">
<style>body{{font-family:sans-serif;background:#f5f2ed;margin:0;padding:20px;}}
.wrap{{max-width:540px;margin:0 auto;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 4px 20px rgba(0,0,0,.1);}}
.hd{{background:#2d0f0f;padding:28px 32px;}}
.hd-chip{{display:inline-block;background:#C44B3A;color:#fff;font-size:12px;font-weight:700;padding:4px 14px;border-radius:20px;margin-bottom:12px;}}
.hd h1{{color:#fff;font-size:22px;margin:0 0 4px;}}
.hd p{{color:rgba(255,255,255,.6);font-size:13px;margin:0;}}
.bd{{padding:24px 32px;font-size:14px;color:#555;line-height:1.7;}}
.ft{{text-align:center;padding:16px;color:#aaa;font-size:12px;background:#f8f8f8;}}</style></head><body>
<div class="wrap">
  <div class="hd"><div class="hd-chip">預約已取消</div><h1>{room}</h1><p>編號：{booking.booking_number}</p></div>
  <div class="bd">您的預約（{booking.date} {booking.start_time}–{booking.end_time}）已取消。<br>如有疑問請聯繫管理員。</div>
  <div class="ft">感謝您的使用。</div>
</div></body></html>'''


# ─────────────────────────────────────────────
# Flex Message 元件
# ─────────────────────────────────────────────

def _info_row(label: str, value: str, value_color: str = '#1a1a1a', bold: bool = False):
    """兩欄資訊列"""
    return {
        'type': 'box', 'layout': 'horizontal',
        'contents': [
            {'type': 'text', 'text': label,
             'size': 'sm', 'color': '#aaaaaa', 'flex': 3, 'gravity': 'center'},
            {'type': 'text', 'text': str(value),
             'size': 'sm', 'color': value_color, 'flex': 7,
             'wrap': True, 'weight': 'bold' if bold else 'regular', 'gravity': 'center'},
        ]
    }


def _divider():
    return {'type': 'box', 'layout': 'vertical', 'margin': 'md',
            'contents': [{'type': 'separator', 'color': '#eeeeee'}]}


def _chip(text: str, bg: str = '#e8f4f4', color: str = '#2A6B6B') -> dict:
    """小標籤膠囊"""
    return {
        'type': 'box', 'layout': 'vertical',
        'backgroundColor': bg, 'cornerRadius': '20px',
        'paddingAll': '6px', 'paddingStart': '14px', 'paddingEnd': '14px',
        'contents': [{'type': 'text', 'text': text,
                      'size': 'xs', 'color': color, 'weight': 'bold'}]
    }


def _hero_gradient_box(top_label: str, top_color: str, title: str,
                        subtitle: str, bg: str) -> dict:
    """頂部視覺 box（模擬漸層 header）"""
    return {
        'type': 'box', 'layout': 'vertical',
        'backgroundColor': bg, 'paddingAll': '24px', 'paddingBottom': '20px',
        'contents': [
            {'type': 'box', 'layout': 'horizontal', 'contents': [
                {'type': 'box', 'layout': 'vertical',
                 'backgroundColor': top_color, 'cornerRadius': '4px',
                 'paddingAll': '4px', 'paddingStart': '10px', 'paddingEnd': '10px',
                 'contents': [{'type': 'text', 'text': top_label,
                               'size': 'xs', 'color': '#ffffff', 'weight': 'bold'}]},
            ]},
            {'type': 'text', 'text': title,
             'color': '#ffffff', 'size': 'xl', 'weight': 'bold', 'margin': 'md', 'wrap': True},
            {'type': 'text', 'text': subtitle,
             'color': '#B3D9D9', 'size': 'sm', 'margin': 'sm', 'wrap': True},
        ]
    }


def _multi_time_badges(booking) -> list:
    """產生多段時段的 badge list，供 Flex 使用"""
    import json as _json
    segs = []
    if booking.segments:
        try: segs = _json.loads(booking.segments)
        except Exception: pass
    if not segs:
        segs = [{'start': booking.start_time, 'end': booking.end_time}]
    if len(segs) == 1:
        return [_time_badge(segs[0]['start'], segs[0]['end'])]
    # 多段：每段一個 badge
    badges = []
    for seg in segs:
        badges.append(_time_badge(seg['start'], seg['end']))
    return badges


def _fmt_segments(booking) -> str:
    """將 booking 的時段格式化成字串，支援多段"""
    import json as _json
    if booking.segments:
        try:
            segs = _json.loads(booking.segments)
            if segs and len(segs) > 1:
                return '、'.join(f'{s["start"]}–{s["end"]}' for s in segs)
            elif segs:
                return f'{segs[0]["start"]} – {segs[0]["end"]}'
        except Exception:
            pass
    return f'{booking.start_time} – {booking.end_time}'


def _time_badge(start: str, end: str) -> dict:
    """時段高亮顯示"""
    return {
        'type': 'box', 'layout': 'horizontal',
        'backgroundColor': '#f0f9f9', 'cornerRadius': '8px',
        'paddingAll': '12px', 'margin': 'md',
        'contents': [
            {'type': 'box', 'layout': 'vertical', 'flex': 1, 'alignItems': 'center',
             'contents': [
                 {'type': 'text', 'text': '開始', 'size': 'xxs', 'color': '#aaaaaa'},
                 {'type': 'text', 'text': start, 'size': 'xl',
                  'color': '#2A6B6B', 'weight': 'bold'},
             ]},
            {'type': 'box', 'layout': 'vertical', 'flex': 0,
             'justifyContent': 'center', 'paddingStart': '8px', 'paddingEnd': '8px',
             'contents': [
                 {'type': 'text', 'text': '→', 'size': 'md', 'color': '#cccccc'}
             ]},
            {'type': 'box', 'layout': 'vertical', 'flex': 1, 'alignItems': 'center',
             'contents': [
                 {'type': 'text', 'text': '結束', 'size': 'xxs', 'color': '#aaaaaa'},
                 {'type': 'text', 'text': end, 'size': 'xl',
                  'color': '#2A6B6B', 'weight': 'bold'},
             ]},
        ]
    }


def _fmt_duration(duration) -> str:
    """Format duration in hours to readable string"""
    if not duration:
        return '—'
    if duration < 1:
        return f'{int(duration * 60)} 分鐘'
    if duration == int(duration):
        return f'{int(duration)} 小時'
    # e.g. 1.5 -> 1 小時 30 分鐘
    h = int(duration)
    m = int((duration - h) * 60)
    return f'{h} 小時 {m} 分鐘' if m else f'{h} 小時'


def _price_block(price: int, duration) -> dict:
    """費用顯示區塊"""
    dur_str = _fmt_duration(duration)
    return {
        'type': 'box', 'layout': 'horizontal',
        'backgroundColor': '#1a1a1a', 'cornerRadius': '8px',
        'paddingAll': '14px', 'margin': 'md',
        'contents': [
            {'type': 'box', 'layout': 'vertical', 'flex': 1,
             'contents': [
                 {'type': 'text', 'text': '總費用', 'size': 'xs', 'color': '#888888'},
                 {'type': 'text', 'text': f'NT$ {price:,}',
                  'size': 'xl', 'color': '#B8965A', 'weight': 'bold'},
             ]},
            {'type': 'box', 'layout': 'vertical', 'flex': 0,
             'justifyContent': 'center', 'alignItems': 'flex-end',
             'contents': [
                 {'type': 'text', 'text': dur_str, 'size': 'sm',
                  'color': '#cccccc', 'align': 'end'},
             ]},
        ]
    }


# ─────────────────────────────────────────────
# 三種 Flex Message
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
# LINE Flex Message 設計系統
# ─────────────────────────────────────────────

_C = {
    'dark':    '#1A3333',
    'teal':    '#2A6B6B',
    'gold':    '#B8965A',
    'red':     '#C44B3A',
    'white':   '#FFFFFF',
    'ink':     '#1A1A1A',
    'ink60':   '#888888',
    'bg':      '#F5F2ED',
    'border':  '#E0DAD0',
}


def _row(label: str, value: str) -> dict:
    return {
        'type': 'box', 'layout': 'horizontal',
        'paddingTop': '8px', 'paddingBottom': '8px',
        'contents': [
            {'type': 'text', 'text': label, 'size': 'sm',
             'color': _C['ink60'], 'flex': 2},
            {'type': 'text', 'text': value, 'size': 'sm',
             'color': _C['ink'], 'flex': 3, 'wrap': True, 'weight': 'bold'},
        ]
    }


def _btn(label: str, action_type: str, data_or_uri: str,
         bg: str = None, color: str = None) -> dict:
    bg    = bg    or _C['teal']
    color = color or _C['white']
    action = ({'type': 'uri',     'label': label, 'uri': data_or_uri}
              if action_type == 'uri' else
              {'type': 'message', 'label': label, 'text': data_or_uri})
    return {
        'type': 'button', 'action': action,
        'style': 'primary', 'color': bg,
        'height': 'sm',
        'margin': 'sm',
    }


def _header_box(title: str, subtitle: str = '', bg: str = None) -> dict:
    bg = bg or _C['dark']
    contents = [
        {'type': 'text', 'text': title, 'color': _C['white'],
         'size': 'lg', 'weight': 'bold', 'wrap': True},
    ]
    if subtitle:
        contents.append(
            {'type': 'text', 'text': subtitle, 'color': '#99C8C8',
             'size': 'xs', 'margin': 'xs', 'wrap': True}
        )
    return {
        'type': 'box', 'layout': 'vertical',
        'backgroundColor': bg,
        'paddingAll': '20px',
        'contents': contents,
    }



def _divider() -> dict:
    return {'type': 'separator', 'color': _C['border'], 'margin': 'md'}


def _fmt_duration(dur_hours) -> str:
    try:
        d = float(dur_hours)
    except Exception:
        return str(dur_hours)
    if d < 1:
        return f'{int(d*60)} 分鐘'
    elif d == int(d):
        return f'{int(d)} 小時'
    else:
        h = int(d)
        return f'{h} 小時 {int((d-h)*60)} 分鐘'


# ─── 主選單 Flex（歡迎 / 說明）────────────────────
def flex_main_menu() -> dict:
    return {
        'type': 'flex', 'altText': '會議室預約系統 — 主選單',
        'contents': {
            'type': 'bubble', 'size': 'mega',
            'header': _header_box('會議室預約系統', '請選擇您要執行的操作'),
            'body': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'],
                'paddingAll': '16px', 'spacing': 'none',
                'contents': [
                    # 說明文字
                    {'type': 'text',
                     'text': '點選下方按鈕，或直接輸入指令操作。',
                     'size': 'sm', 'color': _C['ink60'], 'wrap': True,
                     'margin': 'none'},
                    _divider(),
                    # 指令說明列表
                    {'type': 'box', 'layout': 'vertical', 'margin': 'md',
                     'spacing': 'sm', 'contents': [
                        _cmd_row('前往預約', '開啟網站完成預約'),
                        _cmd_row('我的預約', '查詢最近 3 筆預約記錄'),
                        _cmd_row('時段 2026-03-15', '查詢指定日期各房間時段'),
                        _cmd_row('查詢 MR2026XXXXXX', '依預約編號查詢詳情'),
                        _cmd_row('綁定 0912345678', '綁定手機以接收通知'),
                    ]},
                ]
            },
            'footer': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'],
                'paddingAll': '12px', 'spacing': 'sm',
                'contents': [
                    _btn('開始預約', 'uri', LIFF_URL),
                    _btn('我的預約', 'message', '我的預約',
                         bg='#2D2D2D'),
                ]
            }
        }
    }


def _cmd_row(cmd: str, desc: str) -> dict:
    return {
        'type': 'box', 'layout': 'horizontal',
        'paddingTop': '6px', 'paddingBottom': '6px',
        'contents': [
            {'type': 'text', 'text': cmd,  'size': 'sm', 'color': _C['teal'],
             'weight': 'bold', 'flex': 4, 'wrap': True},
            {'type': 'text', 'text': desc, 'size': 'xs', 'color': _C['ink60'],
             'flex': 5, 'wrap': True, 'align': 'end'},
        ]
    }


# ─── 時段查詢 Flex ─────────────────────────────────
def flex_timeslot(date_str: str, rooms_data: list) -> dict:
    """
    rooms_data: [{'name': str, 'slots': [{'start':..,'end':..}]}]
    """
    from datetime import datetime as _dt
    try:
        d = _dt.strptime(date_str, '%Y-%m-%d')
        weekdays = ['一','二','三','四','五','六','日']
        date_fmt = f'{d.month}/{d.day} 週{weekdays[d.weekday()]}'
    except Exception:
        date_fmt = date_str

    room_rows = []
    for r in rooms_data:
        slots = r['slots']
        if slots:
            slot_str = '  '.join(f"{s['start']}–{s['end']}" for s in slots)
            status_color = _C['red']
            status_text  = '已有預約'
        else:
            slot_str     = '全天可預約'
            status_color = '#2E7D32'
            status_text  = '可預約'

        room_rows.append({
            'type': 'box', 'layout': 'vertical',
            'paddingTop': '10px', 'paddingBottom': '10px',
            'contents': [
                {'type': 'box', 'layout': 'horizontal', 'contents': [
                    {'type': 'text', 'text': r['name'], 'size': 'sm',
                     'weight': 'bold', 'color': _C['ink'], 'flex': 4},
                    {'type': 'box', 'layout': 'vertical',
                     'backgroundColor': status_color,
                     'cornerRadius': '10px',
                     'paddingTop': '2px', 'paddingBottom': '2px',
                     'paddingStart': '8px', 'paddingEnd': '8px',
                     'flex': 0,
                     'contents': [
                         {'type': 'text', 'text': status_text,
                          'size': 'xxs', 'color': _C['white']}
                     ]},
                ]},
                {'type': 'text', 'text': slot_str, 'size': 'xs',
                 'color': _C['ink60'], 'margin': 'sm', 'wrap': True},
            ]
        })

    return {
        'type': 'flex', 'altText': f'{date_str} 時段狀態',
        'contents': {
            'type': 'bubble', 'size': 'mega',
            'header': _header_box(f'{date_fmt}  時段狀態', '以下為各會議室預約狀況'),
            'body': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'],
                'paddingAll': '16px', 'spacing': 'none',
                'contents': room_rows or [
                    {'type': 'text', 'text': '目前沒有可用的會議室',
                     'size': 'sm', 'color': _C['ink60']}
                ],
            },
            'footer': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'],
                'paddingAll': '12px',
                'contents': [
                    _btn('開始預約', 'uri', LIFF_URL),
                ]
            }
        }
    }


# ─── 綁定成功 Flex ─────────────────────────────────
def flex_bind_success(phone: str) -> dict:
    return {
        'type': 'flex', 'altText': '手機號碼綁定成功',
        'contents': {
            'type': 'bubble', 'size': 'kilo',
            'header': _header_box('綁定成功', bg=_C['teal']),
            'body': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'],
                'paddingAll': '16px', 'spacing': 'md',
                'contents': [
                    _row('綁定號碼', phone),
                    _row('通知項目', '預約成立 / 取消'),
                    {'type': 'text',
                     'text': '往後預約成立或取消時，將自動推播通知給您。',
                     'size': 'sm', 'color': _C['ink60'],
                     'margin': 'md', 'wrap': True},
                ]
            },
            'footer': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'],
                'paddingAll': '12px',
                'contents': [_btn('開始預約', 'uri', LIFF_URL)]
            }
        }
    }


# ─── 查無預約 Flex ─────────────────────────────────
def flex_not_found(msg: str, hint: str = '') -> dict:
    return {
        'type': 'flex', 'altText': msg,
        'contents': {
            'type': 'bubble', 'size': 'kilo',
            'header': _header_box('查無資料', bg='#4A4A4A'),
            'body': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'],
                'paddingAll': '16px', 'spacing': 'sm',
                'contents': [
                    {'type': 'text', 'text': msg, 'size': 'sm',
                     'color': _C['ink'], 'wrap': True},
                    *([{'type': 'text', 'text': hint, 'size': 'xs',
                        'color': _C['ink60'], 'wrap': True, 'margin': 'sm'}]
                      if hint else []),
                ]
            },
            'footer': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'],
                'paddingAll': '12px',
                'contents': [_btn('返回主選單', 'message', '說明')]
            }
        }
    }


# ─── 歡迎加入 Flex ─────────────────────────────────
def flex_welcome() -> dict:
    return {
        'type': 'flex', 'altText': '歡迎使用會議室預約系統',
        'contents': {
            'type': 'bubble', 'size': 'mega',
            'header': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['dark'],
                'paddingAll': '24px',
                'contents': [
                    {'type': 'text', 'text': '歡迎使用',
                     'color': '#80B8B8',
                     'size': 'sm'},
                    {'type': 'text', 'text': '會議室預約系統',
                     'color': _C['white'], 'size': 'xl',
                     'weight': 'bold', 'margin': 'xs'},
                ]
            },
            'body': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'],
                'paddingAll': '16px', 'spacing': 'md',
                'contents': [
                    {'type': 'text',
                     'text': '您可以透過本系統查詢會議室時段、綁定手機號碼接收預約通知。',
                     'size': 'sm', 'color': _C['ink60'], 'wrap': True},
                    _divider(),
                    {'type': 'text', 'text': '建議先完成以下步驟：',
                     'size': 'sm', 'weight': 'bold', 'color': _C['ink'],
                     'margin': 'md'},
                    _step_row('1', '前往網站，完成會議室預約'),
                    _step_row('2', '綁定手機號碼以接收通知'),
                    _step_row('3', '輸入「說明」查看所有指令'),
                ]
            },
            'footer': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'],
                'paddingAll': '12px', 'spacing': 'sm',
                'contents': [
                    _btn('開始預約', 'uri', LIFF_URL),
                    _btn('查看所有指令', 'message', '說明', bg='#2D2D2D'),
                ]
            }
        }
    }


def _step_row(num: str, text: str) -> dict:
    return {
        'type': 'box', 'layout': 'horizontal',
        'margin': 'sm', 'spacing': 'md',
        'contents': [
            {'type': 'box', 'layout': 'vertical',
             'backgroundColor': _C['teal'], 'cornerRadius': '20px',
             'width': '22px', 'height': '22px',
             'justifyContent': 'center', 'alignItems': 'center',
             'contents': [{'type': 'text', 'text': num, 'size': 'xs',
                           'color': _C['white'], 'align': 'center'}]},
            {'type': 'text', 'text': text, 'size': 'sm',
             'color': _C['ink'], 'flex': 1, 'wrap': True,
             'gravity': 'center'},
        ]
    }


def flex_booking_confirm(booking) -> dict:
    """
    預約成立通知（給使用者）
    ┌──────────────────────────┐
    │   深青色 header        │
    │  預約成立 · 編號          │
    ├──────────────────────────┤
    │  會議室名稱 (大字)        │
    │   日期   人數   樓層│
    │  ┌── 時段視覺化 ──┐      │
    │  │  09:00  →  12:00     │
    │  └──────────────┘       │
    │  ─────────────          │
    │  資訊列 × 4             │
    │  ─────────────          │
    │  NT$ 3,000  /  3 小時   │
    ├──────────────────────────┤
    │  [查詢預約] 按鈕          │
    └──────────────────────────┘
    """
    room     = booking.room.name if booking.room else '—'
    floor    = (booking.room.floor or '') if booking.room else ''
    room_type= (booking.room.room_type or '') if booking.room else ''
    site_url = SiteContent.get('site_url', 'https://seat-booking-rlf2.onrender.com')

    # 日期格式化
    try:
        from datetime import datetime as dt
        d = dt.strptime(booking.date, '%Y-%m-%d')
        weekdays = ['一', '二', '三', '四', '五', '六', '日']
        date_fmt = f'{d.month}/{d.day}（週{weekdays[d.weekday()]}）'
    except Exception:
        date_fmt = booking.date

    return {
        'type': 'flex',
        'altText': f'【預約確認】{room}｜{booking.date} {_fmt_segments(booking)}｜編號 {booking.booking_number}',
        'contents': {
            'type': 'bubble',
            'size': 'kilo',
            'header': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': '#1a3333',
                'paddingAll': '0px',
                'contents': [
                    # 頂部色條
                    {'type': 'box', 'layout': 'vertical',
                     'backgroundColor': '#2A6B6B', 'height': '4px',
                     'contents': []},
                    # 主 header 內容
                    {'type': 'box', 'layout': 'vertical',
                     'paddingAll': '20px',
                     'contents': [
                         # 狀態標籤（上排）
                         {'type': 'box', 'layout': 'horizontal',
                          'contents': [
                             {'type': 'box', 'layout': 'vertical',
                              'backgroundColor': '#2A6B6B', 'cornerRadius': '20px',
                              'paddingAll': '4px', 'paddingStart': '12px', 'paddingEnd': '12px',
                              'flex': 0,
                              'contents': [{'type': 'text', 'text': '預約已確認',
                                            'size': 'xs', 'color': '#ffffff', 'weight': 'bold'}]},
                         ]},
                         # 預約編號（獨立一行，完整顯示）
                         {'type': 'text', 'text': booking.booking_number,
                          'size': 'xs', 'color': '#66AAAA',
                          'margin': 'sm', 'wrap': False},
                         # 會議室名稱
                         {'type': 'text', 'text': room,
                          'size': 'xl', 'color': '#ffffff', 'weight': 'bold',
                          'margin': 'lg', 'wrap': True},
                         # 副標籤列
                         {'type': 'box', 'layout': 'horizontal', 'margin': 'sm',
                          'contents': [
                              {'type': 'text', 'text': f'{room_type}',
                               'size': 'xs', 'color': '#8CBFBF'},
                              {'type': 'text', 'text': floor or '',
                               'size': 'xs', 'color': '#8CBFBF'},
                          ]},
                     ]},
                ]
            },
            'body': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': '#ffffff',
                'paddingAll': '18px', 'spacing': 'none',
                'contents': [
                    # 日期 + 人數 chips
                    {'type': 'box', 'layout': 'horizontal', 'spacing': 'sm', 'contents': [
                        _chip(f'{date_fmt}'),
                        _chip(f'{booking.attendees} 人'),
                    ]},
                    # 時段視覺化
                    *(_multi_time_badges(booking)),
                    _divider(),
                    # 資訊列
                    {'type': 'box', 'layout': 'vertical', 'spacing': 'sm',
                     'margin': 'md', 'contents': [
                         _info_row('聯絡人', booking.customer_name, bold=True),
                         _info_row('部門',   booking.department or '—'),
                         _info_row('目的',   booking.purpose or '—'),
                     ]},
                    # 費用區塊
                    _price_block(booking.total_price, booking.duration),
                ]
            },
            'footer': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': '#f8f8f8',
                'paddingAll': '14px', 'spacing': 'sm',
                'contents': [
                    {'type': 'button',
                     'action': {'type': 'message', 'label': '查詢此預約',
                                'text': f'查詢 {booking.booking_number}'},
                     'style': 'primary', 'color': '#2A6B6B',
                     'height': 'sm'},
                    {'type': 'button',
                     'action': {'type': 'message', 'label': '取消此預約',
                                'text': f'取消預約 {booking.booking_number}'},
                     'style': 'secondary', 'height': 'sm',
                     'color': '#C44B3A'},
                ]
            }
        }
    }


def flex_booking_cancel(booking) -> dict:
    """
    預約取消通知（給使用者）
    ┌──────────────────────────┐
    │   深紅 header          │
    │  預約已取消              │
    ├──────────────────────────┤
    │  會議室 / 日期 / 時段     │
    │  編號                    │
    ├──────────────────────────┤
    │  重新預約 按鈕            │
    └──────────────────────────┘
    """
    room     = booking.room.name if booking.room else '—'
    floor    = (booking.room.floor or '') if booking.room else ''
    site_url = SiteContent.get('site_url', 'https://seat-booking-rlf2.onrender.com')

    try:
        from datetime import datetime as dt
        d = dt.strptime(booking.date, '%Y-%m-%d')
        weekdays = ['一', '二', '三', '四', '五', '六', '日']
        date_fmt = f'{d.month}/{d.day}（週{weekdays[d.weekday()]}）'
    except Exception:
        date_fmt = booking.date

    return {
        'type': 'flex',
        'altText': f'【預約取消】{room}｜{booking.date}｜編號 {booking.booking_number}',
        'contents': {
            'type': 'bubble',
            'size': 'kilo',
            'header': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': '#2d0f0f',
                'paddingAll': '0px',
                'contents': [
                    {'type': 'box', 'layout': 'vertical',
                     'backgroundColor': '#C44B3A', 'height': '4px', 'contents': []},
                    {'type': 'box', 'layout': 'vertical', 'paddingAll': '20px',
                     'contents': [
                         {'type': 'box', 'layout': 'horizontal', 'contents': [
                             {'type': 'box', 'layout': 'vertical',
                              'backgroundColor': '#C44B3A', 'cornerRadius': '20px',
                              'paddingAll': '4px', 'paddingStart': '12px', 'paddingEnd': '12px',
                              'contents': [{'type': 'text', 'text': '預約已取消',
                                            'size': 'xs', 'color': '#ffffff', 'weight': 'bold'}]},
                         ]},
                         {'type': 'text', 'text': room,
                          'size': 'xl', 'color': '#ffffff', 'weight': 'bold',
                          'margin': 'lg', 'wrap': True},
                         {'type': 'text', 'text': f'{date_fmt} {_fmt_segments(booking)}',
                          'size': 'sm', 'color': '#8CBFBF', 'margin': 'sm'},
                     ]},
                ]
            },
            'body': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': '#ffffff', 'paddingAll': '18px', 'spacing': 'sm',
                'contents': [
                    _info_row('預約編號', booking.booking_number),
                    _info_row('樓層',     floor or '—'),
                    _divider(),
                    {'type': 'text',
                     'text': '此預約已由管理員取消，如有疑問請聯繫管理員。',
                     'size': 'xs', 'color': '#aaaaaa', 'wrap': True, 'margin': 'md'},
                ]
            },
            'footer': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': '#f8f8f8', 'paddingAll': '14px',
                'contents': [
                    {'type': 'button',
                     'action': {'type': 'message', 'label': '重新預約',
                                'text': '預約'},
                     'style': 'primary', 'color': '#2A6B6B', 'height': 'sm'},
                ]
            }
        }
    }


def flex_admin_notify(booking) -> dict:
    """
    新預約管理員通知
    ┌──────────────────────────┐
    │   金色 header          │
    │   新預約通知  時間戳   │
    ├──────────────────────────┤
    │  姓名（大字）部門         │
    │  電話                    │
    │  ─────                  │
    │  會議室 / 日期 / 時段     │
    │  時段視覺化              │
    │  ─────                  │
    │  人數 / 目的 / 備註       │
    │  費用區塊                │
    └──────────────────────────┘
    """
    room      = booking.room.name if booking.room else '—'
    floor     = (booking.room.floor or '') if booking.room else ''
    room_type = (booking.room.room_type or '') if booking.room else ''
    created   = booking.created_at.strftime('%m/%d %H:%M') if booking.created_at else ''

    try:
        from datetime import datetime as dt
        d = dt.strptime(booking.date, '%Y-%m-%d')
        weekdays = ['一', '二', '三', '四', '五', '六', '日']
        date_fmt = f'{d.month}/{d.day}（週{weekdays[d.weekday()]}）'
    except Exception:
        date_fmt = booking.date

    note_contents = []
    if booking.note and booking.note.strip():
        note_contents = [
            _divider(),
            {'type': 'box', 'layout': 'vertical', 'margin': 'md',
             'backgroundColor': '#fffbf0', 'cornerRadius': '8px', 'paddingAll': '10px',
             'contents': [
                 {'type': 'text', 'text': '備註', 'size': 'xs',
                  'color': '#B8965A', 'weight': 'bold'},
                 {'type': 'text', 'text': booking.note, 'size': 'sm',
                  'color': '#555555', 'wrap': True, 'margin': 'sm'},
             ]},
        ]

    return {
        'type': 'flex',
        'altText': f'【新預約】{booking.customer_name}・{room}｜{booking.date} {_fmt_segments(booking)}',
        'contents': {
            'type': 'bubble',
            'size': 'kilo',
            'header': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': '#1f1600',
                'paddingAll': '0px',
                'contents': [
                    {'type': 'box', 'layout': 'vertical',
                     'backgroundColor': '#B8965A', 'height': '4px', 'contents': []},
                    {'type': 'box', 'layout': 'vertical', 'paddingAll': '20px',
                     'contents': [
                         {'type': 'box', 'layout': 'horizontal', 'contents': [
                             {'type': 'box', 'layout': 'vertical',
                              'backgroundColor': '#B8965A', 'cornerRadius': '20px',
                              'paddingAll': '4px', 'paddingStart': '12px', 'paddingEnd': '12px',
                              'contents': [{'type': 'text', 'text': '新預約通知',
                                            'size': 'xs', 'color': '#ffffff', 'weight': 'bold'}]},
                             {'type': 'filler'},
                             {'type': 'text', 'text': created,
                              'size': 'xs', 'color': '#66AAAA', 'gravity': 'center'},
                         ]},
                         # 顧客姓名
                         {'type': 'text', 'text': booking.customer_name,
                          'size': 'xl', 'color': '#ffffff', 'weight': 'bold', 'margin': 'lg'},
                         {'type': 'box', 'layout': 'horizontal', 'margin': 'sm', 'contents': [
                             {'type': 'text', 'text': booking.customer_phone,
                              'size': 'xs', 'color': '#99C8C8'},
                             {'type': 'text', 'text': booking.department or '',
                              'size': 'xs', 'color': '#66AAAA'},
                         ]},
                     ]},
                ]
            },
            'body': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': '#ffffff', 'paddingAll': '18px', 'spacing': 'none',
                'contents': [
                    # 會議室資訊
                    {'type': 'box', 'layout': 'horizontal', 'spacing': 'sm', 'contents': [
                        _chip(f'{room}', bg='#e8f4f4', color='#2A6B6B'),
                        _chip(f'{date_fmt}', bg='#f5f2ed', color='#B8965A'),
                    ]},
                    # 時段視覺化
                    *(_multi_time_badges(booking)),
                    _divider(),
                    # 詳細資訊
                    {'type': 'box', 'layout': 'vertical', 'spacing': 'sm',
                     'margin': 'md', 'contents': [
                         _info_row('會議室',   f'{room} {floor}'),
                         _info_row('出席人數', f'{booking.attendees} 人'),
                         _info_row('會議目的', booking.purpose or '—'),
                         _info_row('預約編號', booking.booking_number),
                     ]},
                    # 備註（有才顯示）
                    *note_contents,
                    # 費用
                    _price_block(booking.total_price, booking.duration),
                ]
            }
        }
    }


# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────

class Room(db.Model):
    __tablename__ = 'rooms'
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(100), nullable=False)
    room_type   = db.Column(db.String(50), nullable=False)
    capacity    = db.Column(db.Integer, default=10)   # 最大容納
    capacity_min = db.Column(db.Integer, default=0)   # 最少人數（0=不設下限）
    hourly_rate = db.Column(db.Integer, default=500)
    description = db.Column(db.Text)
    amenities   = db.Column(db.Text)
    photo_url   = db.Column(db.String(500))
    photos      = db.Column(db.Text)   # JSON: ["url1","url2",...] 最多5張
    cover_index = db.Column(db.Integer, default=0)  # 主封面為第幾張
    is_active   = db.Column(db.Boolean, default=True)
    floor       = db.Column(db.String(20))
    created_at  = db.Column(db.DateTime, default=datetime.now)

    def get_photos(self):
        """回傳照片陣列（含舊 photo_url 相容）"""
        if self.photos:
            try:
                arr = json.loads(self.photos)
                if arr: return arr
            except Exception: pass
        if self.photo_url:
            return [self.photo_url]
        return []

    def get_cover(self):
        """回傳主封面 URL"""
        photos = self.get_photos()
        if not photos: return ''
        idx = self.cover_index or 0
        return photos[idx] if idx < len(photos) else photos[0]

    def to_dict(self):
        photos = self.get_photos()
        return {
            'id': self.id, 'name': self.name, 'room_type': self.room_type,
            'capacity': self.capacity,
            'capacity_min': self.capacity_min or 0,
            'capacity_label': (
                f'{self.capacity_min}–{self.capacity} 人'
                if self.capacity_min and self.capacity_min > 0
                else f'{self.capacity} 人'
            ),
            'hourly_rate': self.hourly_rate,
            'description': self.description,
            'amenities': json.loads(self.amenities) if self.amenities else [],
            'photo_url': self.get_cover(),
            'photos': photos,
            'cover_index': self.cover_index or 0,
            'is_active': self.is_active, 'floor': self.floor,
        }


class Booking(db.Model):
    __tablename__ = 'bookings'
    id             = db.Column(db.Integer, primary_key=True)
    booking_number = db.Column(db.String(20), unique=True)
    room_id        = db.Column(db.Integer, db.ForeignKey('rooms.id'))
    customer_name  = db.Column(db.String(50), nullable=False)
    customer_phone = db.Column(db.String(20), nullable=False)
    customer_email = db.Column(db.String(100))
    department     = db.Column(db.String(100))
    date           = db.Column(db.String(10), nullable=False)
    start_time     = db.Column(db.String(5), nullable=False)
    end_time       = db.Column(db.String(5), nullable=False)
    duration       = db.Column(db.Float, default=1)
    segments       = db.Column(db.Text)   # JSON: [{"start":"08:30","end":"10:00"},...]
    total_price    = db.Column(db.Integer, default=0)
    attendees      = db.Column(db.Integer, default=1)
    purpose        = db.Column(db.Text)
    status         = db.Column(db.String(20), default='confirmed')
    note           = db.Column(db.Text)
    line_user_id   = db.Column(db.String(100))   # 綁定 LINE userId
    created_at     = db.Column(db.DateTime, default=datetime.now)
    room           = db.relationship('Room', backref='bookings')

    def to_dict(self):
        return {
            'id': self.id, 'booking_number': self.booking_number,
            'room_id': self.room_id,
            'room_name': self.room.name if self.room else '',
            'room_type': self.room.room_type if self.room else '',
            'customer_name': self.customer_name, 'customer_phone': self.customer_phone,
            'customer_email': self.customer_email, 'department': self.department,
            'date': self.date, 'start_time': self.start_time, 'end_time': self.end_time,
            'segments': self.segments,
            'duration': self.duration, 'total_price': self.total_price,
            'attendees': self.attendees, 'purpose': self.purpose,
            'status': self.status, 'note': self.note,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else ''
        }


class SiteContent(db.Model):
    __tablename__ = 'site_content'
    id         = db.Column(db.Integer, primary_key=True)
    key        = db.Column(db.String(100), unique=True, nullable=False)
    value      = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    @staticmethod
    def get(key, default=''):
        obj = SiteContent.query.filter_by(key=key).first()
        return obj.value if obj else default

    @staticmethod
    def set(key, value):
        obj = SiteContent.query.filter_by(key=key).first()
        if obj:
            obj.value = value
            obj.updated_at = datetime.now()
        else:
            obj = SiteContent(key=key, value=value)
            db.session.add(obj)
        db.session.commit()


class BlockedSlot(db.Model):
    """管理員封鎖的時段（不開放預約）"""
    __tablename__ = 'blocked_slots'
    id         = db.Column(db.Integer, primary_key=True)
    room_id    = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=True)  # None = 全館
    date       = db.Column(db.String(10), nullable=False)   # YYYY-MM-DD
    start_time = db.Column(db.String(5), nullable=False)    # HH:MM
    end_time   = db.Column(db.String(5), nullable=False)    # HH:MM
    reason     = db.Column(db.String(200), default='')
    created_at = db.Column(db.DateTime, default=datetime.now)
    room       = db.relationship('Room', backref='blocked_slots')

    def to_dict(self):
        return {
            'id': self.id, 'room_id': self.room_id,
            'room_name': self.room.name if self.room else '全館',
            'date': self.date, 'start_time': self.start_time, 'end_time': self.end_time,
            'reason': self.reason,
        }



class AdminUser(db.Model):
    __tablename__ = 'admin_users'
    id            = db.Column(db.Integer, primary_key=True)
    username      = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(128), nullable=False)
    display_name  = db.Column(db.String(50), default='')
    role          = db.Column(db.String(20), default='staff')
    permissions   = db.Column(db.Text, default='')
    is_active     = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime, default=datetime.now)
    created_by    = db.Column(db.String(50), default='')

    def set_password(self, pw):
        import hashlib
        self.password_hash = hashlib.sha256(pw.encode()).hexdigest()

    def check_password(self, pw):
        import hashlib
        return self.password_hash == hashlib.sha256(pw.encode()).hexdigest()

    def get_permissions(self):
        import json as _j
        ALL = ['dashboard','bookings','rooms','content','photos','formfields','blocked','accounts','logs']
        if self.role == 'superadmin':
            return ALL
        if self.permissions:
            try:
                return _j.loads(self.permissions)
            except Exception:
                pass
        defaults = {
            'admin':   ['dashboard','bookings','rooms','content','photos','formfields','blocked'],
            'manager': ['dashboard','bookings','rooms'],
            'staff':   ['dashboard','bookings'],
        }
        return defaults.get(self.role, ['dashboard'])

    def to_dict(self):
        return {
            'id': self.id, 'username': self.username,
            'display_name': self.display_name, 'role': self.role,
            'permissions': self.get_permissions(),
            'is_active': self.is_active,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else '',
            'created_by': self.created_by,
        }


class AdminLoginLog(db.Model):
    __tablename__ = 'admin_login_logs'
    id          = db.Column(db.Integer, primary_key=True)
    username    = db.Column(db.String(50), nullable=False)
    success     = db.Column(db.Boolean, default=True)
    ip_address  = db.Column(db.String(50), default='')
    country     = db.Column(db.String(100), default='')
    city        = db.Column(db.String(100), default='')
    user_agent  = db.Column(db.String(300), default='')
    login_at    = db.Column(db.DateTime, default=datetime.now)
    note        = db.Column(db.String(200), default='')

    def to_dict(self):
        return {
            'id': self.id, 'username': self.username,
            'success': self.success, 'ip_address': self.ip_address,
            'country': self.country, 'city': self.city,
            'user_agent': self.user_agent[:80] if self.user_agent else '',
            'login_at': self.login_at.strftime('%Y-%m-%d %H:%M:%S') if self.login_at else '',
            'note': self.note,
        }


class LineUser(db.Model):
    """LINE Bot 使用者記錄"""
    __tablename__ = 'line_users'
    id              = db.Column(db.Integer, primary_key=True)
    line_user_id    = db.Column(db.String(100), unique=True, nullable=False)
    phone           = db.Column(db.String(20))
    display_name    = db.Column(db.String(100))
    is_admin        = db.Column(db.Boolean, default=False)
    created_at      = db.Column(db.DateTime, default=datetime.now)
    booking_session = db.Column(db.Text)  # JSON：儲存預約對話進度

    def to_dict(self):
        return {
            'line_user_id': self.line_user_id, 'phone': self.phone,
            'display_name': self.display_name, 'is_admin': self.is_admin,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else '',
        }


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def check_admin():
    if session.get('admin_user_id'):
        uid = session['admin_user_id']
        if uid == 0:
            return None
        u = AdminUser.query.get(uid)
        if u and u.is_active:
            return None
        session.clear()
    pw = request.headers.get('X-Admin-Password')
    if pw:
        if pw == ADMIN_PASSWORD:
            return None
        u = AdminUser.query.filter_by(username='admin').first()
        if u and u.check_password(pw):
            return None
    return jsonify({'error': 'Unauthorized'}), 401


def get_current_admin():
    if session.get('admin_user_id'):
        uid = session['admin_user_id']
        if uid == 0:
            return AdminUser.query.filter_by(username='admin').first()
        return AdminUser.query.get(uid)
    pw = request.headers.get('X-Admin-Password')
    if pw:
        return AdminUser.query.filter_by(username='admin').first()
    return None


def get_client_ip():
    for h in ['X-Forwarded-For', 'X-Real-IP', 'CF-Connecting-IP']:
        v = request.headers.get(h)
        if v:
            return v.split(',')[0].strip()
    return request.remote_addr or ''


def get_ip_location(ip):
    if not ip or ip in ('127.0.0.1', '::1', 'localhost'):
        return '本機', ''
    try:
        r = http_requests.get(
            f'http://ip-api.com/json/{ip}?lang=zh-TW&fields=status,country,city',
            timeout=3)
        d = r.json()
        if d.get('status') == 'success':
            return d.get('country', ''), d.get('city', '')
    except Exception:
        pass
    return '', ''



def generate_booking_number():
    today = datetime.now().strftime('%Y%m%d')
    count = Booking.query.filter(Booking.booking_number.like(f'MR{today}%')).count()
    return f'MR{today}{str(count + 1).zfill(4)}'


def allowed_file(fn):
    return '.' in fn and fn.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def check_availability(room_id, date, start_time, end_time, exclude_id=None):
    """檢查單一時段是否可用（支援多段預約的 segments 展開，含封鎖時段）"""
    import json as _json
    def m(t): h, mn = map(int, t.split(':')); return h*60+mn
    s, e = m(start_time), m(end_time)
    # 一般預約衝突
    bookings = Booking.query.filter_by(room_id=room_id, date=date).filter(
        Booking.status.in_(['confirmed', 'completed'])).all()
    if exclude_id:
        bookings = [b for b in bookings if b.id != exclude_id]
    for b in bookings:
        segs = []
        if b.segments:
            try: segs = _json.loads(b.segments)
            except Exception: pass
        if not segs:
            segs = [{'start': b.start_time, 'end': b.end_time}]
        for seg in segs:
            if not (e <= m(seg['start']) or s >= m(seg['end'])):
                return False
    # 封鎖時段衝突（全館 + 指定房間）
    blocked = BlockedSlot.query.filter_by(date=date).filter(
        (BlockedSlot.room_id == room_id) | (BlockedSlot.room_id.is_(None))
    ).all()
    for bl in blocked:
        if not (e <= m(bl.start_time) or s >= m(bl.end_time)):
            return False
    return True


def check_segments_availability(room_id, date, segments, exclude_id=None):
    """檢查多段時段是否全部可用"""
    for seg in segments:
        if not check_availability(room_id, date, seg['start'], seg['end'], exclude_id):
            return False, seg
    return True, None


def get_booked_slots(room_id, date):
    import json as _json
    bookings = Booking.query.filter_by(room_id=room_id, date=date).filter(
        Booking.status.in_(['confirmed', 'completed'])).all()
    result = []
    for b in bookings:
        segs = []
        if b.segments:
            try: segs = _json.loads(b.segments)
            except Exception: pass
        if not segs:
            segs = [{'start': b.start_time, 'end': b.end_time}]
        for seg in segs:
            result.append({'start': seg['start'], 'end': seg['end'],
                           'booking_number': b.booking_number})
    # 加入封鎖時段（全館 + 指定房間）
    blocked = BlockedSlot.query.filter_by(date=date).filter(
        (BlockedSlot.room_id == room_id) | (BlockedSlot.room_id.is_(None))
    ).all()
    for bl in blocked:
        result.append({'start': bl.start_time, 'end': bl.end_time,
                       'blocked': True, 'reason': bl.reason or '不開放'})
    return result


def admin_line_ids():
    return [u.line_user_id for u in LineUser.query.filter_by(is_admin=True).all()]


def upsert_line_user(user_id, display_name=''):
    lu = LineUser.query.filter_by(line_user_id=user_id).first()
    if not lu:
        lu = LineUser(line_user_id=user_id, display_name=display_name)
        db.session.add(lu)
        db.session.commit()
    return lu


# ─────────────────────────────────────────────
# Static Files
# ─────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

@app.route('/admin')
def admin_login_page():
    return send_from_directory('static', 'admin_login.html')

@app.route('/dashboard')
def dashboard():
    return send_from_directory('static', 'admin_dashboard.html')

@app.route('/static/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# ─────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────

@app.route('/api/site-content')
def get_site_content():
    keys = ['site_title','site_subtitle','site_description','hero_badge',
            'step1_title','step2_title','step3_title',
            'service_hours','contact_phone','contact_email','footer_text',
            'notice_1','notice_2','notice_3','notice_4','notice_5','logo_url']
    data = {k: SiteContent.get(k) for k in keys}
    # form_fields：若未設定則回傳預設值
    data['form_fields'] = SiteContent.get('form_fields') or """[{\"id\": \"name\", \"label\": \"聯絡人姓名\", \"type\": \"text\", \"placeholder\": \"請輸入姓名\", \"required\": true, \"system\": true, \"full\": false}, {\"id\": \"phone\", \"label\": \"手機號碼\", \"type\": \"tel\", \"placeholder\": \"0912345678\", \"required\": true, \"system\": true, \"full\": false}, {\"id\": \"email\", \"label\": \"Email\", \"type\": \"email\", \"placeholder\": \"your@email.com\", \"required\": true, \"system\": true, \"full\": false, \"hint\": \"必填，接收確認信\"}, {\"id\": \"department\", \"label\": \"部門／公司\", \"type\": \"text\", \"placeholder\": \"例：行銷部\", \"required\": false, \"system\": true, \"full\": false}, {\"id\": \"attendees\", \"label\": \"預計出席人數\", \"type\": \"select\", \"options\": \"1,2,3,4,5,6,8,10,15,20,30,50\", \"required\": false, \"system\": true, \"full\": false}, {\"id\": \"purpose\", \"label\": \"會議類型\", \"type\": \"select\", \"options\": \"部門會議,客戶洽談,員工培訓,產品發表,視訊會議,腦力激盪,其他\", \"required\": false, \"system\": true, \"full\": false}, {\"id\": \"note\", \"label\": \"備註\", \"type\": \"textarea\", \"placeholder\": \"特殊需求或注意事項...\", \"required\": false, \"system\": true, \"full\": true}]"""
    return jsonify(data)


@app.route('/api/rooms')
def get_rooms():
    return jsonify([r.to_dict() for r in Room.query.filter_by(is_active=True).all()])


@app.route('/api/rooms/<int:room_id>/availability')
def room_availability(room_id):
    date = request.args.get('date')
    if not date:
        return jsonify({'error': 'Missing date'}), 400
    return jsonify({'booked_slots': get_booked_slots(room_id, date)})


@app.route('/api/line/bind-profile', methods=['POST'])
def line_bind_profile():
    """LIFF 自動傳入 LINE 用戶資訊，建立或更新 LineUser 記錄"""
    data = request.get_json() or {}
    uid  = data.get('line_user_id', '').strip()
    if not uid:
        return jsonify({'error': 'missing line_user_id'}), 400
    lu = LineUser.query.filter_by(line_user_id=uid).first()
    if not lu:
        lu = LineUser(line_user_id=uid,
                      display_name=data.get('display_name', ''))
        db.session.add(lu)
    else:
        if data.get('display_name'):
            lu.display_name = data['display_name']
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/book', methods=['POST'])
def create_booking():
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': '請求格式錯誤'}), 400

        import json as _json
        room = Room.query.get(data.get('room_id'))
        if not room:
            return jsonify({'error': '找不到此會議室'}), 404

        # 支援多段時段
        segments = data.get('segments')  # [{"start":"08:30","end":"10:00"},...]
        if segments and len(segments) > 0:
            ok, conflict = check_segments_availability(room.id, data['date'], segments)
            if not ok:
                return jsonify({'error': f'時段 {conflict["start"]}–{conflict["end"]} 已被預約，請選擇其他時間'}), 400
            def _m(t):
                h, mn = map(int, t.split(':'))
                return h * 60 + mn
            dur = sum((_m(s['end']) - _m(s['start'])) for s in segments) / 60
            start_time = segments[0]['start']
            end_time   = segments[-1]['end']
            segments_json = _json.dumps(segments, ensure_ascii=False)
        else:
            # 單段時段（向下相容）
            segments = None
            if not check_availability(room.id, data['date'], data['start_time'], data['end_time']):
                return jsonify({'error': '此時段已被預約，請選擇其他時間'}), 400
            def _m(t):
                h, mn = map(int, t.split(':'))
                return h * 60 + mn
            dur = (_m(data['end_time']) - _m(data['start_time'])) / 60
            start_time = data['start_time']
            end_time   = data['end_time']
            segments_json = None

        if not data.get('phone', '').strip():
            return jsonify({'error': '請填寫手機號碼'}), 400
        if not data.get('email', '').strip():
            return jsonify({'error': '請填寫 Email，用於接收預約確認信'}), 400

        price = int(dur * room.hourly_rate)

        line_uid = data.get('line_user_id', '')
        if not line_uid and data.get('phone'):
            lu = LineUser.query.filter_by(phone=data['phone']).first()
            if lu:
                line_uid = lu.line_user_id

        booking = Booking(
            booking_number = generate_booking_number(),
            room_id        = room.id,
            customer_name  = data['name'],
            customer_phone = data['phone'],
            customer_email = data.get('email', ''),
            department     = data.get('department', ''),
            date           = data['date'],
            start_time     = start_time,
            end_time       = end_time,
            segments       = segments_json,
            duration       = dur,
            total_price    = price,
            attendees      = data.get('attendees', 1),
            purpose        = data.get('purpose', ''),
            note           = data.get('note', ''),
            line_user_id   = line_uid,
        )
        db.session.add(booking)
        db.session.commit()
        booking = Booking.query.get(booking.id)

        # 通知（失敗不影響預約成立）
        try:
            if booking.line_user_id:
                push_line(booking.line_user_id, [flex_booking_confirm(booking)])
            for aid in admin_line_ids():
                push_line(aid, [flex_admin_notify(booking)])
            if booking.customer_email:
                send_email(booking.customer_email,
                           f'【預約確認】{booking.room.name} – {booking.date}',
                           _booking_email_html(booking))
            send_sms(booking.customer_phone, _booking_sms_body(booking))
        except Exception as ne:
            print(f'[通知錯誤] {ne}')

        return jsonify({'success': True, 'booking': booking.to_dict()}), 201

    except Exception as e:
        db.session.rollback()
        print(f'[create_booking error] {type(e).__name__}: {e}')
        import traceback; traceback.print_exc()
        return jsonify({'error': f'預約失敗，請稍後再試（{type(e).__name__}）'}), 500

@app.route('/api/bookings/check')
def check_booking():
    number = request.args.get('number')
    phone  = request.args.get('phone')
    if not number or not phone:
        return jsonify({'error': '請提供預約編號和電話'}), 400
    b = Booking.query.filter_by(booking_number=number, customer_phone=phone).first()
    if not b:
        return jsonify({'error': '找不到此預約'}), 404
    return jsonify(b.to_dict())


# ─────────────────────────────────────────────
# LINE Webhook
# ─────────────────────────────────────────────

@app.route('/webhook/line', methods=['POST'])
def line_webhook():
    """
    LINE Messaging API Webhook
    Console 設定：Webhook URL = https://<your>.onrender.com/webhook/line
    """
    signature = request.headers.get('X-Line-Signature', '')
    body      = request.get_data()

    if not verify_line_signature(body, signature):
        return jsonify({'error': 'Invalid signature'}), 403

    events = (request.get_json() or {}).get('events', [])
    for event in events:
        etype    = event.get('type')
        uid      = event.get('source', {}).get('userId', '')
        rtok     = event.get('replyToken', '')

        if etype == 'follow':
            upsert_line_user(uid)
            reply_line(rtok, [flex_welcome()])

        elif etype == 'message' and event.get('message', {}).get('type') == 'text':
            _handle_line_text(uid, rtok, event['message']['text'].strip())

    return 'OK', 200


# ─────────────────────────────────────────────
# 預約對話流程 Flex Messages
# ─────────────────────────────────────────────

def _sess(lu) -> dict:
    """取得使用者的 booking session，沒有就回空 dict"""
    try:
        return json.loads(lu.booking_session) if lu.booking_session else {}
    except Exception:
        return {}


def _save_sess(lu, data: dict):
    lu.booking_session = json.dumps(data, ensure_ascii=False)
    db.session.commit()


def _clear_sess(lu):
    lu.booking_session = None
    db.session.commit()


def flex_select_room(rooms) -> dict:
    """Step 1：選擇會議室 Flex（每間房間一個按鈕）"""
    room_btns = []
    for r in rooms:
        if r.capacity_min and r.capacity_min > 0:
            cap = f'{r.capacity_min}–{r.capacity} 人  ·  NT${r.hourly_rate}/hr'
        else:
            cap = f'{r.capacity} 人  ·  NT${r.hourly_rate}/hr'
        floor_txt = f'{r.floor}  ' if r.floor else ''
        room_btns.append({
            'type': 'box', 'layout': 'vertical',
            'backgroundColor': '#FFFFFF',
            'paddingAll': '14px',
            'margin': 'sm',
            'action': {'type': 'message', 'label': r.name,
                       'text': f'選房間 {r.id}'},
            'contents': [
                {'type': 'text', 'text': r.name, 'size': 'md',
                 'weight': 'bold', 'color': _C['ink']},
                {'type': 'text',
                 'text': f'{floor_txt}{r.room_type}  ·  {cap}',
                 'size': 'xs', 'color': _C['ink60'], 'margin': 'xs', 'wrap': True},
                *([{'type': 'text', 'text': r.description[:40] + ('…' if len(r.description or '') > 40 else ''),
                    'size': 'xs', 'color': _C['ink60'], 'margin': 'xs', 'wrap': True}]
                  if r.description else []),
            ]
        })

    return {
        'type': 'flex', 'altText': '請選擇會議室',
        'contents': {
            'type': 'bubble', 'size': 'mega',
            'header': _header_box('預約會議室', 'Step 1 / 5  ·  選擇會議室'),
            'body': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'],
                'paddingAll': '12px', 'spacing': 'none',
                'contents': room_btns or [
                    {'type': 'text', 'text': '目前沒有可用的會議室',
                     'color': _C['ink60'], 'size': 'sm'}
                ]
            },
            'footer': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'], 'paddingAll': '12px',
                'contents': [_btn('取消預約', 'message', '取消預約', bg='#888888')]
            }
        }
    }


def flex_input_date(room_name: str) -> dict:
    """Step 2：輸入日期"""
    from datetime import datetime as _dt, timedelta
    today = _dt.now()
    # 快捷日期：明天 / 後天 / 大後天
    shortcuts = []
    for delta in [1, 2, 3]:
        d = today + timedelta(days=delta)
        weekdays = ['一','二','三','四','五','六','日']
        label = f'{d.month}/{d.day} 週{weekdays[d.weekday()]}'
        shortcuts.append({
            'type': 'box', 'layout': 'vertical',
            'backgroundColor': '#FFFFFF', 'cornerRadius': '8px',
            'paddingAll': '10px', 'flex': 1,
            'action': {'type': 'message', 'label': label,
                       'text': d.strftime('%Y-%m-%d')},
            'contents': [
                {'type': 'text', 'text': label, 'size': 'sm',
                 'align': 'center', 'color': _C['teal'], 'weight': 'bold'},
            ]
        })

    return {
        'type': 'flex', 'altText': '請輸入日期',
        'contents': {
            'type': 'bubble', 'size': 'mega',
            'header': _header_box(f'{room_name}', 'Step 2 / 5  ·  選擇日期'),
            'body': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'],
                'paddingAll': '16px', 'spacing': 'md',
                'contents': [
                    {'type': 'text',
                     'text': '請輸入日期，例：2026-03-15',
                     'size': 'sm', 'color': _C['ink60'], 'wrap': True},
                    {'type': 'box', 'layout': 'horizontal',
                     'spacing': 'sm', 'contents': shortcuts},
                ]
            },
            'footer': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'], 'paddingAll': '12px',
                'contents': [_btn('取消預約', 'message', '取消預約', bg='#888888')]
            }
        }
    }


def flex_select_slot(room_name: str, date_str: str,
                     booked: list, room_id: int) -> dict:
    """Step 3：選擇時段（顯示可用 / 已占用）"""
    from datetime import datetime as _dt
    try:
        d = _dt.strptime(date_str, '%Y-%m-%d')
        weekdays = ['一','二','三','四','五','六','日']
        date_fmt = f'{d.month}/{d.day} 週{weekdays[d.weekday()]}'
    except Exception:
        date_fmt = date_str

    # 把已占用時段展開成 slot set
    blocked = set()
    for b in booked:
        sh, sm = map(int, b['start'].split(':'))
        eh, em = map(int, b['end'].split(':'))
        s_idx = (sh * 60 + sm - 8 * 60) // 30
        e_idx = (eh * 60 + em - 8 * 60) // 30
        for i in range(s_idx, e_idx):
            blocked.add(i)

    # 產生時段按鈕（以小時為單位，8:00~22:00 = 14 個整點）
    slot_rows = []
    for h in range(8, 21):
        s_idx = (h - 8) * 2
        e_idx = s_idx + 2
        is_blocked = any(i in blocked for i in range(s_idx, e_idx))
        start_t = f'{h:02d}:00'
        end_t   = f'{h+1:02d}:00'
        label   = f'{start_t} – {end_t}'
        if is_blocked:
            slot_rows.append({
                'type': 'box', 'layout': 'horizontal',
                'paddingTop': '8px', 'paddingBottom': '8px',
                'contents': [
                    {'type': 'text', 'text': label, 'size': 'sm',
                     'color': '#AAAAAA', 'flex': 3},
                    {'type': 'box', 'layout': 'vertical',
                     'backgroundColor': '#DDDDDD', 'cornerRadius': '10px',
                     'paddingTop': '2px', 'paddingBottom': '2px',
                     'paddingStart': '8px', 'paddingEnd': '8px', 'flex': 0,
                     'contents': [{'type': 'text', 'text': '已預約',
                                   'size': 'xxs', 'color': '#888888'}]},
                ]
            })
        else:
            slot_rows.append({
                'type': 'box', 'layout': 'horizontal',
                'paddingTop': '8px', 'paddingBottom': '8px',
                'action': {'type': 'message', 'label': label,
                           'text': f'選時段 {start_t} {end_t}'},
                'contents': [
                    {'type': 'text', 'text': label, 'size': 'sm',
                     'color': _C['teal'], 'weight': 'bold', 'flex': 3},
                    {'type': 'box', 'layout': 'vertical',
                     'backgroundColor': _C['teal'], 'cornerRadius': '10px',
                     'paddingTop': '2px', 'paddingBottom': '2px',
                     'paddingStart': '8px', 'paddingEnd': '8px', 'flex': 0,
                     'contents': [{'type': 'text', 'text': '可預約',
                                   'size': 'xxs', 'color': '#FFFFFF'}]},
                ]
            })

    return {
        'type': 'flex', 'altText': f'{date_fmt} 可用時段',
        'contents': {
            'type': 'bubble', 'size': 'mega',
            'header': _header_box(f'{date_fmt}  ·  {room_name}',
                                  'Step 3 / 5  ·  選擇時段（點選可預約時段）'),
            'body': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'],
                'paddingAll': '12px', 'spacing': 'none',
                'contents': slot_rows,
            },
            'footer': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'], 'paddingAll': '12px',
                'contents': [_btn('取消預約', 'message', '取消預約', bg='#888888')]
            }
        }
    }


def flex_input_name() -> dict:
    """Step 4a：輸入姓名"""
    return {
        'type': 'flex', 'altText': '請輸入聯絡人姓名',
        'contents': {
            'type': 'bubble', 'size': 'kilo',
            'header': _header_box('聯絡人資料', 'Step 4 / 5  ·  請填寫資料'),
            'body': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'], 'paddingAll': '16px',
                'spacing': 'sm',
                'contents': [
                    {'type': 'text', 'text': '請輸入您的姓名',
                     'size': 'sm', 'color': _C['ink'], 'weight': 'bold'},
                    {'type': 'text', 'text': '例：王小明',
                     'size': 'xs', 'color': _C['ink60']},
                ]
            },
            'footer': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'], 'paddingAll': '12px',
                'contents': [_btn('取消預約', 'message', '取消預約', bg='#888888')]
            }
        }
    }


def flex_input_phone() -> dict:
    """Step 4b：輸入手機"""
    return {
        'type': 'flex', 'altText': '請輸入手機號碼',
        'contents': {
            'type': 'bubble', 'size': 'kilo',
            'header': _header_box('聯絡人資料', 'Step 4 / 5  ·  手機號碼'),
            'body': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'], 'paddingAll': '16px',
                'spacing': 'sm',
                'contents': [
                    {'type': 'text', 'text': '請輸入手機號碼',
                     'size': 'sm', 'color': _C['ink'], 'weight': 'bold'},
                    {'type': 'text', 'text': '例：0912345678',
                     'size': 'xs', 'color': _C['ink60']},
                ]
            },
            'footer': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'], 'paddingAll': '12px',
                'contents': [_btn('取消預約', 'message', '取消預約', bg='#888888')]
            }
        }
    }


def flex_input_email() -> dict:
    """Step 4c：輸入 Email"""
    return {
        'type': 'flex', 'altText': '請輸入 Email',
        'contents': {
            'type': 'bubble', 'size': 'kilo',
            'header': _header_box('聯絡人資料', 'Step 4 / 5  ·  Email'),
            'body': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'], 'paddingAll': '16px',
                'spacing': 'sm',
                'contents': [
                    {'type': 'text', 'text': '請輸入 Email',
                     'size': 'sm', 'color': _C['ink'], 'weight': 'bold'},
                    {'type': 'text', 'text': '預約確認信將發送至此信箱',
                     'size': 'xs', 'color': _C['ink60']},
                    {'type': 'text', 'text': '例：name@example.com',
                     'size': 'xs', 'color': _C['ink60']},
                ]
            },
            'footer': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'], 'paddingAll': '12px',
                'contents': [_btn('取消預約', 'message', '取消預約', bg='#888888')]
            }
        }
    }


def flex_confirm_booking(sess: dict) -> dict:
    """Step 5：確認預約資料"""
    room_name = sess.get('room_name', '—')
    date_str  = sess.get('date', '—')
    start_t   = sess.get('start_time', '—')
    end_t     = sess.get('end_time', '—')
    name      = sess.get('name', '—')
    phone     = sess.get('phone', '—')
    email     = sess.get('email', '—')
    try:
        from datetime import datetime as _dt
        d = _dt.strptime(date_str, '%Y-%m-%d')
        weekdays = ['一','二','三','四','五','六','日']
        date_fmt = f'{d.month}/{d.day} 週{weekdays[d.weekday()]}'
    except Exception:
        date_fmt = date_str

    # 計算金額
    try:
        sh, sm = map(int, start_t.split(':'))
        eh, em = map(int, end_t.split(':'))
        dur = (eh * 60 + em - sh * 60 - sm) / 60
        price = int(dur * sess.get('hourly_rate', 0))
        price_txt = f'NT$ {price:,}  /{  _fmt_duration(dur)}'
    except Exception:
        price_txt = '—'

    return {
        'type': 'flex', 'altText': '請確認預約資料',
        'contents': {
            'type': 'bubble', 'size': 'mega',
            'header': _header_box('確認預約資料', 'Step 5 / 5  ·  請確認以下資訊'),
            'body': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'],
                'paddingAll': '16px', 'spacing': 'sm',
                'contents': [
                    _row('會議室', room_name),
                    _row('日期',   date_fmt),
                    _row('時段',   f'{start_t} – {end_t}'),
                    _row('聯絡人', name),
                    _row('手機',   phone),
                    _row('Email',  email),
                    _divider(),
                    _row('費用',   price_txt),
                ]
            },
            'footer': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': _C['bg'], 'paddingAll': '12px',
                'spacing': 'sm',
                'contents': [
                    _btn('確認送出', 'message', '確認送出預約'),
                    _btn('取消預約', 'message', '取消預約', bg='#888888'),
                ]
            }
        }
    }


# ─────────────────────────────────────────────
# 預約對話流程主控制器
# ─────────────────────────────────────────────

def _handle_booking_flow(uid: str, rtok: str, text: str, lu):
    """
    回傳 True 表示訊息已被預約流程處理，False 表示不在流程中
    """
    import re as _re
    sess = _sess(lu)
    step = sess.get('step', '')

    # ── 全域指令：不論 session 狀態都不攔截，交給一般 handler ──
    GLOBAL_CMDS = ('說明', 'help', '指令', '?', '？', '選單', 'menu',
                   '我的預約', '預約紀錄', '時段', '查詢', '綁定')
    lower_t = text.lower()
    if lower_t in GLOBAL_CMDS or any(lower_t.startswith(c) for c in GLOBAL_CMDS):
        # 如果在流程中，先清除 session 再讓一般 handler 處理
        if step:
            _clear_sess(lu)
        return False

    # ── 取消 ──
    if text in ('取消預約', '取消', 'cancel'):
        if step:
            _clear_sess(lu)
            reply_line(rtok, [flex_not_found('已取消預約流程', '如需重新預約，請點選「預約」')])
            return True
        return False  # 不在流程中，交給一般指令處理

    # ── Step 0：開始預約 → 顯示會議室列表 ──
    if text in ('預約', '開始預約', '我要預約'):
        rooms = Room.query.filter_by(is_active=True).all()
        if not rooms:
            reply_line(rtok, [flex_not_found('目前沒有可用的會議室', '請稍後再試')])
            return True
        _save_sess(lu, {'step': 'select_room'})
        reply_line(rtok, [flex_select_room(rooms)])
        return True

    # ── Step 1：已選會議室 ──
    if step == 'select_room':
        m = _re.match(r'^選房間 (\d+)$', text)
        if not m:
            rooms = Room.query.filter_by(is_active=True).all()
            reply_line(rtok, [flex_select_room(rooms)])
            return True
        room_id = int(m.group(1))
        room = Room.query.get(room_id)
        if not room or not room.is_active:
            reply_line(rtok, [flex_not_found('找不到此會議室', '請重新選擇')])
            return True
        sess = {'step': 'select_date', 'room_id': room_id,
                'room_name': room.name, 'hourly_rate': room.hourly_rate}
        _save_sess(lu, sess)
        reply_line(rtok, [flex_input_date(room.name)])
        return True

    # ── Step 2：輸入日期 ──
    if step == 'select_date':
        # 解析日期
        date_str = None
        m8 = _re.match(r'^(\d{4})[/-](\d{1,2})[/-](\d{1,2})$', text)
        m4 = _re.match(r'^(\d{1,2})[/-](\d{1,2})$', text)
        if m8:
            date_str = f'{m8.group(1)}-{int(m8.group(2)):02d}-{int(m8.group(3)):02d}'
        elif m4:
            year = datetime.now().year
            date_str = f'{year}-{int(m4.group(1)):02d}-{int(m4.group(2)):02d}'
        if not date_str:
            reply_line(rtok, [flex_input_date(sess.get('room_name', ''))])
            return True
        # 不能選過去
        try:
            from datetime import datetime as _dt, date as _date
            chosen = _dt.strptime(date_str, '%Y-%m-%d').date()
            if chosen < _date.today():
                reply_line(rtok, [flex_not_found('不能選擇過去的日期', '請重新輸入日期')])
                return True
        except Exception:
            reply_line(rtok, [flex_input_date(sess.get('room_name', ''))])
            return True

        booked = get_booked_slots(sess['room_id'], date_str)
        sess['step']  = 'select_slot'
        sess['date']  = date_str
        _save_sess(lu, sess)
        reply_line(rtok, [flex_select_slot(
            sess['room_name'], date_str, booked, sess['room_id'])])
        return True

    # ── Step 3：選時段 ──
    if step == 'select_slot':
        m = _re.match(r'^選時段 (\d{2}:\d{2}) (\d{2}:\d{2})$', text)
        if not m:
            booked = get_booked_slots(sess['room_id'], sess['date'])
            reply_line(rtok, [flex_select_slot(
                sess['room_name'], sess['date'], booked, sess['room_id'])])
            return True
        start_t, end_t = m.group(1), m.group(2)
        # 即時衝突檢查
        if not check_availability(sess['room_id'], sess['date'], start_t, end_t):
            booked = get_booked_slots(sess['room_id'], sess['date'])
            reply_line(rtok, [
                flex_not_found('此時段已被預約', '請選擇其他時段'),
                flex_select_slot(sess['room_name'], sess['date'], booked, sess['room_id'])
            ])
            return True
        sess['step']       = 'input_name'
        sess['start_time'] = start_t
        sess['end_time']   = end_t
        _save_sess(lu, sess)
        reply_line(rtok, [flex_input_name()])
        return True

    # ── Step 4a：輸入姓名 ──
    if step == 'input_name':
        if not text.strip():
            reply_line(rtok, [flex_input_name()])
            return True
        sess['step'] = 'input_phone'
        sess['name'] = text.strip()
        # 若已綁定手機，自動帶入
        if lu.phone:
            sess['step']  = 'input_email'
            sess['phone'] = lu.phone
            _save_sess(lu, sess)
            reply_line(rtok, [flex_input_email()])
        else:
            _save_sess(lu, sess)
            reply_line(rtok, [flex_input_phone()])
        return True

    # ── Step 4b：輸入手機 ──
    if step == 'input_phone':
        phone = text.strip().replace('-', '').replace(' ', '')
        if not phone.isdigit() or len(phone) < 8:
            reply_line(rtok, [flex_not_found('手機號碼格式不正確', '請輸入 10 碼手機號碼，例：0912345678')])
            return True
        sess['step']  = 'input_email'
        sess['phone'] = phone
        _save_sess(lu, sess)
        reply_line(rtok, [flex_input_email()])
        return True

    # ── Step 4c：輸入 Email ──
    if step == 'input_email':
        import re as _re2
        if not _re2.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$', text.strip()):
            reply_line(rtok, [flex_not_found('Email 格式不正確', '請重新輸入，例：name@example.com')])
            return True
        sess['step']  = 'confirm'
        sess['email'] = text.strip()
        _save_sess(lu, sess)
        reply_line(rtok, [flex_confirm_booking(sess)])
        return True

    # ── Step 5：確認送出 ──
    if step == 'confirm' and text == '確認送出預約':
        # 最終衝突再確認（防止兩人同時搶同一時段）
        if not check_availability(sess['room_id'], sess['date'],
                                  sess['start_time'], sess['end_time']):
            _clear_sess(lu)
            reply_line(rtok, [flex_not_found(
                '很抱歉，此時段剛被其他人預約',
                '請重新開始預約，輸入「預約」繼續')])
            return True

        # 計算費用
        sh, sm = map(int, sess['start_time'].split(':'))
        eh, em = map(int, sess['end_time'].split(':'))
        dur   = (eh * 60 + em - sh * 60 - sm) / 60
        price = int(dur * sess.get('hourly_rate', 0))

        # 建立預約
        booking = Booking(
            booking_number  = generate_booking_number(),
            room_id         = sess['room_id'],
            customer_name   = sess['name'],
            customer_phone  = sess['phone'],
            customer_email  = sess['email'],
            department      = '',
            date            = sess['date'],
            start_time      = sess['start_time'],
            end_time        = sess['end_time'],
            duration        = dur,
            total_price     = price,
            attendees       = 1,
            purpose         = 'LINE 預約',
            note            = '',
            line_user_id    = uid,
        )
        db.session.add(booking)
        # 綁定手機
        lu.phone = sess['phone']
        db.session.commit()
        booking = Booking.query.get(booking.id)
        _clear_sess(lu)

        # 通知
        push_line(uid, [flex_booking_confirm(booking)])
        for aid in admin_line_ids():
            push_line(aid, [flex_admin_notify(booking)])
        if booking.customer_email:
            send_email(booking.customer_email,
                       f'【預約確認】{booking.room.name} – {booking.date}',
                       _booking_email_html(booking))
        send_sms(booking.customer_phone, _booking_sms_body(booking))
        return True

    return False  # 不在流程中


def _handle_line_text(uid, rtok, text):
    lower = text.lower()
    lu = upsert_line_user(uid)

    # ── 預約對話流程攔截（最高優先）──
    if _handle_booking_flow(uid, rtok, text, lu):
        return

    # ── 說明 / 主選單 ──
    if lower in ('說明', 'help', '指令', '?', '？', '選單', 'menu'):
        reply_line(rtok, [flex_main_menu()])
        return

    # ── 查詢預約編號 ──
    if lower.startswith('查詢'):
        number = text[2:].strip().upper()
        if not number:
            reply_line(rtok, [flex_not_found(
                '請輸入預約編號',
                '範例：查詢 MR2026030100001')])
            return
        b = Booking.query.filter_by(booking_number=number).first()
        if not b:
            reply_line(rtok, [flex_not_found(
                f'找不到預約編號 {number}',
                '請確認編號是否正確，或前往網站查詢。')])
        else:
            reply_line(rtok, [flex_booking_confirm(b)])
        return

    # ── 我的預約 ──
    if lower in ('我的預約', '預約紀錄'):
        # 優先用 line_user_id 查，再 fallback 到綁定手機號碼
        q_uid   = Booking.query.filter_by(line_user_id=uid)
        q_phone = (Booking.query.filter_by(customer_phone=lu.phone)
                   if lu and lu.phone else None)
        # 合併兩個來源（去重）
        seen, bs = set(), []
        for b in (q_uid.order_by(Booking.created_at.desc()).limit(10).all()):
            if b.id not in seen:
                seen.add(b.id); bs.append(b)
        if q_phone:
            for b in q_phone.order_by(Booking.created_at.desc()).limit(10).all():
                if b.id not in seen:
                    seen.add(b.id); bs.append(b)
        # 取最新 3 筆
        bs = sorted(bs, key=lambda b: b.created_at or b.id, reverse=True)[:3]
        if not bs:
            hint = '前往網站預約，或在 LINE 輸入「預約」開始。'
            if not (lu and lu.phone):
                hint = '也可輸入「綁定 0912345678」連結網頁預約紀錄。'
            reply_line(rtok, [flex_not_found('目前沒有預約紀錄', hint)])
        else:
            reply_line(rtok, [flex_booking_confirm(b) for b in bs])
        return

    # ── 時段查詢 ──
    if lower.startswith('時段'):
        import re as _re
        raw_date = text[2:].strip()
        date_str = None
        m8 = _re.match(r'^(\d{4})[/-]?(\d{2})[/-]?(\d{2})$', raw_date)
        m4 = _re.match(r'^(\d{1,2})[/-](\d{1,2})$', raw_date)
        if m8:
            date_str = f'{m8.group(1)}-{m8.group(2)}-{m8.group(3)}'
        elif m4:
            from datetime import datetime as _dt
            year = _dt.now().year
            date_str = f'{year}-{int(m4.group(1)):02d}-{int(m4.group(2)):02d}'
        if not date_str:
            reply_line(rtok, [flex_not_found(
                '日期格式不正確',
                '請輸入：時段 2026-03-15  或  時段 3/15')])
            return
        rooms = Room.query.filter_by(is_active=True).all()
        rooms_data = []
        for room in rooms:
            slots = get_booked_slots(room.id, date_str)
            rooms_data.append({'name': room.name, 'slots': slots})
        reply_line(rtok, [flex_timeslot(date_str, rooms_data)])
        return

    # ── 取消特定預約：取消預約 MR2026XXXXXX ──
    if lower.startswith('取消預約 '):
        number = text[5:].strip().upper()
        b = Booking.query.filter_by(booking_number=number).first()
        if not b:
            reply_line(rtok, [flex_not_found(
                f'找不到預約編號 {number}',
                '請確認編號是否正確')])
            return
        # 確認是本人的預約（by line_user_id 或綁定手機）
        is_owner = (b.line_user_id == uid or
                    (lu and lu.phone and b.customer_phone == lu.phone))
        if not is_owner:
            reply_line(rtok, [flex_not_found(
                '無法取消此預約',
                '只能取消您自己的預約')])
            return
        if b.status == 'cancelled':
            reply_line(rtok, [flex_not_found(
                '此預約已取消',
                '如有疑問請聯繫管理員')])
            return
        # 時間限制：距使用不足 2 小時
        from datetime import datetime as _dt
        try:
            booking_dt = _dt.strptime(f"{b.date} {b.start_time}", '%Y-%m-%d %H:%M')
            if (booking_dt - _dt.now()).total_seconds() < 7200:
                reply_line(rtok, [flex_not_found(
                    '距離使用時間不足 2 小時',
                    '請直接聯繫管理員處理')])
                return
        except Exception:
            pass
        b.status = 'cancelled'
        db.session.commit()
        push_line(uid, [flex_booking_cancel(b)])
        for aid in admin_line_ids():
            push_line(aid, [flex_admin_notify(b)])
        return

    # ── 綁定手機 ──
    if lower.startswith('綁定'):
        phone = text[2:].strip().replace('-', '').replace(' ', '')
        if not phone.isdigit() or len(phone) < 8:
            reply_line(rtok, [flex_not_found(
                '手機號碼格式不正確',
                '請輸入：綁定 0912345678')])
            return
        lu.phone = phone
        Booking.query.filter_by(customer_phone=phone, line_user_id=None).update(
            {'line_user_id': uid})
        db.session.commit()
        reply_line(rtok, [flex_bind_success(phone)])
        return

    # ── 未識別指令 → 引導到主選單 ──
    reply_line(rtok, [flex_main_menu()])


# ─────────────────────────────────────────────
# Admin Login
# ─────────────────────────────────────────────

@app.route('/admin/api/login', methods=['POST'])
def admin_login():
    data  = request.get_json()
    uname = data.get('username', 'admin').strip()
    pw    = data.get('password', '').strip()
    ip    = get_client_ip()
    ua    = request.headers.get('User-Agent', '')[:300]

    def _log(success, note=''):
        try:
            country, city = ('', '')
            if success:
                country, city = get_ip_location(ip)
            log = AdminLoginLog(username=uname, success=success,
                                ip_address=ip, country=country, city=city,
                                user_agent=ua, note=note)
            db.session.add(log)
            db.session.commit()
        except Exception as e:
            print(f'[login log error] {e}')

    user = AdminUser.query.filter_by(username=uname).first()
    if user and user.is_active and user.check_password(pw):
        session['admin_user_id'] = user.id
        session['admin_username'] = user.username
        session['admin_role'] = user.role
        _log(True)
        return jsonify({'success': True, 'user': user.to_dict()})

    if uname == 'admin' and pw == ADMIN_PASSWORD:
        session['admin_user_id'] = 0
        session['admin_username'] = 'admin'
        session['admin_role'] = 'superadmin'
        _log(True, '舊式密碼')
        return jsonify({'success': True})

    _log(False, '密碼錯誤')
    return jsonify({'error': '帳號或密碼錯誤'}), 401


@app.route('/admin/api/logout', methods=['POST'])
def admin_api_logout():
    session.clear()
    return jsonify({'success': True})


@app.route('/admin/api/me', methods=['GET'])
def admin_me():
    err = check_admin()
    if err: return err
    u = get_current_admin()
    if u:
        return jsonify(u.to_dict())
    return jsonify({'username': 'admin', 'role': 'superadmin',
                    'permissions': ['dashboard','bookings','rooms','content',
                                    'photos','formfields','blocked','accounts','logs']})


# ─────────────────────────────────────────────
# Admin — Rooms
# ─────────────────────────────────────────────

@app.route('/admin/api/floor-status')
def admin_floor_status():
    err = check_admin()
    if err: return err
    from datetime import datetime as _dt
    date_str = request.args.get('date', _dt.now().strftime('%Y-%m-%d'))
    rooms = Room.query.filter_by(is_active=True).order_by(Room.floor, Room.name).all()
    result = []
    for r in rooms:
        import json as _json
        bookings = Booking.query.filter_by(
            room_id=r.id, date=date_str).filter(
            Booking.status.in_(['confirmed', 'completed'])).all()
        occupied = [False] * 28
        for b in bookings:
            # 展開 segments（多段時段）
            segs = []
            if b.segments:
                try: segs = _json.loads(b.segments)
                except Exception: pass
            if not segs:
                segs = [{'start': b.start_time, 'end': b.end_time}]
            for seg in segs:
                sh, sm = map(int, seg['start'].split(':'))
                eh, em = map(int, seg['end'].split(':'))
                si = (sh * 60 + sm - 480) // 30
                ei = (eh * 60 + em - 480) // 30
                for i in range(max(0, si), min(28, ei)):
                    occupied[i] = True
        result.append({
            'id': r.id, 'name': r.name,
            'floor': r.floor or '未分層',
            'capacity': r.capacity,
            'room_type': r.room_type or '',
            'slots': occupied,
            'bookings': [{'start': b.start_time, 'end': b.end_time,
                          'segments': b.segments,
                          'name': b.customer_name,
                          'number': b.booking_number} for b in bookings]
        })
    return jsonify({'date': date_str, 'rooms': result})


@app.route('/admin/api/rooms', methods=['GET'])
def admin_get_rooms():
    err = check_admin()
    if err: return err
    return jsonify([r.to_dict() for r in Room.query.order_by(Room.created_at.desc()).all()])

@app.route('/admin/api/rooms', methods=['POST'])
def admin_add_room():
    err = check_admin()
    if err: return err
    d = request.get_json()
    r = Room(name=d['name'], room_type=d['room_type'],
             capacity=d.get('capacity', 10), capacity_min=d.get('capacity_min', 0),
             hourly_rate=d.get('hourly_rate', 500),
             description=d.get('description',''),
             amenities=json.dumps(d.get('amenities',[]), ensure_ascii=False),
             floor=d.get('floor',''), photo_url=d.get('photo_url',''),
             is_active=d.get('is_active', True))
    db.session.add(r)
    db.session.commit()
    return jsonify(r.to_dict()), 201

@app.route('/admin/api/rooms/<int:rid>', methods=['PUT'])
def admin_update_room(rid):
    err = check_admin()
    if err: return err
    room = Room.query.get_or_404(rid)
    d = request.get_json()
    for f in ['name','room_type','capacity','capacity_min','hourly_rate','description',
              'floor','photo_url','is_active']:
        if f in d:
            setattr(room, f, d[f])
    if 'amenities' in d:
        room.amenities = json.dumps(d['amenities'], ensure_ascii=False)
    db.session.commit()
    return jsonify(room.to_dict())

@app.route('/admin/api/rooms/<int:rid>', methods=['DELETE'])
def admin_delete_room(rid):
    err = check_admin()
    if err: return err
    room = Room.query.get_or_404(rid)
    room.is_active = False
    db.session.commit()
    return jsonify({'success': True})


# ─────────────────────────────────────────────
# Admin — Photo Upload
# ─────────────────────────────────────────────

@app.route('/admin/api/rooms/<int:rid>/photos', methods=['GET'])
def admin_get_room_photos(rid):
    err = check_admin(); 
    if err: return err
    r = Room.query.get_or_404(rid)
    return jsonify({'photos': r.get_photos(), 'cover_index': r.cover_index or 0})

@app.route('/admin/api/rooms/<int:rid>/photos', methods=['POST'])
def admin_add_room_photo(rid):
    """上傳照片並加入 room.photos（最多5張）"""
    err = check_admin()
    if err: return err
    r = Room.query.get_or_404(rid)
    if 'photo' not in request.files:
        return jsonify({'error': '未選擇檔案'}), 400
    f = request.files['photo']
    if f.filename == '' or not allowed_file(f.filename):
        return jsonify({'error': '不支援的檔案格式'}), 400
    # 上傳
    if USE_CLOUDINARY:
        url = _upload_to_cloudinary(f)
        if not url:
            return jsonify({'error': 'Cloudinary 上傳失敗'}), 500
    else:
        ext = f.filename.rsplit('.', 1)[1].lower()
        filename = f'{uuid.uuid4().hex}.{ext}'
        f.save(os.path.join(UPLOAD_FOLDER, filename))
        url = f'/static/uploads/{filename}'
    photos = r.get_photos()
    if len(photos) >= 5:
        return jsonify({'error': '最多只能上傳 5 張照片'}), 400
    photos.append(url)
    r.photos = json.dumps(photos, ensure_ascii=False)
    r.photo_url = r.get_cover()
    db.session.commit()
    return jsonify({'success': True, 'photos': photos, 'cover_index': r.cover_index or 0})

@app.route('/admin/api/rooms/<int:rid>/photos/<int:idx>', methods=['DELETE'])
def admin_delete_room_photo(rid, idx):
    """刪除指定索引的照片"""
    err = check_admin()
    if err: return err
    r = Room.query.get_or_404(rid)
    photos = r.get_photos()
    if idx < 0 or idx >= len(photos):
        return jsonify({'error': '無效的照片索引'}), 400
    photos.pop(idx)
    cover = r.cover_index or 0
    if cover >= len(photos):
        cover = 0
    r.photos = json.dumps(photos, ensure_ascii=False)
    r.cover_index = cover
    r.photo_url = photos[cover] if photos else ''
    db.session.commit()
    return jsonify({'success': True, 'photos': photos, 'cover_index': cover})

@app.route('/admin/api/rooms/<int:rid>/photos/cover', methods=['PUT'])
def admin_set_cover_photo(rid):
    """設定主封面（傳 index）"""
    err = check_admin()
    if err: return err
    r = Room.query.get_or_404(rid)
    data = request.get_json()
    idx = data.get('index', 0)
    photos = r.get_photos()
    if idx < 0 or idx >= len(photos):
        return jsonify({'error': '無效的索引'}), 400
    r.cover_index = idx
    r.photo_url = photos[idx]
    db.session.commit()
    return jsonify({'success': True, 'cover_index': idx})

@app.route('/admin/api/site-logo', methods=['POST'])
def admin_upload_logo():
    """上傳 Logo 圖片，儲存至 SiteContent"""
    err = check_admin()
    if err: return err
    if 'photo' not in request.files:
        return jsonify({'error': '未選擇檔案'}), 400
    f = request.files['photo']
    if f.filename == '' or not allowed_file(f.filename):
        return jsonify({'error': '不支援的檔案格式'}), 400
    if USE_CLOUDINARY:
        url = _upload_to_cloudinary(f)
        if not url:
            return jsonify({'error': 'Cloudinary 上傳失敗'}), 500
    else:
        ext = f.filename.rsplit('.', 1)[1].lower()
        filename = f'{uuid.uuid4().hex}.{ext}'
        f.save(os.path.join(UPLOAD_FOLDER, filename))
        url = f'/static/uploads/{filename}'
    SiteContent.query.filter_by(key='logo_url').delete()
    db.session.add(SiteContent(key='logo_url', value=url))
    db.session.commit()
    return jsonify({'success': True, 'logo_url': url})

@app.route('/admin/api/upload-photo', methods=['POST'])
def upload_photo():
    err = check_admin()
    if err: return err
    if 'photo' not in request.files:
        return jsonify({'error': '未選擇檔案'}), 400
    f = request.files['photo']
    if f.filename == '' or not allowed_file(f.filename):
        return jsonify({'error': '不支援的檔案格式（支援 PNG/JPG/GIF/WEBP）'}), 400

    if USE_CLOUDINARY:
        # ── 上傳至 Cloudinary ──
        photo_url = _upload_to_cloudinary(f)
        if not photo_url:
            return jsonify({'error': 'Cloudinary 上傳失敗，請確認設定'}), 500
    else:
        # ── 存本地 ──
        ext      = f.filename.rsplit('.', 1)[1].lower()
        filename = f'{uuid.uuid4().hex}.{ext}'
        f.save(os.path.join(UPLOAD_FOLDER, filename))
        photo_url = f'/static/uploads/{filename}'

    return jsonify({'success': True, 'photo_url': photo_url})


def _upload_to_cloudinary(file_storage) -> str:
    """上傳檔案至 Cloudinary，回傳安全 URL；失敗回傳空字串"""
    import hmac as _hmac, hashlib as _hashlib, time
    timestamp = str(int(time.time()))
    folder    = 'meeting_rooms'
    # 簽章
    params_to_sign = f'folder={folder}&timestamp={timestamp}'
    sig = _hashlib.sha1(
        (params_to_sign + CLOUDINARY_API_SECRET).encode()
    ).hexdigest()

    upload_url = f'https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/image/upload'
    try:
        resp = http_requests.post(upload_url, data={
            'api_key':   CLOUDINARY_API_KEY,
            'timestamp': timestamp,
            'folder':    folder,
            'signature': sig,
        }, files={'file': (file_storage.filename, file_storage.stream, file_storage.mimetype)},
        timeout=30)
        data = resp.json()
        return data.get('secure_url', '')
    except Exception as e:
        print(f'[Cloudinary error] {e}')
        return ''


# ─────────────────────────────────────────────
# Admin — Bookings
# ─────────────────────────────────────────────

@app.route('/admin/api/bookings', methods=['GET'])
def admin_get_bookings():
    err = check_admin()
    if err: return err
    q = Booking.query
    if v := request.args.get('date'):    q = q.filter_by(date=v)
    if v := request.args.get('status'):  q = q.filter_by(status=v)
    if v := request.args.get('room_id'): q = q.filter_by(room_id=int(v))
    return jsonify([b.to_dict() for b in q.order_by(Booking.created_at.desc()).all()])

@app.route('/admin/api/bookings/<int:bid>/cancel', methods=['POST'])
def admin_cancel_booking(bid):
    err = check_admin()
    if err: return err
    b = Booking.query.get_or_404(bid)
    b.status = 'cancelled'
    db.session.commit()
    if b.line_user_id:
        push_line(b.line_user_id, [flex_booking_cancel(b)])
    # Email 取消通知
    if b.customer_email:
        send_email(
            b.customer_email,
            f'【預約取消】{b.room.name if b.room else ""} – {b.date}',
            _cancel_email_html(b)
        )
    # SMS 取消通知
    send_sms(b.customer_phone, _cancel_sms_body(b))
    return jsonify({'success': True})

@app.route('/admin/api/bookings/<int:bid>/complete', methods=['POST'])
def admin_complete_booking(bid):
    err = check_admin()
    if err: return err
    b = Booking.query.get_or_404(bid)
    b.status = 'completed'
    db.session.commit()
    return jsonify({'success': True})


# ─────────────────────────────────────────────
# Admin — LINE Users
# ─────────────────────────────────────────────

@app.route('/admin/api/line-users', methods=['GET'])
def admin_get_line_users():
    err = check_admin()
    if err: return err
    return jsonify([u.to_dict() for u in
                    LineUser.query.order_by(LineUser.created_at.desc()).all()])

@app.route('/admin/api/line-users/<uid>/admin', methods=['POST'])
def admin_toggle_line_admin(uid):
    err = check_admin()
    if err: return err
    lu = LineUser.query.filter_by(line_user_id=uid).first_or_404()
    lu.is_admin = not lu.is_admin
    db.session.commit()
    return jsonify(lu.to_dict())

@app.route('/admin/api/line-broadcast', methods=['POST'])
def admin_broadcast():
    """廣播文字訊息給所有（或僅管理員）LINE 使用者"""
    err = check_admin()
    if err: return err
    d       = request.get_json()
    msg     = d.get('message', '').strip()
    admonly = d.get('admins_only', False)
    if not msg:
        return jsonify({'error': '訊息不能為空'}), 400
    q = LineUser.query
    if admonly:
        q = q.filter_by(is_admin=True)
    users = q.all()
    for u in users:
        push_line(u.line_user_id, [{'type': 'text', 'text': msg}])
    return jsonify({'success': True, 'sent': len(users)})


# ─────────────────────────────────────────────
# Admin — Site Content
# ─────────────────────────────────────────────

@app.route('/admin/api/site-content', methods=['GET'])
def admin_get_site_content():
    err = check_admin()
    if err: return err
    data = {i.key: i.value for i in SiteContent.query.all()}
    if 'form_fields' not in data or not data['form_fields']:
        data['form_fields'] = '[{"id": "name", "label": "聯絡人姓名", "type": "text", "placeholder": "請輸入姓名", "required": true, "system": true, "full": false}, {"id": "phone", "label": "手機號碼", "type": "tel", "placeholder": "0912345678", "required": true, "system": true, "full": false}, {"id": "email", "label": "Email", "type": "email", "placeholder": "your@email.com", "required": true, "system": true, "full": false, "hint": "必填，接收確認信"}, {"id": "department", "label": "部門／公司", "type": "text", "placeholder": "例：行銷部", "required": false, "system": true, "full": false}, {"id": "attendees", "label": "預計出席人數", "type": "select", "options": "1,2,3,4,5,6,8,10,15,20,30,50", "required": false, "system": true, "full": false}, {"id": "purpose", "label": "會議類型", "type": "select", "options": "部門會議,客戶洽談,員工培訓,產品發表,視訊會議,腦力激盪,其他", "required": false, "system": true, "full": false}, {"id": "note", "label": "備註", "type": "textarea", "placeholder": "特殊需求或注意事項...", "required": false, "system": true, "full": true}]'
    return jsonify(data)

@app.route('/admin/api/site-content', methods=['POST'])
def admin_update_site_content():
    err = check_admin()
    if err: return err
    for k, v in request.get_json().items():
        SiteContent.set(k, v)
    return jsonify({'success': True})


# ─────────────────────────────────────────────
# Admin — Stats
# ─────────────────────────────────────────────

@app.route('/admin/api/stats', methods=['GET'])
def admin_get_stats():
    err = check_admin()
    if err: return err
    today = datetime.now().strftime('%Y-%m-%d')
    return jsonify({
        'total_bookings': Booking.query.filter_by(status='confirmed').count(),
        'today_bookings': Booking.query.filter_by(date=today, status='confirmed').count(),
        'total_rooms':    Room.query.filter_by(is_active=True).count(),
        'total_revenue':  db.session.query(func.sum(Booking.total_price))
                            .filter_by(status='confirmed').scalar() or 0,
        'cancelled':   Booking.query.filter_by(status='cancelled').count(),
        'completed':   Booking.query.filter_by(status='completed').count(),
        'line_users':  LineUser.query.count(),
    })


# ─────────────────────────────────────────────
# Seed
# ─────────────────────────────────────────────

DEFAULT_CONTENT = {
    'site_title':       '會議室預約系統',
    'site_subtitle':    '企業空間 · 即時預約',
    'site_description': '提供多種類型會議室，彈性時段預約，滿足各種商務需求。',
    'hero_badge':       '專業會議空間',
    'service_hours':    '週一至週五 08:00 – 22:00 ／ 週六 09:00 – 18:00',
    'contact_phone':    '02-1234-5678',
    'contact_email':    'booking@example.com',
    'site_url':         'https://seat-booking-rlf2.onrender.com',
    'notice_1': '請提前 15 分鐘辦理入場手續',
    'notice_2': '取消或更改請提前 2 小時通知',
    'notice_3': '禁止攜帶食物進入精緻會議室',
    'notice_4': '使用後請恢復設備原始設定',
    'notice_5': '逾時使用將依時薪計費',
    'footer_text': '© 2026 會議室預約系統 · 版權所有',
}

ROOM_SEED = [
    {'name':'創意腦力激盪室','room_type':'腦力激盪','capacity':8,'hourly_rate':600,
     'description':'開放式空間設計，配備白板牆面，激發創意思維。',
     'amenities':['白板牆','磁性貼紙','活動式座椅','投影機','WiFi','充電站'],'floor':'3F'},
    {'name':'精緻洽談室 A','room_type':'洽談室','capacity':4,'hourly_rate':400,
     'description':'私密安靜的小型洽談空間，皮革座椅搭配木質桌面。',
     'amenities':['螢幕共享','視訊攝影機','噪音隔絕','WiFi','白板','咖啡機'],'floor':'2F'},
    {'name':'大型簡報廳','room_type':'簡報廳','capacity':50,'hourly_rate':2000,
     'description':'專業簡報空間，配備劇院式座椅、雙螢幕投影、麥克風系統。',
     'amenities':['雙投影幕','麥克風系統','劇院座椅','燈光控制','錄影設備','舞台'],'floor':'2F'},
    {'name':'視訊會議中心','room_type':'視訊會議','capacity':12,'hourly_rate':1000,
     'description':'4K 攝影機搭配環繞音響，遠端與現場皆有絕佳體驗。',
     'amenities':['4K 攝影機','環繞音響','自動追蹤','雙顯示器','噪音抑制麥克風','WiFi 6'],'floor':'3F'},
    {'name':'主管行政套房','room_type':'行政套房','capacity':6,'hourly_rate':1500,
     'description':'頂層行政會議室，俯瞰城市景觀，適合董事會議、VIP 接待。',
     'amenities':['城市景觀','高端家具','私人衛浴','秘書服務','餐飲服務','私人停車'],'floor':'3F'},
    {'name':'多功能培訓教室','room_type':'培訓教室','capacity':30,'hourly_rate':1200,
     'description':'彈性空間配置，適合員工培訓、研討會、工作坊。',
     'amenities':['電子白板','個人顯示器','彈性座位','錄音設備','茶水站','停車場'],'floor':'2F'},
]


def seed():
    for k, v in DEFAULT_CONTENT.items():
        if not SiteContent.query.filter_by(key=k).first():
            db.session.add(SiteContent(key=k, value=v))
    if Room.query.count() == 0:
        for r in ROOM_SEED:
            db.session.add(Room(
                name=r['name'], room_type=r['room_type'],
                capacity=r['capacity'], hourly_rate=r['hourly_rate'],
                description=r['description'],
                amenities=json.dumps(r['amenities'], ensure_ascii=False),
                floor=r['floor'], is_active=True
            ))
    # 更新現有房間樓層（確保舊資料也套用正確樓層）
    floor_updates = {
        '多功能培訓教室': '2F',
        '精緻洽談室 A':   '2F',
        '大型簡報廳':     '2F',
        '視訊會議中心':   '3F',
        '行政套房':       '3F',
        '主管行政套房':   '3F',
        '創意腦力激盪室': '3F',
    }
    for name, floor in floor_updates.items():
        Room.query.filter_by(name=name).update({'floor': floor})
    # 兜底：把所有不是 2F/3F 的房間都改成 3F
    from sqlalchemy import not_, or_
    Room.query.filter(
        ~Room.floor.in_(['2F', '3F'])
    ).update({'floor': '3F'}, synchronize_session=False)
    db.session.commit()
    print('資料庫初始化完成')


with app.app_context():
    db.create_all()
    # ── 欄位遷移：補上舊資料庫缺少的欄位 ──
    try:
        with db.engine.connect() as conn:
            existing = db.engine.dialect.get_columns(conn, 'line_users')
            col_names = [c['name'] for c in existing]
            if 'booking_session' not in col_names:
                conn.execute(db.text(
                    'ALTER TABLE line_users ADD COLUMN booking_session TEXT'))
                conn.commit()
                print('[migrate] 新增 line_users.booking_session 欄位')
            # bookings.segments
            bk_cols = [c['name'] for c in db.engine.dialect.get_columns(conn, 'bookings')]
            if 'segments' not in bk_cols:
                conn.execute(db.text('ALTER TABLE bookings ADD COLUMN segments TEXT'))
                conn.commit()
                print('[migrate] 新增 bookings.segments 欄位')
            # rooms.photos / cover_index
            rm_cols = [c['name'] for c in db.engine.dialect.get_columns(conn, 'rooms')]
            if 'photos' not in rm_cols:
                conn.execute(db.text('ALTER TABLE rooms ADD COLUMN photos TEXT'))
                conn.commit()
                print('[migrate] 新增 rooms.photos 欄位')
            if 'cover_index' not in rm_cols:
                conn.execute(db.text('ALTER TABLE rooms ADD COLUMN cover_index INTEGER DEFAULT 0'))
                conn.commit()
                print('[migrate] 新增 rooms.cover_index 欄位')
            # blocked_slots table
            inspector = db.inspect(db.engine)
            existing_tables = inspector.get_table_names()
            if 'blocked_slots' not in existing_tables:
                db.create_all()
                print('[migrate] 新增 blocked_slots table')
            if 'capacity_min' not in rm_cols:
                conn.execute(db.text('ALTER TABLE rooms ADD COLUMN capacity_min INTEGER DEFAULT 0'))
                conn.commit()
                print('[migrate] 新增 rooms.capacity_min 欄位')
    except Exception as e:
        print(f'[migrate] 欄位檢查略過：{e}')
    try:
        db.create_all()
        print('[migrate] db.create_all() done')
    except Exception as e:
        print(f'[migrate] create_all error: {e}')
    try:
        if not AdminUser.query.filter_by(username='admin').first():
            su = AdminUser(username='admin', display_name='超級管理員',
                           role='superadmin', is_active=True, created_by='system')
            su.set_password(ADMIN_PASSWORD)
            db.session.add(su)
            db.session.commit()
            print(f'[migrate] superadmin created, pw={ADMIN_PASSWORD}')
    except Exception as e:
        print(f'[migrate] superadmin error: {e}')
    seed()

# ─────────────────────────────────────────────
# Admin -- Accounts & Login Logs
# ─────────────────────────────────────────────

@app.route('/admin/api/accounts', methods=['GET'])
def admin_get_accounts():
    err = check_admin()
    if err: return err
    users = AdminUser.query.order_by(AdminUser.created_at).all()
    return jsonify([u.to_dict() for u in users])

@app.route('/admin/api/accounts', methods=['POST'])
def admin_create_account():
    err = check_admin()
    if err: return err
    d = request.get_json()
    if AdminUser.query.filter_by(username=d['username']).first():
        return jsonify({'error': '帳號已存在'}), 400
    creator = get_current_admin()
    u = AdminUser(
        username     = d['username'].strip(),
        display_name = d.get('display_name', '').strip(),
        role         = d.get('role', 'staff'),
        is_active    = d.get('is_active', True),
        created_by   = creator.username if creator else 'admin',
    )
    u.set_password(d.get('password', 'changeme'))
    if d.get('permissions') is not None:
        import json as _j
        u.permissions = _j.dumps(d['permissions'], ensure_ascii=False)
    db.session.add(u)
    db.session.commit()
    return jsonify({'success': True, 'user': u.to_dict()}), 201

@app.route('/admin/api/accounts/<int:uid>', methods=['PUT'])
def admin_update_account(uid):
    err = check_admin()
    if err: return err
    u = AdminUser.query.get_or_404(uid)
    d = request.get_json()
    import json as _j
    if 'display_name' in d: u.display_name = d['display_name']
    if 'role' in d: u.role = d['role']
    if 'is_active' in d: u.is_active = d['is_active']
    if 'password' in d and d['password']: u.set_password(d['password'])
    if 'permissions' in d: u.permissions = _j.dumps(d['permissions'], ensure_ascii=False)
    db.session.commit()
    return jsonify({'success': True, 'user': u.to_dict()})

@app.route('/admin/api/accounts/<int:uid>', methods=['DELETE'])
def admin_delete_account(uid):
    err = check_admin()
    if err: return err
    u = AdminUser.query.get_or_404(uid)
    if u.role == 'superadmin' and AdminUser.query.filter_by(role='superadmin').count() <= 1:
        return jsonify({'error': '至少保留一個超級管理員'}), 400
    db.session.delete(u)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/admin/api/bookings/<int:bid>', methods=['DELETE'])
def admin_delete_booking(bid):
    err = check_admin()
    if err: return err
    b = Booking.query.get_or_404(bid)
    db.session.delete(b)
    db.session.commit()
    return jsonify({'success': True})


@app.route('/admin/api/login-logs', methods=['GET'])
def admin_get_login_logs():
    err = check_admin()
    if err: return err
    page = int(request.args.get('page', 1))
    per  = int(request.args.get('per', 50))
    q    = AdminLoginLog.query.order_by(AdminLoginLog.login_at.desc())
    total = q.count()
    logs  = q.offset((page-1)*per).limit(per).all()
    return jsonify({'total': total, 'logs': [l.to_dict() for l in logs]})


# ─────────────────────────────────────────────
# Admin — Blocked Slots
# ─────────────────────────────────────────────

@app.route('/admin/api/blocked-slots', methods=['GET'])
def admin_get_blocked_slots():
    err = check_admin()
    if err: return err
    from datetime import datetime as _dt
    date_from = request.args.get('from', _dt.now().strftime('%Y-%m-%d'))
    date_to   = request.args.get('to', '')
    room_id   = request.args.get('room_id')
    q = BlockedSlot.query.filter(BlockedSlot.date >= date_from)
    if date_to:
        q = q.filter(BlockedSlot.date <= date_to)
    if room_id:
        q = q.filter((BlockedSlot.room_id == int(room_id)) | (BlockedSlot.room_id.is_(None)))
    slots = q.order_by(BlockedSlot.date, BlockedSlot.start_time).all()
    return jsonify([s.to_dict() for s in slots])

@app.route('/admin/api/blocked-slots', methods=['POST'])
def admin_add_blocked_slot():
    err = check_admin()
    if err: return err
    d = request.get_json()
    slots_data = d.get('slots', [])
    if not slots_data:
        slots_data = [d]
    created = []
    for s in slots_data:
        bs = BlockedSlot(
            room_id    = s.get('room_id') or None,
            date       = s['date'],
            start_time = s['start_time'],
            end_time   = s['end_time'],
            reason     = s.get('reason', ''),
        )
        db.session.add(bs)
        created.append(bs)
    db.session.commit()
    return jsonify({'success': True, 'count': len(created)}), 201

@app.route('/admin/api/blocked-slots/<int:bid>', methods=['DELETE'])
def admin_delete_blocked_slot(bid):
    err = check_admin()
    if err: return err
    bs = BlockedSlot.query.get_or_404(bid)
    db.session.delete(bs)
    db.session.commit()
    return jsonify({'success': True})

@app.route('/admin/api/blocked-slots/bulk-delete', methods=['POST'])
def admin_bulk_delete_blocked_slots():
    err = check_admin()
    if err: return err
    ids = request.get_json().get('ids', [])
    BlockedSlot.query.filter(BlockedSlot.id.in_(ids)).delete(synchronize_session=False)
    db.session.commit()
    return jsonify({'success': True})


# ─────────────────────────────────────────────
# Health Check（供 UptimeRobot / Render ping 用）
# ─────────────────────────────────────────────

@app.route('/health')
def health_check():
    return jsonify({'status': 'ok', 'time': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}), 200


if __name__ == '__main__':
    print('\n會議室預約系統啟動中...')
    print('   前台預約：http://localhost:5000')
    print('   管理後台：http://localhost:5000/admin')
    print('   LINE Webhook：http://localhost:5000/webhook/line')
    print(f'  管理密碼：{ADMIN_PASSWORD}\n')
    app.run(debug=True, port=5000)