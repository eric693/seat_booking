# -*- coding: utf-8 -*-
import os
import json
import uuid
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory, session
from flask_sqlalchemy import SQLAlchemy
from flask_cors import CORS
from sqlalchemy import func
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'meeting-room-booking-2026')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///meeting_rooms.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload

UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}

CORS(app)
db = SQLAlchemy(app)

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')


# ─────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────

class Room(db.Model):
    __tablename__ = 'rooms'
    id           = db.Column(db.Integer, primary_key=True)
    name         = db.Column(db.String(100), nullable=False)
    room_type    = db.Column(db.String(50), nullable=False)   # 六種類型
    capacity     = db.Column(db.Integer, default=10)
    hourly_rate  = db.Column(db.Integer, default=500)
    description  = db.Column(db.Text)
    amenities    = db.Column(db.Text)   # JSON array string
    photo_url    = db.Column(db.String(500))
    is_active    = db.Column(db.Boolean, default=True)
    floor        = db.Column(db.String(20))
    created_at   = db.Column(db.DateTime, default=datetime.now)

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'room_type': self.room_type,
            'capacity': self.capacity,
            'hourly_rate': self.hourly_rate,
            'description': self.description,
            'amenities': json.loads(self.amenities) if self.amenities else [],
            'photo_url': self.photo_url,
            'is_active': self.is_active,
            'floor': self.floor,
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
    duration       = db.Column(db.Integer, default=1)   # hours
    total_price    = db.Column(db.Integer, default=0)
    attendees      = db.Column(db.Integer, default=1)
    purpose        = db.Column(db.Text)
    status         = db.Column(db.String(20), default='confirmed')
    note           = db.Column(db.Text)
    created_at     = db.Column(db.DateTime, default=datetime.now)
    room           = db.relationship('Room', backref='bookings')

    def to_dict(self):
        return {
            'id': self.id,
            'booking_number': self.booking_number,
            'room_id': self.room_id,
            'room_name': self.room.name if self.room else '',
            'room_type': self.room.room_type if self.room else '',
            'customer_name': self.customer_name,
            'customer_phone': self.customer_phone,
            'customer_email': self.customer_email,
            'department': self.department,
            'date': self.date,
            'start_time': self.start_time,
            'end_time': self.end_time,
            'duration': self.duration,
            'total_price': self.total_price,
            'attendees': self.attendees,
            'purpose': self.purpose,
            'status': self.status,
            'note': self.note,
            'created_at': self.created_at.strftime('%Y-%m-%d %H:%M') if self.created_at else ''
        }


class SiteContent(db.Model):
    """前端說明文字設定"""
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


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def check_room_availability(room_id, date, start_time, end_time, exclude_id=None):
    """Check if room is free for given time range"""
    query = Booking.query.filter(
        Booking.room_id == room_id,
        Booking.date == date,
        Booking.status == 'confirmed'
    )
    if exclude_id:
        query = query.filter(Booking.id != exclude_id)
    existing = query.all()
    # Convert to minutes for comparison
    def to_min(t):
        h, m = map(int, t.split(':'))
        return h * 60 + m
    s = to_min(start_time)
    e = to_min(end_time)
    for b in existing:
        bs = to_min(b.start_time)
        be = to_min(b.end_time)
        if not (e <= bs or s >= be):
            return False
    return True


def get_booked_slots(room_id, date):
    bookings = Booking.query.filter(
        Booking.room_id == room_id,
        Booking.date == date,
        Booking.status == 'confirmed'
    ).all()
    return [{'start': b.start_time, 'end': b.end_time, 'booking_number': b.booking_number} for b in bookings]


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
    """Return all editable front-end text"""
    keys = [
        'site_title', 'site_subtitle', 'site_description',
        'hero_badge', 'hero_cta',
        'service_hours', 'contact_phone', 'contact_email',
        'notice_1', 'notice_2', 'notice_3', 'notice_4', 'notice_5',
        'footer_text',
    ]
    return jsonify({k: SiteContent.get(k) for k in keys})


@app.route('/api/rooms')
def get_rooms():
    rooms = Room.query.filter_by(is_active=True).all()
    return jsonify([r.to_dict() for r in rooms])


@app.route('/api/rooms/<int:room_id>/availability')
def room_availability(room_id):
    date = request.args.get('date')
    if not date:
        return jsonify({'error': 'Missing date'}), 400
    slots = get_booked_slots(room_id, date)
    return jsonify({'booked_slots': slots})


@app.route('/api/book', methods=['POST'])
def create_booking():
    data = request.get_json()
    room = Room.query.get(data.get('room_id'))
    if not room:
        return jsonify({'error': '找不到此會議室'}), 404
    if not check_room_availability(room.id, data['date'], data['start_time'], data['end_time']):
        return jsonify({'error': '此時段已被預約，請選擇其他時間'}), 400

    # Calculate duration and price
    def to_min(t):
        h, m = map(int, t.split(':'))
        return h * 60 + m
    duration_min = to_min(data['end_time']) - to_min(data['start_time'])
    duration_hr = duration_min / 60
    total_price = int(duration_hr * room.hourly_rate)

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
        duration=duration_hr,
        total_price=total_price,
        attendees=data.get('attendees', 1),
        purpose=data.get('purpose', ''),
        note=data.get('note', ''),
        source='web' if True else 'web'
    )
    db.session.add(booking)
    db.session.commit()
    booking = Booking.query.get(booking.id)
    return jsonify({'success': True, 'booking': booking.to_dict()}), 201


@app.route('/api/bookings/check')
def check_booking():
    number = request.args.get('number')
    phone = request.args.get('phone')
    if not number or not phone:
        return jsonify({'error': '請提供預約編號和電話'}), 400
    booking = Booking.query.filter_by(booking_number=number, customer_phone=phone).first()
    if not booking:
        return jsonify({'error': '找不到此預約'}), 404
    return jsonify(booking.to_dict())


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
# Admin API — Rooms
# ─────────────────────────────────────────────

@app.route('/admin/api/rooms', methods=['GET'])
def admin_get_rooms():
    err = check_admin()
    if err: return err
    rooms = Room.query.order_by(Room.created_at.desc()).all()
    return jsonify([r.to_dict() for r in rooms])


@app.route('/admin/api/rooms', methods=['POST'])
def admin_add_room():
    err = check_admin()
    if err: return err
    data = request.get_json()
    room = Room(
        name=data['name'],
        room_type=data['room_type'],
        capacity=data.get('capacity', 10),
        hourly_rate=data.get('hourly_rate', 500),
        description=data.get('description', ''),
        amenities=json.dumps(data.get('amenities', []), ensure_ascii=False),
        floor=data.get('floor', ''),
        photo_url=data.get('photo_url', ''),
        is_active=data.get('is_active', True)
    )
    db.session.add(room)
    db.session.commit()
    return jsonify(room.to_dict()), 201


@app.route('/admin/api/rooms/<int:rid>', methods=['PUT'])
def admin_update_room(rid):
    err = check_admin()
    if err: return err
    room = Room.query.get_or_404(rid)
    data = request.get_json()
    for field in ['name', 'room_type', 'capacity', 'hourly_rate', 'description', 'floor', 'photo_url', 'is_active']:
        if field in data:
            setattr(room, field, data[field])
    if 'amenities' in data:
        room.amenities = json.dumps(data['amenities'], ensure_ascii=False)
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
# Admin API — Photo Upload
# ─────────────────────────────────────────────

@app.route('/admin/api/upload-photo', methods=['POST'])
def upload_photo():
    err = check_admin()
    if err: return err
    if 'photo' not in request.files:
        return jsonify({'error': '未選擇檔案'}), 400
    file = request.files['photo']
    if file.filename == '':
        return jsonify({'error': '未選擇檔案'}), 400
    if not allowed_file(file.filename):
        return jsonify({'error': '不支援的檔案格式（支援 PNG, JPG, GIF, WEBP）'}), 400
    ext = file.filename.rsplit('.', 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    file.save(filepath)
    photo_url = f'/static/uploads/{filename}'
    return jsonify({'success': True, 'photo_url': photo_url})


# ─────────────────────────────────────────────
# Admin API — Bookings
# ─────────────────────────────────────────────

@app.route('/admin/api/bookings', methods=['GET'])
def admin_get_bookings():
    err = check_admin()
    if err: return err
    date = request.args.get('date')
    status = request.args.get('status')
    room_id = request.args.get('room_id')
    query = Booking.query
    if date: query = query.filter_by(date=date)
    if status: query = query.filter_by(status=status)
    if room_id: query = query.filter_by(room_id=int(room_id))
    bookings = query.order_by(Booking.created_at.desc()).all()
    return jsonify([b.to_dict() for b in bookings])


@app.route('/admin/api/bookings/<int:bid>/cancel', methods=['POST'])
def admin_cancel_booking(bid):
    err = check_admin()
    if err: return err
    booking = Booking.query.get_or_404(bid)
    booking.status = 'cancelled'
    db.session.commit()
    return jsonify({'success': True})


@app.route('/admin/api/bookings/<int:bid>/complete', methods=['POST'])
def admin_complete_booking(bid):
    err = check_admin()
    if err: return err
    booking = Booking.query.get_or_404(bid)
    booking.status = 'completed'
    db.session.commit()
    return jsonify({'success': True})


# ─────────────────────────────────────────────
# Admin API — Site Content
# ─────────────────────────────────────────────

@app.route('/admin/api/site-content', methods=['GET'])
def admin_get_site_content():
    err = check_admin()
    if err: return err
    items = SiteContent.query.all()
    return jsonify({i.key: i.value for i in items})


@app.route('/admin/api/site-content', methods=['POST'])
def admin_update_site_content():
    err = check_admin()
    if err: return err
    data = request.get_json()
    for key, value in data.items():
        SiteContent.set(key, value)
    return jsonify({'success': True})


# ─────────────────────────────────────────────
# Admin API — Stats
# ─────────────────────────────────────────────

@app.route('/admin/api/stats', methods=['GET'])
def admin_get_stats():
    err = check_admin()
    if err: return err
    today = datetime.now().strftime('%Y-%m-%d')
    stats = {
        'total_bookings': Booking.query.filter_by(status='confirmed').count(),
        'today_bookings': Booking.query.filter_by(date=today, status='confirmed').count(),
        'total_rooms': Room.query.filter_by(is_active=True).count(),
        'total_revenue': db.session.query(func.sum(Booking.total_price)).filter_by(status='confirmed').scalar() or 0,
        'cancelled': Booking.query.filter_by(status='cancelled').count(),
        'completed': Booking.query.filter_by(status='completed').count(),
    }
    return jsonify(stats)


# ─────────────────────────────────────────────
# Seed Data
# ─────────────────────────────────────────────

DEFAULT_CONTENT = {
    'site_title': '會議室預約系統',
    'site_subtitle': '企業空間 · 即時預約',
    'site_description': '提供多種類型會議室，彈性時段預約，滿足各種商務需求。從小型洽談到大型簡報，我們都為您準備好了。',
    'hero_badge': '專業會議空間',
    'service_hours': '週一至週五 08:00 – 22:00 ／ 週六 09:00 – 18:00',
    'contact_phone': '02-1234-5678',
    'contact_email': 'booking@example.com',
    'notice_1': '請提前 15 分鐘辦理入場手續',
    'notice_2': '取消或更改請提前 2 小時通知',
    'notice_3': '禁止攜帶食物進入精緻會議室',
    'notice_4': '使用後請恢復設備原始設定',
    'notice_5': '逾時使用將依時薪計費',
    'footer_text': '© 2026 會議室預約系統 · 版權所有',
}

ROOM_TYPES = [
    {'name': '創意腦力激盪室', 'room_type': '腦力激盪', 'capacity': 8, 'hourly_rate': 600,
     'description': '開放式空間設計，配備白板牆面與磁性貼牆，激發創意思維。適合產品企劃、設計衝刺、創意發想等工作坊。',
     'amenities': ['白板牆', '磁性貼紙', '活動式座椅', '投影機', 'WiFi', '充電站'], 'floor': '3F'},
    {'name': '精緻洽談室 A', 'room_type': '洽談室', 'capacity': 4, 'hourly_rate': 400,
     'description': '私密安靜的小型洽談空間，皮革座椅搭配木質桌面，營造專業且舒適的商談氛圍。',
     'amenities': ['螢幕共享', '視訊攝影機', '噪音隔絕', 'WiFi', '白板', '咖啡機'], 'floor': '2F'},
    {'name': '大型簡報廳', 'room_type': '簡報廳', 'capacity': 50, 'hourly_rate': 2000,
     'description': '專業簡報空間，配備劇院式座椅、雙螢幕投影、麥克風系統，適合公司發表會、教育訓練、大型會議。',
     'amenities': ['雙投影幕', '麥克風系統', '劇院座椅', '燈光控制', '錄影設備', '舞台'], 'floor': '1F'},
    {'name': '視訊會議中心', 'room_type': '視訊會議', 'capacity': 12, 'hourly_rate': 1000,
     'description': '高規格視訊會議室，4K 攝影機搭配環繞音響，無論遠端或現場與會者皆有絕佳體驗。',
     'amenities': ['4K 攝影機', '環繞音響', '自動追蹤', '雙顯示器', '噪音抑制麥克風', 'WiFi 6'], 'floor': '4F'},
    {'name': '主管行政套房', 'room_type': '行政套房', 'capacity': 6, 'hourly_rate': 1500,
     'description': '頂層行政會議室，俯瞰城市景觀，配備高端辦公家具，適合董事會議、高階主管洽談、VIP 接待。',
     'amenities': ['城市景觀', '高端家具', '私人衛浴', '秘書服務', '餐飲服務', '私人停車'], 'floor': '12F'},
    {'name': '多功能培訓教室', 'room_type': '培訓教室', 'capacity': 30, 'hourly_rate': 1200,
     'description': '彈性空間配置，座椅可重新排列，配備電子白板與個人顯示器，適合員工培訓、研討會、工作坊。',
     'amenities': ['電子白板', '個人顯示器', '彈性座位', '錄音設備', '茶水站', '停車場'], 'floor': '5F'},
]


def seed():
    # Site content
    for key, value in DEFAULT_CONTENT.items():
        if not SiteContent.query.filter_by(key=key).first():
            db.session.add(SiteContent(key=key, value=value))

    # Rooms
    if Room.query.count() == 0:
        for r in ROOM_TYPES:
            room = Room(
                name=r['name'],
                room_type=r['room_type'],
                capacity=r['capacity'],
                hourly_rate=r['hourly_rate'],
                description=r['description'],
                amenities=json.dumps(r['amenities'], ensure_ascii=False),
                floor=r['floor'],
                is_active=True
            )
            db.session.add(room)
    db.session.commit()
    print('初始化完成')


with app.app_context():
    db.create_all()
    seed()

if __name__ == '__main__':
    print('\n🏢 會議室預約系統啟動中...')
    print('   前台預約：http://localhost:5000')
    print('   管理後台：http://localhost:5000/admin')
    print(f'   管理密碼：{ADMIN_PASSWORD}\n')
    app.run(debug=True, port=5000)