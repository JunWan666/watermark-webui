#!/usr/bin/env python3
"""Blind Watermark WebUI backend with local SQLite persistence."""
import json
import math
import os
import sqlite3
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from xml.sax.saxutils import escape as xml_escape

# Use system packages (numpy, cv2, pywt installed via apt when available).
sys.path.insert(0, '/usr/lib/python3/dist-packages')

import cv2
from blind_watermark import WaterMark
from blind_watermark.recover import estimate_crop_parameters, match_template, recover_crop
from flask import Flask, jsonify, request, render_template_string, send_file, session
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get('WATERMARK_DATA_DIR', os.path.join(BASE_DIR, 'data'))
UPLOAD_DIR = os.path.join(DATA_DIR, 'uploads')
RESULT_DIR = os.path.join(DATA_DIR, 'results')
DB_PATH = os.path.join(DATA_DIR, 'watermark.db')
LEGACY_OUTPUT_DIR = os.path.join(BASE_DIR, 'output')
HTML_PATH = os.path.join(BASE_DIR, 'static', 'index.html')
RECOVERY_MATCH_LOCK = threading.Lock()
RECOVERY_MIN_SCORE = 0.15
RECOVERY_LOW_SCORE = 0.45

for directory in (DATA_DIR, UPLOAD_DIR, RESULT_DIR, LEGACY_OUTPUT_DIR):
    os.makedirs(directory, exist_ok=True)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024
app.secret_key = os.environ.get('WATERMARK_SECRET_KEY', 'change-this-local-secret')
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('WATERMARK_COOKIE_SECURE', '').lower() == 'true',
)


@contextmanager
def db_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    with db_connection() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                operation TEXT NOT NULL CHECK(operation IN ('embed', 'extract')),
                original_name TEXT NOT NULL,
                original_path TEXT NOT NULL,
                result_name TEXT,
                result_path TEXT,
                watermark TEXT,
                wm_shape INTEGER,
                pwd_img TEXT NOT NULL,
                pwd_wm TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                original_size INTEGER NOT NULL DEFAULT 0,
                result_size INTEGER NOT NULL DEFAULT 0,
                original_width INTEGER,
                original_height INTEGER,
                result_width INTEGER,
                result_height INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_records_user_created ON records(user_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_records_search ON records(user_id, original_name, result_name, watermark, note, wm_shape);
            """
        )
        db.execute('BEGIN IMMEDIATE')
        existing_columns = {row['name'] for row in db.execute('PRAGMA table_info(records)')}
        recovery_columns = {
            'extract_mode': "TEXT NOT NULL DEFAULT 'normal'",
            'reference_source': 'TEXT',
            'reference_record_id': 'INTEGER',
            'reference_name': 'TEXT',
            'reference_path': 'TEXT',
            'reference_size': 'INTEGER NOT NULL DEFAULT 0',
            'reference_width': 'INTEGER',
            'reference_height': 'INTEGER',
            'recovered_name': 'TEXT',
            'recovered_path': 'TEXT',
            'recovered_size': 'INTEGER NOT NULL DEFAULT 0',
            'recovered_width': 'INTEGER',
            'recovered_height': 'INTEGER',
            'recover_score': 'REAL',
            'recover_loc': 'TEXT',
            'recover_scale': 'REAL',
        }
        for column, definition in recovery_columns.items():
            if column not in existing_columns:
                db.execute(f'ALTER TABLE records ADD COLUMN {column} {definition}')


init_db()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def current_user_id():
    return session.get('user_id')


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user_id():
            return jsonify({'error': '请先登录', 'code': 'AUTH_REQUIRED'}), 401
        return view(*args, **kwargs)

    return wrapped


def user_count():
    with db_connection() as db:
        return db.execute('SELECT COUNT(*) FROM users').fetchone()[0]


def user_payload(user):
    return {'id': user['id'], 'username': user['username']}


def record_payload(row, include_secrets=False):
    extract_mode = row['extract_mode'] or 'normal'
    is_recovery = row['operation'] == 'extract' and extract_mode == 'recover'
    recover_loc = None
    if row['recover_loc']:
        try:
            recover_loc = json.loads(row['recover_loc'])
        except (TypeError, ValueError):
            recover_loc = None
    payload = {
        'id': row['id'],
        'operation': row['operation'],
        'operation_label': '嵌入水印' if row['operation'] == 'embed' else ('恢复提取' if is_recovery else '普通提取'),
        'extract_mode': extract_mode,
        'original_name': row['original_name'],
        'result_name': row['result_name'],
        'watermark': row['watermark'] or '',
        'wm_shape': row['wm_shape'],
        'note': row['note'] or '',
        'original_size': row['original_size'],
        'result_size': row['result_size'],
        'original_width': row['original_width'],
        'original_height': row['original_height'],
        'result_width': row['result_width'],
        'result_height': row['result_height'],
        'created_at': row['created_at'],
        'original_url': f"/api/records/{row['id']}/original",
        'result_url': f"/api/records/{row['id']}/result" if row['result_path'] else None,
        'reference_source': row['reference_source'],
        'reference_record_id': row['reference_record_id'],
        'reference_name': row['reference_name'],
        'reference_size': row['reference_size'],
        'reference_width': row['reference_width'],
        'reference_height': row['reference_height'],
        'reference_url': f"/api/records/{row['id']}/reference" if is_recovery else None,
        'recovered_name': row['recovered_name'],
        'recovered_size': row['recovered_size'],
        'recovered_width': row['recovered_width'],
        'recovered_height': row['recovered_height'],
        'recovered_url': f"/api/records/{row['id']}/recovered" if row['recovered_path'] else None,
        'recover_score': row['recover_score'],
        'recover_loc': recover_loc,
        'recover_scale': row['recover_scale'],
        'low_confidence': bool(is_recovery and row['recover_score'] is not None and row['recover_score'] < RECOVERY_LOW_SCORE),
    }
    if include_secrets:
        payload.update({'pwd_img': row['pwd_img'], 'pwd_wm': row['pwd_wm']})
    return payload


def safe_upload_path(uploaded_file, directory, default_ext='.jpg'):
    original_name = (uploaded_file.filename or '').strip() or f'upload{default_ext}'
    storage_name = secure_filename(original_name) or f'upload{default_ext}'
    extension = os.path.splitext(storage_name)[1].lower() or default_ext
    stored_name = f'{uuid.uuid4().hex}{extension}'
    path = os.path.join(directory, stored_name)
    try:
        uploaded_file.save(path)
    except Exception:
        remove_stored_file(path, directory)
        raise
    return original_name, path


def safe_stored_path(path, directory):
    """Resolve a database path only when it stays inside its storage root."""
    if not path:
        return None
    try:
        candidate = os.path.realpath(path)
        root = os.path.realpath(directory)
        if os.path.normcase(os.path.commonpath((candidate, root))) != os.path.normcase(root):
            return None
        return candidate
    except (OSError, ValueError, TypeError):
        return None


def remove_stored_file(path, directory):
    safe_path = safe_stored_path(path, directory)
    if not safe_path:
        return
    try:
        if os.path.isfile(safe_path):
            os.remove(safe_path)
    except OSError:
        app.logger.warning('Failed to remove stored file %s', safe_path, exc_info=True)


def cleanup_created_files(paths):
    for path, directory in paths:
        remove_stored_file(path, directory)


def record_stored_files(row):
    return (
        (row['original_path'], UPLOAD_DIR),
        (row['reference_path'], UPLOAD_DIR),
        (row['result_path'], RESULT_DIR),
        (row['recovered_path'], RESULT_DIR),
    )


def watermark_password_values(form):
    common_password = form.get('password')
    default_password = common_password if common_password not in (None, '') else '1234'
    pwd_img = form.get('pwd_img', default_password) or default_password
    pwd_wm = form.get('pwd_wm', default_password) or default_password
    return str(pwd_img).strip(), str(pwd_wm).strip()


def image_dimensions(path):
    try:
        image = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if image is None:
            return None, None
        height, width = image.shape[:2]
        return int(width), int(height)
    except Exception:
        return None, None


def watermark_svg_lines(value, max_chars=28):
    """Wrap extracted text into readable SVG lines without relying on a font library."""
    text = str(value or '').strip() or '未读取到水印文字'
    text = ''.join(character if character in '\t\n\r' or ord(character) >= 32 else '�' for character in text)
    lines = []
    for paragraph in text.splitlines() or ['']:
        paragraph = paragraph.strip()
        if not paragraph:
            lines.append('')
            continue
        while len(paragraph) > max_chars:
            lines.append(paragraph[:max_chars])
            paragraph = paragraph[max_chars:]
        lines.append(paragraph)
    return lines[:6]


def write_watermark_svg(path, watermark, wm_shape, created_at=None):
    """Create a compact result image used by extraction records and thumbnails."""
    lines = watermark_svg_lines(watermark)
    text_nodes = []
    for index, line in enumerate(lines):
        y = 142 + index * 34
        text_nodes.append(f'<tspan x="64" y="{y}">{xml_escape(line)}</tspan>')
    timestamp = (created_at or now_iso()).replace('+00:00', ' UTC')
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="960" height="420" viewBox="0 0 960 420">
  <rect width="960" height="420" rx="24" fill="#f4f8fd"/>
  <rect x="24" y="24" width="912" height="372" rx="18" fill="#ffffff" stroke="#d8e5f4" stroke-width="2"/>
  <rect x="64" y="62" width="6" height="32" rx="3" fill="#2d6cdf"/>
  <text x="88" y="87" fill="#637089" font-family="Microsoft YaHei, PingFang SC, sans-serif" font-size="18" font-weight="600">提取出的水印内容</text>
  <text x="64" y="142" fill="#142033" font-family="Microsoft YaHei, PingFang SC, sans-serif" font-size="27" font-weight="700">{''.join(text_nodes)}</text>
  <line x1="64" y1="342" x2="896" y2="342" stroke="#e6edf6"/>
  <text x="64" y="372" fill="#93a0b4" font-family="Microsoft YaHei, PingFang SC, sans-serif" font-size="14">wm_shape：{xml_escape(str(wm_shape))}    提取时间：{xml_escape(timestamp)}</text>
</svg>'''
    with open(path, 'w', encoding='utf-8') as svg_file:
        svg_file.write(svg)


def backfill_extract_results():
    """Give older extraction records the same result-image representation."""
    with db_connection() as db:
        rows = db.execute(
            "SELECT * FROM records WHERE operation = 'extract' AND (result_path IS NULL OR result_path = '')"
        ).fetchall()
        for row in rows:
            result_name = f'extract-{row["id"]}-{uuid.uuid4().hex[:10]}.svg'
            result_path = os.path.join(RESULT_DIR, result_name)
            write_watermark_svg(result_path, row['watermark'], row['wm_shape'], row['created_at'])
            db.execute(
                'UPDATE records SET result_name = ?, result_path = ?, result_size = ?, result_width = ?, result_height = ? WHERE id = ?',
                (result_name, result_path, os.path.getsize(result_path), 960, 420, row['id']),
            )


backfill_extract_results()


def owned_record(record_id):
    with db_connection() as db:
        return db.execute(
            'SELECT * FROM records WHERE id = ? AND user_id = ?',
            (record_id, current_user_id()),
        ).fetchone()


def render_index():
    with open(HTML_PATH, encoding='utf-8') as html_file:
        return render_template_string(html_file.read())


@app.route('/')
def index():
    return render_index()


@app.route('/embed')
@app.route('/extract')
@app.route('/history')
def app_page():
    """Serve the single-page workspace for direct menu URLs and refreshes."""
    return render_index()


@app.route('/api/auth/status')
def auth_status():
    user_id = current_user_id()
    user = None
    if user_id:
        with db_connection() as db:
            user = db.execute('SELECT id, username FROM users WHERE id = ?', (user_id,)).fetchone()
        if user is None:
            session.clear()
    return jsonify({
        'authenticated': bool(user),
        'user': user_payload(user) if user else None,
        'has_users': user_count() > 0,
    })


@app.route('/api/auth/register', methods=['POST'])
def register():
    if user_count() > 0:
        return jsonify({'error': '系统已经完成初始化，请登录后在账户设置中修改信息'}), 409
    data = request.get_json(silent=True) or {}
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))
    if len(username) < 2 or len(username) > 32:
        return jsonify({'error': '用户名称长度需为 2-32 个字符'}), 400
    if len(password) < 6:
        return jsonify({'error': '密码至少需要 6 个字符'}), 400
    timestamp = now_iso()
    try:
        with db_connection() as db:
            cursor = db.execute(
                'INSERT INTO users(username, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?)',
                (username, generate_password_hash(password), timestamp, timestamp),
            )
            session.clear()
            session['user_id'] = cursor.lastrowid
    except sqlite3.IntegrityError:
        return jsonify({'error': '用户名称已存在'}), 409
    return jsonify({'success': True, 'user': {'id': session['user_id'], 'username': username}})


@app.route('/api/auth/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))
    with db_connection() as db:
        user = db.execute('SELECT * FROM users WHERE username = ? COLLATE NOCASE', (username,)).fetchone()
    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'error': '用户名称或密码错误'}), 401
    session.clear()
    session['user_id'] = user['id']
    return jsonify({'success': True, 'user': user_payload(user)})


@app.route('/api/auth/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True})


@app.route('/api/auth/account', methods=['PATCH'])
@login_required
def update_account():
    data = request.get_json(silent=True) or {}
    current_password = str(data.get('current_password', ''))
    username = str(data.get('username', '')).strip()
    new_password = str(data.get('password', ''))
    if not current_password:
        return jsonify({'error': '请输入当前密码'}), 400
    with db_connection() as db:
        user = db.execute('SELECT * FROM users WHERE id = ?', (current_user_id(),)).fetchone()
        if not user or not check_password_hash(user['password_hash'], current_password):
            return jsonify({'error': '当前密码错误'}), 400
        next_username = username or user['username']
        if len(next_username) < 2 or len(next_username) > 32:
            return jsonify({'error': '用户名称长度需为 2-32 个字符'}), 400
        if new_password and len(new_password) < 6:
            return jsonify({'error': '新密码至少需要 6 个字符'}), 400
        try:
            db.execute(
                'UPDATE users SET username = ?, password_hash = ?, updated_at = ? WHERE id = ?',
                (next_username, generate_password_hash(new_password) if new_password else user['password_hash'], now_iso(), user['id']),
            )
        except sqlite3.IntegrityError:
            return jsonify({'error': '用户名称已存在'}), 409
    return jsonify({'success': True, 'user': {'id': user['id'], 'username': next_username}})


@app.route('/api/embed', methods=['POST'])
@login_required
def api_embed():
    if 'image' not in request.files:
        return jsonify({'error': '请上传图片'}), 400
    uploaded = request.files['image']
    if not uploaded.filename:
        return jsonify({'error': '未选择图片'}), 400
    watermark_text = request.form.get('text', '').strip()
    if not watermark_text:
        return jsonify({'error': '请输入水印文字'}), 400
    pwd_img, pwd_wm = watermark_password_values(request.form)
    note = request.form.get('note', '').strip()[:500]
    try:
        int(pwd_img)
        int(pwd_wm)
    except ValueError:
        return jsonify({'error': '图片密码和水印密码必须为数字'}), 400

    original_name, original_path = safe_upload_path(uploaded, UPLOAD_DIR)
    original_width, original_height = image_dimensions(original_path)
    result_name = f'{uuid.uuid4().hex}.png'
    result_path = os.path.join(RESULT_DIR, result_name)
    try:
        bwm = WaterMark(password_img=int(pwd_img), password_wm=int(pwd_wm))
        bwm.read_img(original_path)
        bwm.read_wm(watermark_text, mode='str')
        bwm.embed(result_path)
        wm_shape = len(bwm.wm_bit)
        result_width, result_height = image_dimensions(result_path)
        timestamp = now_iso()
        with db_connection() as db:
            cursor = db.execute(
                """INSERT INTO records(
                    user_id, operation, original_name, original_path, result_name, result_path,
                    watermark, wm_shape, pwd_img, pwd_wm, note, original_size, result_size,
                    original_width, original_height, result_width, result_height, created_at
                ) VALUES (?, 'embed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    current_user_id(), original_name, original_path, result_name, result_path,
                    watermark_text, wm_shape, pwd_img, pwd_wm, note,
                    os.path.getsize(original_path), os.path.getsize(result_path),
                    original_width, original_height, result_width, result_height, timestamp,
                ),
            )
            record_id = cursor.lastrowid
        return jsonify({
            'success': True,
            'record_id': record_id,
            'result_url': f'/api/records/{record_id}/result',
            'original_url': f'/api/records/{record_id}/original',
            'wm_shape': wm_shape,
            'message': f'水印嵌入成功！wm_shape={wm_shape}，提取时需要用这个值',
        })
    except Exception as error:
        for path in (original_path, result_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        return jsonify({'error': f'水印嵌入失败: {error}'}), 500


@app.route('/api/extract', methods=['POST'])
@login_required
def api_extract():
    if 'image' not in request.files:
        return jsonify({'error': '请上传图片'}), 400
    uploaded = request.files['image']
    if not uploaded.filename:
        return jsonify({'error': '未选择图片'}), 400
    wm_shape_value = request.form.get('wm_shape', '').strip()
    if not wm_shape_value:
        return jsonify({'error': '请输入水印长度 (wm_shape)，需要和嵌入时一致'}), 400
    try:
        wm_shape = int(wm_shape_value)
        pwd_img, pwd_wm = watermark_password_values(request.form)
        int(pwd_img)
        int(pwd_wm)
    except ValueError:
        return jsonify({'error': '水印长度和密码必须为数字'}), 400
    if wm_shape <= 0:
        return jsonify({'error': '水印长度 wm_shape 必须大于 0'}), 400
    note = request.form.get('note', '').strip()[:500]

    original_name, original_path = safe_upload_path(uploaded, UPLOAD_DIR)
    original_width, original_height = image_dimensions(original_path)
    if original_width is None or original_height is None:
        remove_stored_file(original_path, UPLOAD_DIR)
        return jsonify({'error': '图片无法读取，请上传有效的 PNG 或 JPG 图片', 'code': 'INVALID_IMAGE'}), 400
    result_name = f'extract-{uuid.uuid4().hex}.svg'
    result_path = os.path.join(RESULT_DIR, result_name)
    try:
        bwm = WaterMark(password_img=int(pwd_img), password_wm=int(pwd_wm))
        extracted = bwm.extract(original_path, wm_shape=wm_shape, mode='str')
        timestamp = now_iso()
        write_watermark_svg(result_path, extracted, wm_shape, timestamp)
        with db_connection() as db:
            cursor = db.execute(
                """INSERT INTO records(
                    user_id, operation, original_name, original_path, result_name, result_path,
                    watermark, wm_shape, pwd_img, pwd_wm, note, original_size, result_size,
                    original_width, original_height, result_width, result_height, created_at
                ) VALUES (?, 'extract', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    current_user_id(), original_name, original_path, result_name, result_path,
                    extracted, wm_shape, pwd_img, pwd_wm, note, os.path.getsize(original_path),
                    os.path.getsize(result_path), original_width, original_height, 960, 420, timestamp,
                ),
            )
            record_id = cursor.lastrowid
        return jsonify({
            'success': True,
            'record_id': record_id,
            'watermark': extracted,
            'original_url': f'/api/records/{record_id}/original',
            'result_url': f'/api/records/{record_id}/result',
            'result_name': result_name,
            'result_size': os.path.getsize(result_path),
            'result_width': 960,
            'result_height': 420,
        })
    except Exception as error:
        for path in (original_path, result_path):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        return jsonify({'error': f'水印提取失败: {error}'}), 500


class RecoveryRequestError(Exception):
    def __init__(self, message, status=400, code='RECOVERY_REQUEST_INVALID'):
        super().__init__(message)
        self.status = status
        self.code = code


def recovery_extract_parameters():
    wm_shape_value = request.form.get('wm_shape', '').strip()
    pwd_img, pwd_wm = watermark_password_values(request.form)
    if not wm_shape_value:
        raise RecoveryRequestError('请输入嵌入时保存的水印长度 wm_shape')
    try:
        wm_shape = int(wm_shape_value)
        pwd_img_number = int(pwd_img)
        pwd_wm_number = int(pwd_wm)
    except ValueError as error:
        raise RecoveryRequestError('水印长度和密码必须为数字') from error
    if wm_shape <= 0:
        raise RecoveryRequestError('水印长度 wm_shape 必须大于 0')
    if not 0 <= pwd_img_number <= 2**32 - 1 or not 0 <= pwd_wm_number <= 2**32 - 1:
        raise RecoveryRequestError('图片密码和水印密码必须在 0 到 4294967295 之间')
    return wm_shape, pwd_img, pwd_wm, pwd_img_number, pwd_wm_number


@app.route('/api/extract/recover', methods=['POST'])
@login_required
def api_extract_recover():
    uploaded = request.files.get('image')
    if not uploaded or not uploaded.filename:
        return jsonify({'error': '请上传经过裁剪或缩放的待提取图片'}), 400

    reference_source = request.form.get('reference_source', '').strip().lower()
    if not reference_source:
        reference_source = 'history' if request.form.get('history_record_id', '').strip() else 'manual'
    if reference_source not in ('history', 'manual'):
        return jsonify({'error': '参考图来源必须为历史记录或手动上传'}), 400

    created_files = []
    reference_record_id = None
    reference_path = None
    stored_reference_path = None
    try:
        if reference_source == 'history':
            try:
                reference_record_id = int(request.form.get('history_record_id', '').strip())
            except ValueError as error:
                raise RecoveryRequestError('请选择一条有效的历史嵌入记录') from error
            reference_record = owned_record(reference_record_id)
            if not reference_record:
                raise RecoveryRequestError('历史嵌入记录不存在', 404, 'REFERENCE_RECORD_NOT_FOUND')
            if reference_record['operation'] != 'embed':
                raise RecoveryRequestError('所选记录不是嵌入记录，不能作为完整水印参考图')
            reference_path = safe_stored_path(reference_record['result_path'], RESULT_DIR)
            if not reference_path or not os.path.isfile(reference_path):
                raise RecoveryRequestError('历史记录的完整水印结果图不存在', 404, 'REFERENCE_FILE_NOT_FOUND')
            wm_shape = reference_record['wm_shape']
            pwd_img = reference_record['pwd_img']
            pwd_wm = reference_record['pwd_wm']
            try:
                pwd_img_number = int(pwd_img)
                pwd_wm_number = int(pwd_wm)
                wm_shape = int(wm_shape)
            except (TypeError, ValueError) as error:
                raise RecoveryRequestError('历史记录保存的 wm_shape 或密码无效') from error
            if wm_shape <= 0:
                raise RecoveryRequestError('历史记录保存的 wm_shape 无效')
            reference_name = reference_record['result_name'] or '完整水印参考图.png'
        else:
            reference_upload = request.files.get('reference_image')
            if not reference_upload or not reference_upload.filename:
                raise RecoveryRequestError('请上传嵌入水印后生成的完整水印参考图')
            wm_shape, pwd_img, pwd_wm, pwd_img_number, pwd_wm_number = recovery_extract_parameters()
            reference_name, reference_path = safe_upload_path(reference_upload, UPLOAD_DIR)
            stored_reference_path = reference_path
            created_files.append((reference_path, UPLOAD_DIR))

        reference_width, reference_height = image_dimensions(reference_path)
        if reference_width is None or reference_height is None:
            raise RecoveryRequestError('完整水印参考图无法读取，请上传有效图片', 400, 'INVALID_REFERENCE_IMAGE')

        original_name, original_path = safe_upload_path(uploaded, UPLOAD_DIR)
        created_files.append((original_path, UPLOAD_DIR))
        original_width, original_height = image_dimensions(original_path)
        if original_width is None or original_height is None:
            raise RecoveryRequestError('待提取图片无法读取，请上传有效图片', 400, 'INVALID_TARGET_IMAGE')
        if original_width > reference_width or original_height > reference_height:
            raise RecoveryRequestError(
                '待提取图片尺寸大于完整水印参考图，第一版恢复提取不支持这种情况',
                400,
                'TARGET_LARGER_THAN_REFERENCE',
            )

        recovered_name = f'recovered-{uuid.uuid4().hex}.png'
        recovered_path = os.path.join(RESULT_DIR, recovered_name)
        created_files.append((recovered_path, RESULT_DIR))
        try:
            with RECOVERY_MATCH_LOCK:
                try:
                    loc, image_o_shape, score, scale = estimate_crop_parameters(
                        original_file=reference_path,
                        template_file=original_path,
                        scale=(0.5, 2),
                        search_num=200,
                    )
                finally:
                    match_template.cache_clear()
                score = float(score)
                scale = float(scale)
                loc = tuple(int(value) for value in loc)
                if not math.isfinite(score) or not math.isfinite(scale):
                    raise ValueError('non-finite match result')
                if score < RECOVERY_MIN_SCORE:
                    raise RecoveryRequestError(
                        '参考图与待提取图片无法匹配，请确认两张图片来自同一次嵌入结果',
                        422,
                        'RECOVERY_NO_MATCH',
                    )
                x1, y1, x2, y2 = loc
                if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1 or x2 > reference_width or y2 > reference_height:
                    raise ValueError('invalid recovered crop bounds')
                recover_crop(
                    template_file=original_path,
                    output_file_name=recovered_path,
                    loc=loc,
                    image_o_shape=image_o_shape,
                )
        except RecoveryRequestError:
            raise
        except (cv2.error, TypeError, ValueError, IndexError) as error:
            raise RecoveryRequestError(
                '参考图与待提取图片无法匹配，请检查图片尺寸和内容',
                422,
                'RECOVERY_MATCH_FAILED',
            ) from error

        recovered_width, recovered_height = image_dimensions(recovered_path)
        if recovered_width is None or recovered_height is None:
            raise RecoveryRequestError('恢复后的图片生成失败', 500, 'RECOVERED_IMAGE_INVALID')

        try:
            bwm = WaterMark(password_img=pwd_img_number, password_wm=pwd_wm_number)
            extracted = bwm.extract(recovered_path, wm_shape=wm_shape, mode='str')
        except Exception as error:
            raise RecoveryRequestError(
                '水印提取失败，请检查 wm_shape、图片密码和水印密码',
                422,
                'WATERMARK_EXTRACTION_FAILED',
            ) from error

        timestamp = now_iso()
        result_name = f'extract-{uuid.uuid4().hex}.svg'
        result_path = os.path.join(RESULT_DIR, result_name)
        created_files.append((result_path, RESULT_DIR))
        write_watermark_svg(result_path, extracted, wm_shape, timestamp)
        reference_size = os.path.getsize(reference_path)
        recovered_size = os.path.getsize(recovered_path)
        result_size = os.path.getsize(result_path)
        note = request.form.get('note', '').strip()[:500]
        with db_connection() as db:
            cursor = db.execute(
                """INSERT INTO records(
                    user_id, operation, original_name, original_path, result_name, result_path,
                    watermark, wm_shape, pwd_img, pwd_wm, note, original_size, result_size,
                    original_width, original_height, result_width, result_height, created_at,
                    extract_mode, reference_source, reference_record_id, reference_name, reference_path,
                    reference_size, reference_width, reference_height, recovered_name, recovered_path,
                    recovered_size, recovered_width, recovered_height, recover_score, recover_loc, recover_scale
                ) VALUES (
                    :user_id, 'extract', :original_name, :original_path, :result_name, :result_path,
                    :watermark, :wm_shape, :pwd_img, :pwd_wm, :note, :original_size, :result_size,
                    :original_width, :original_height, 960, 420, :created_at,
                    'recover', :reference_source, :reference_record_id, :reference_name, :reference_path,
                    :reference_size, :reference_width, :reference_height, :recovered_name, :recovered_path,
                    :recovered_size, :recovered_width, :recovered_height, :recover_score, :recover_loc, :recover_scale
                )""",
                {
                    'user_id': current_user_id(),
                    'original_name': original_name,
                    'original_path': original_path,
                    'result_name': result_name,
                    'result_path': result_path,
                    'watermark': extracted,
                    'wm_shape': wm_shape,
                    'pwd_img': pwd_img,
                    'pwd_wm': pwd_wm,
                    'note': note,
                    'original_size': os.path.getsize(original_path),
                    'result_size': result_size,
                    'original_width': original_width,
                    'original_height': original_height,
                    'created_at': timestamp,
                    'reference_source': reference_source,
                    'reference_record_id': reference_record_id,
                    'reference_name': reference_name,
                    'reference_path': stored_reference_path,
                    'reference_size': reference_size,
                    'reference_width': reference_width,
                    'reference_height': reference_height,
                    'recovered_name': recovered_name,
                    'recovered_path': recovered_path,
                    'recovered_size': recovered_size,
                    'recovered_width': recovered_width,
                    'recovered_height': recovered_height,
                    'recover_score': score,
                    'recover_loc': json.dumps(loc),
                    'recover_scale': scale,
                },
            )
            record_id = cursor.lastrowid

        low_confidence = score < RECOVERY_LOW_SCORE
        return jsonify({
            'success': True,
            'record_id': record_id,
            'watermark': extracted,
            'score': score,
            'loc': list(loc),
            'scale': scale,
            'low_confidence': low_confidence,
            'warning': '参考图与待提取图片可能无法匹配' if low_confidence else '',
            'reference_source': reference_source,
            'original_url': f'/api/records/{record_id}/original',
            'reference_url': f'/api/records/{record_id}/reference',
            'recovered_url': f'/api/records/{record_id}/recovered',
            'recovered_name': recovered_name,
            'recovered_size': recovered_size,
            'recovered_width': recovered_width,
            'recovered_height': recovered_height,
            'result_url': f'/api/records/{record_id}/result',
            'result_name': result_name,
            'result_size': result_size,
            'result_width': 960,
            'result_height': 420,
        })
    except RecoveryRequestError as error:
        cleanup_created_files(created_files)
        return jsonify({'error': str(error), 'code': error.code}), error.status
    except Exception:
        cleanup_created_files(created_files)
        app.logger.exception('Unexpected recovery extraction failure')
        return jsonify({'error': '恢复提取失败，请稍后重试', 'code': 'RECOVERY_FAILED'}), 500


@app.route('/api/records')
@login_required
def list_records():
    query = request.args.get('q', '').strip()
    operation = request.args.get('operation', '').strip()
    sql = 'SELECT * FROM records WHERE user_id = ?'
    params = [current_user_id()]
    if operation in ('embed', 'extract'):
        sql += ' AND operation = ?'
        params.append(operation)
    if query:
        sql += " AND (original_name LIKE ? OR result_name LIKE ? OR watermark LIKE ? OR note LIKE ? OR CAST(wm_shape AS TEXT) LIKE ? OR pwd_img LIKE ? OR pwd_wm LIKE ?)"
        pattern = f'%{query}%'
        params.extend([pattern] * 7)
    sql += ' ORDER BY created_at DESC, id DESC LIMIT 200'
    with db_connection() as db:
        rows = db.execute(sql, params).fetchall()
    return jsonify({'success': True, 'records': [record_payload(row) for row in rows], 'total': len(rows)})


@app.route('/api/records/<int:record_id>')
@login_required
def get_record(record_id):
    row = owned_record(record_id)
    if not row:
        return jsonify({'error': '记录不存在'}), 404
    return jsonify({'success': True, 'record': record_payload(row, include_secrets=True)})


@app.route('/api/records/<int:record_id>', methods=['PATCH'])
@login_required
def update_record(record_id):
    row = owned_record(record_id)
    if not row:
        return jsonify({'error': '记录不存在'}), 404
    data = request.get_json(silent=True) or {}
    note = str(data.get('note', '')).strip()[:500]
    with db_connection() as db:
        db.execute('UPDATE records SET note = ? WHERE id = ? AND user_id = ?', (note, record_id, current_user_id()))
    return jsonify({'success': True, 'note': note})


@app.route('/api/records/<int:record_id>', methods=['DELETE'])
@login_required
def delete_record(record_id):
    row = owned_record(record_id)
    if not row:
        return jsonify({'error': '记录不存在'}), 404
    with db_connection() as db:
        db.execute('DELETE FROM records WHERE id = ? AND user_id = ?', (record_id, current_user_id()))
    cleanup_created_files(record_stored_files(row))
    return jsonify({'success': True})


@app.route('/api/records/batch', methods=['DELETE'])
@login_required
def delete_records_batch():
    data = request.get_json(silent=True) or {}
    raw_ids = data.get('ids')
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({'error': '请选择要删除的记录', 'code': 'RECORD_IDS_REQUIRED'}), 400
    if len(raw_ids) > 200:
        return jsonify({'error': '单次最多删除 200 条记录', 'code': 'TOO_MANY_RECORDS'}), 400
    if any(isinstance(record_id, bool) or not isinstance(record_id, int) or record_id <= 0 for record_id in raw_ids):
        return jsonify({'error': '记录 ID 格式无效', 'code': 'INVALID_RECORD_IDS'}), 400

    record_ids = list(dict.fromkeys(raw_ids))
    placeholders = ','.join('?' for _ in record_ids)
    with db_connection() as db:
        rows = db.execute(
            f'SELECT * FROM records WHERE user_id = ? AND id IN ({placeholders})',
            [current_user_id(), *record_ids],
        ).fetchall()
        if len(rows) != len(record_ids):
            return jsonify({'error': '部分记录不存在或不属于当前用户', 'code': 'RECORDS_NOT_FOUND'}), 404
        db.execute(
            f'DELETE FROM records WHERE user_id = ? AND id IN ({placeholders})',
            [current_user_id(), *record_ids],
        )

    cleanup_created_files(path for row in rows for path in record_stored_files(row))
    return jsonify({'success': True, 'deleted': len(rows)})


def serve_record_file(record_id, kind, as_download=False):
    row = owned_record(record_id)
    if not row:
        return jsonify({'error': '记录不存在'}), 404
    if kind == 'original':
        path = safe_stored_path(row['original_path'], UPLOAD_DIR)
        filename = row['original_name']
    elif kind == 'result':
        path = safe_stored_path(row['result_path'], RESULT_DIR)
        filename = row['result_name'] or 'result.png'
    elif kind == 'recovered':
        path = safe_stored_path(row['recovered_path'], RESULT_DIR)
        filename = row['recovered_name'] or 'recovered.png'
    elif kind == 'reference':
        filename = row['reference_name'] or 'reference.png'
        path = safe_stored_path(row['reference_path'], UPLOAD_DIR)
        if not path and row['reference_record_id']:
            reference_record = owned_record(row['reference_record_id'])
            if reference_record and reference_record['operation'] == 'embed':
                path = safe_stored_path(reference_record['result_path'], RESULT_DIR)
    else:
        return jsonify({'error': '文件类型无效'}), 404
    if not path or not os.path.isfile(path):
        return jsonify({'error': '文件不存在'}), 404
    return send_file(path, as_attachment=as_download, download_name=filename)


@app.route('/api/records/<int:record_id>/original')
@login_required
def record_original(record_id):
    return serve_record_file(record_id, 'original', request.args.get('download') == '1')


@app.route('/api/records/<int:record_id>/result')
@login_required
def record_result(record_id):
    return serve_record_file(record_id, 'result', request.args.get('download') == '1')


@app.route('/api/records/<int:record_id>/reference')
@login_required
def record_reference(record_id):
    return serve_record_file(record_id, 'reference', request.args.get('download') == '1')


@app.route('/api/records/<int:record_id>/recovered')
@login_required
def record_recovered(record_id):
    return serve_record_file(record_id, 'recovered', request.args.get('download') == '1')


@app.route('/api/result/<filename>')
@login_required
def get_legacy_result(filename):
    """Serve legacy output files and newly stored files by basename."""
    clean_name = secure_filename(filename)
    if not clean_name or clean_name != filename:
        return jsonify({'error': '文件不存在'}), 404
    with db_connection() as db:
        rows = db.execute(
            'SELECT result_path, recovered_path FROM records WHERE user_id = ?',
            (current_user_id(),),
        ).fetchall()
    for row in rows:
        for column in ('result_path', 'recovered_path'):
            stored_path = row[column]
            if not stored_path or os.path.basename(stored_path) != clean_name:
                continue
            for directory in (RESULT_DIR, LEGACY_OUTPUT_DIR):
                path = safe_stored_path(stored_path, directory)
                if path and os.path.isfile(path):
                    return send_file(path)
    return jsonify({'error': '文件不存在'}), 404


@app.route('/api/health')
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8655, debug=False)
