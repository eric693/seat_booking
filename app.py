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

# Render 持久磁碟用 /data/meeting_rooms.db，本地用 sqlite:///meeting_rooms.db
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///meeting_rooms.db')
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


def _row(label, value, bold=False, color='#333333'):
    return {
        'type': 'box', 'layout': 'horizontal', 'paddingTop': '4px',
        'contents': [
            {'type': 'text', 'text': label, 'size': 'sm', 'color': '#888888', 'flex': 2},
            {'type': 'text', 'text': str(value), 'size': 'sm', 'color': color,
             'flex': 4, 'wrap': True, 'weight': 'bold' if bold else 'regular'},
        ]
    }


def _sep():
    return {'type': 'separator', 'margin': 'sm'}


def flex_booking_confirm(booking) -> dict:
    """預約成立 Flex Message"""
    room  = booking.room.name if booking.room else '—'
    smap  = {'confirmed': '✅ 已確認', 'cancelled': '❌ 已取消', 'completed': '✔ 已完成'}
    return {
        'type': 'flex',
        'altText': f'【預約確認】{room} {booking.date} {booking.start_time}–{booking.end_time}',
        'contents': {
            'type': 'bubble',
            'header': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': '#2A6B6B', 'paddingAll': '20px',
                'contents': [
                    {'type': 'text', 'text': '🏢 會議室預約確認',
                     'color': '#FFFFFF', 'size': 'lg', 'weight': 'bold'},
                    {'type': 'text', 'text': smap.get(booking.status, booking.status),
                     'color': '#B8E0E0', 'size': 'sm', 'margin': 'sm'},
                ]
            },
            'body': {
                'type': 'box', 'layout': 'vertical', 'spacing': 'sm',
                'contents': [
                    _row('預約編號', booking.booking_number),
                    _row('會議室',   room),
                    _row('日期',     booking.date),
                    _row('時段',     f'{booking.start_time} – {booking.end_time}'),
                    _row('時長',     f'{booking.duration} 小時'),
                    _row('出席人數', f'{booking.attendees} 人'),
                    _sep(),
                    _row('聯絡人', booking.customer_name, bold=True),
                    _row('費用',   f'NT$ {booking.total_price:,}', bold=True, color='#2A6B6B'),
                ]
            },
            'footer': {
                'type': 'box', 'layout': 'vertical', 'paddingAll': '16px',
                'backgroundColor': '#F5F2ED',
                'contents': [{'type': 'text',
                    'text': '如需取消或更改，請提前 2 小時聯繫管理員',
                    'color': '#888888', 'size': 'xs', 'wrap': True}]
            }
        }
    }


def flex_booking_cancel(booking) -> dict:
    """取消通知 Flex Message"""
    room = booking.room.name if booking.room else '—'
    return {
        'type': 'flex',
        'altText': f'【預約取消】{room} {booking.date}',
        'contents': {
            'type': 'bubble',
            'header': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': '#C44B3A', 'paddingAll': '20px',
                'contents': [{'type': 'text', 'text': '❌ 預約已取消',
                              'color': '#FFFFFF', 'size': 'lg', 'weight': 'bold'}]
            },
            'body': {
                'type': 'box', 'layout': 'vertical', 'spacing': 'sm',
                'contents': [
                    _row('預約編號', booking.booking_number),
                    _row('會議室',   room),
                    _row('日期',     booking.date),
                    _row('時段',     f'{booking.start_time} – {booking.end_time}'),
                ]
            }
        }
    }


def flex_admin_notify(booking) -> dict:
    """新預約管理員通知 Flex Message"""
    room = booking.room.name if booking.room else '—'
    return {
        'type': 'flex',
        'altText': f'[新預約] {booking.customer_name} · {room} {booking.date}',
        'contents': {
            'type': 'bubble',
            'header': {
                'type': 'box', 'layout': 'vertical',
                'backgroundColor': '#B8965A', 'paddingAll': '20px',
                'contents': [
                    {'type': 'text', 'text': '🔔 新預約通知',
                     'color': '#FFFFFF', 'size': 'lg', 'weight': 'bold'},
                    {'type': 'text',
                     'text': booking.created_at.strftime('%Y-%m-%d %H:%M') if booking.created_at else '',
                     'color': '#FFF3D6', 'size': 'xs'},
                ]
            },
            'body': {
                'type': 'box', 'layout': 'vertical', 'spacing': 'sm',
                'contents': [
                    _row('預約人', booking.customer_name, bold=True),
                    _row('電話',   booking.customer_phone),
                    _row('部門',   booking.department or '—'),
                    _sep(),
                    _row('會議室', room),
                    _row('日期',   booking.date),
                    _row('時段',   f'{booking.start_time} – {booking.end_time}'),
                    _row('人數',   f'{booking.attendees} 人'),
                    _row('目的',   booking.purpose or '—'),
                    _sep(),
                    _row('費用', f'NT$ {booking.total_price:,}', bold=True, color='#2A6B6B'),
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
    capacity    = db.Column(db.Integer, default=10)
    hourly_rate = db.Column(db.Integer, default=500)
    description = db.Column(db.Text)
    amenities   = db.Column(db.Text)
    photo_url   = db.Column(db.String(500))
    is_active   = db.Column(db.Boolean, default=True)
    floor       = db.Column(db.String(20))
    created_at  = db.Column(db.DateTime, default=datetime.now)

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'room_type': self.room_type,
            'capacity': self.capacity, 'hourly_rate': self.hourly_rate,
            'description': self.description,
            'amenities': json.loads(self.amenities) if self.amenities else [],
            'photo_url': self.photo_url, 'is_active': self.is_active, 'floor': self.floor,
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


class LineUser(db.Model):
    """LINE Bot 使用者記錄"""
    __tablename__ = 'line_users'
    id           = db.Column(db.Integer, primary_key=True)
    line_user_id = db.Column(db.String(100), unique=True, nullable=False)
    phone        = db.Column(db.String(20))
    display_name = db.Column(db.String(100))
    is_admin     = db.Column(db.Boolean, default=False)
    created_at   = db.Column(db.DateTime, default=datetime.now)

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
    pw = request.headers.get('X-Admin-Password')
    if not pw or pw != ADMIN_PASSWORD:
        return jsonify({'error': 'Unauthorized'}), 401
    return None


def generate_booking_number():
    today = datetime.now().strftime('%Y%m%d')
    count = Booking.query.filter(Booking.booking_number.like(f'MR{today}%')).count()
    return f'MR{today}{str(count + 1).zfill(4)}'


def allowed_file(fn):
    return '.' in fn and fn.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def check_availability(room_id, date, start_time, end_time, exclude_id=None):
    q = Booking.query.filter_by(room_id=room_id, date=date, status='confirmed')
    if exclude_id:
        q = q.filter(Booking.id != exclude_id)
    def m(t):
        h, mn = map(int, t.split(':'))
        return h * 60 + mn
    s, e = m(start_time), m(end_time)
    for b in q.all():
        if not (e <= m(b.start_time) or s >= m(b.end_time)):
            return False
    return True


def get_booked_slots(room_id, date):
    bookings = Booking.query.filter_by(
        room_id=room_id, date=date, status='confirmed').all()
    return [{'start': b.start_time, 'end': b.end_time,
             'booking_number': b.booking_number} for b in bookings]


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
    keys = ['site_title','site_subtitle','site_description','hero_badge','hero_cta',
            'service_hours','contact_phone','contact_email',
            'notice_1','notice_2','notice_3','notice_4','notice_5','footer_text']
    return jsonify({k: SiteContent.get(k) for k in keys})


@app.route('/api/rooms')
def get_rooms():
    return jsonify([r.to_dict() for r in Room.query.filter_by(is_active=True).all()])


@app.route('/api/rooms/<int:room_id>/availability')
def room_availability(room_id):
    date = request.args.get('date')
    if not date:
        return jsonify({'error': 'Missing date'}), 400
    return jsonify({'booked_slots': get_booked_slots(room_id, date)})


@app.route('/api/book', methods=['POST'])
def create_booking():
    data = request.get_json()
    room = Room.query.get(data.get('room_id'))
    if not room:
        return jsonify({'error': '找不到此會議室'}), 404
    if not check_availability(room.id, data['date'], data['start_time'], data['end_time']):
        return jsonify({'error': '此時段已被預約，請選擇其他時間'}), 400

    def m(t):
        h, mn = map(int, t.split(':'))
        return h * 60 + mn
    dur   = (m(data['end_time']) - m(data['start_time'])) / 60
    price = int(dur * room.hourly_rate)

    # 嘗試從 LineUser 查詢 line_user_id（若使用者有綁定手機）
    line_uid = data.get('line_user_id', '')
    if not line_uid and data.get('phone'):
        lu = LineUser.query.filter_by(phone=data['phone']).first()
        if lu:
            line_uid = lu.line_user_id

    booking = Booking(
        booking_number=generate_booking_number(),
        room_id=room.id,
        customer_name=data['name'],
        customer_phone=data['phone'],
        customer_email=data.get('email', ''),
        department=data.get('department', ''),
        date=data['date'],
        start_time=data['start_time'],
        end_time=data['end_time'],
        duration=dur,
        total_price=price,
        attendees=data.get('attendees', 1),
        purpose=data.get('purpose', ''),
        note=data.get('note', ''),
        line_user_id=line_uid,
    )
    db.session.add(booking)
    db.session.commit()
    booking = Booking.query.get(booking.id)

    # LINE 推播：使用者確認 + 所有管理員通知
    if booking.line_user_id:
        push_line(booking.line_user_id, [flex_booking_confirm(booking)])
    for aid in admin_line_ids():
        push_line(aid, [flex_admin_notify(booking)])

    return jsonify({'success': True, 'booking': booking.to_dict()}), 201


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
            reply_line(rtok, [{'type': 'text', 'text': (
                '👋 歡迎使用會議室預約系統！\n\n'
                '可用指令：\n'
                '• 查詢 [預約編號] — 查詢預約狀態\n'
                '• 我的預約 — 查詢最近 3 筆\n'
                '• 綁定 [手機號碼] — 綁定後自動收通知\n'
                '• 說明 — 查看所有指令'
            )}])

        elif etype == 'message' and event.get('message', {}).get('type') == 'text':
            _handle_line_text(uid, rtok, event['message']['text'].strip())

    return 'OK', 200


def _handle_line_text(uid, rtok, text):
    lower = text.lower()

    if lower in ('說明', 'help', '指令', '?', '？'):
        reply_line(rtok, [{'type': 'text', 'text': (
            '📋 可用指令：\n\n'
            '查詢 [預約編號]\n  範例：查詢 MR202601010001\n\n'
            '我的預約\n  顯示最近 3 筆預約\n\n'
            '綁定 [手機號碼]\n  範例：綁定 0912345678\n  綁定後預約成立／取消將自動通知\n\n'
            '說明 — 顯示此說明'
        )}])
        return

    if lower.startswith('查詢'):
        number = text[2:].strip().upper()
        b = Booking.query.filter_by(booking_number=number).first() if number else None
        if not b:
            reply_line(rtok, [{'type': 'text',
                'text': f'❌ 找不到預約編號 {number}，請確認後再試。'}])
        else:
            reply_line(rtok, [flex_booking_confirm(b)])
        return

    if lower in ('我的預約', '預約紀錄'):
        lu = LineUser.query.filter_by(line_user_id=uid).first()
        if not lu or not lu.phone:
            reply_line(rtok, [{'type': 'text',
                'text': '請先綁定手機號碼：\n綁定 0912345678'}])
            return
        bs = (Booking.query.filter_by(customer_phone=lu.phone)
              .order_by(Booking.created_at.desc()).limit(3).all())
        if not bs:
            reply_line(rtok, [{'type': 'text', 'text': '目前沒有預約紀錄。'}])
        else:
            reply_line(rtok, [flex_booking_confirm(b) for b in bs])
        return

    if lower.startswith('綁定'):
        phone = text[2:].strip().replace('-', '').replace(' ', '')
        if not phone.isdigit() or len(phone) < 8:
            reply_line(rtok, [{'type': 'text',
                'text': '手機格式不正確，請輸入如：\n綁定 0912345678'}])
            return
        lu = upsert_line_user(uid)
        lu.phone = phone
        # 同步舊預約
        Booking.query.filter_by(customer_phone=phone, line_user_id=None).update(
            {'line_user_id': uid})
        db.session.commit()
        reply_line(rtok, [{'type': 'text',
            'text': f'✅ 已綁定 {phone}\n往後預約通知將自動推播給您。'}])
        return

    reply_line(rtok, [{'type': 'text', 'text': '收到！輸入「說明」查看可用指令。'}])


# ─────────────────────────────────────────────
# Admin Login
# ─────────────────────────────────────────────

@app.route('/admin/api/login', methods=['POST'])
def admin_login():
    data = request.get_json()
    if data.get('password') == ADMIN_PASSWORD:
        session['admin'] = True
        return jsonify({'success': True})
    return jsonify({'error': '密碼錯誤'}), 401


# ─────────────────────────────────────────────
# Admin — Rooms
# ─────────────────────────────────────────────

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
             capacity=d.get('capacity', 10), hourly_rate=d.get('hourly_rate', 500),
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
    for f in ['name','room_type','capacity','hourly_rate','description',
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
    return jsonify({i.key: i.value for i in SiteContent.query.all()})

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
    'site_url':         'https://your-app.onrender.com',
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
     'amenities':['雙投影幕','麥克風系統','劇院座椅','燈光控制','錄影設備','舞台'],'floor':'1F'},
    {'name':'視訊會議中心','room_type':'視訊會議','capacity':12,'hourly_rate':1000,
     'description':'4K 攝影機搭配環繞音響，遠端與現場皆有絕佳體驗。',
     'amenities':['4K 攝影機','環繞音響','自動追蹤','雙顯示器','噪音抑制麥克風','WiFi 6'],'floor':'4F'},
    {'name':'主管行政套房','room_type':'行政套房','capacity':6,'hourly_rate':1500,
     'description':'頂層行政會議室，俯瞰城市景觀，適合董事會議、VIP 接待。',
     'amenities':['城市景觀','高端家具','私人衛浴','秘書服務','餐飲服務','私人停車'],'floor':'12F'},
    {'name':'多功能培訓教室','room_type':'培訓教室','capacity':30,'hourly_rate':1200,
     'description':'彈性空間配置，適合員工培訓、研討會、工作坊。',
     'amenities':['電子白板','個人顯示器','彈性座位','錄音設備','茶水站','停車場'],'floor':'5F'},
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
    db.session.commit()
    print('資料庫初始化完成')


with app.app_context():
    db.create_all()
    seed()

if __name__ == '__main__':
    print('\n會議室預約系統啟動中...')
    print('   前台預約：http://localhost:5000')
    print('   管理後台：http://localhost:5000/admin')
    print('   LINE Webhook：http://localhost:5000/webhook/line')
    print(f'   管理密碼：{ADMIN_PASSWORD}\n')
    app.run(debug=True, port=5000)