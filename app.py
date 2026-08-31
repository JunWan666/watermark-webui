#!/usr/bin/env python3
"""Blind Watermark WebUI backend with local SQLite persistence."""
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from functools import wraps
from xml.sax.saxutils import escape as xml_escape

# Use system packages (numpy, cv2, pywt installed via apt when available).
sys.path.insert(0, '/usr/lib/python3/dist-packages')

import cv2
from blind_watermark import WaterMark
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


def db_connection():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    return connection


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
    payload = {
        'id': row['id'],
        'operation': row['operation'],
        'operation_label': '嵌入水印' if row['operation'] == 'embed' else '提取水印',
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
    uploaded_file.save(path)
    return original_name, path


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
    pwd_img = request.form.get('pwd_img', '1234') or '1234'
    pwd_wm = request.form.get('pwd_wm', '1234') or '1234'
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
        pwd_img = request.form.get('pwd_img', '1234') or '1234'
        pwd_wm = request.form.get('pwd_wm', '1234') or '1234'
        int(pwd_img)
        int(pwd_wm)
    except ValueError:
        return jsonify({'error': '水印长度和密码必须为数字'}), 400
    note = request.form.get('note', '').strip()[:500]

    original_name, original_path = safe_upload_path(uploaded, UPLOAD_DIR)
    original_width, original_height = image_dimensions(original_path)
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
    for path in (row['original_path'], row['result_path']):
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    return jsonify({'success': True})


def serve_record_file(record_id, kind, as_download=False):
    row = owned_record(record_id)
    if not row:
        return jsonify({'error': '记录不存在'}), 404
    path = row['original_path'] if kind == 'original' else row['result_path']
    filename = row['original_name'] if kind == 'original' else (row['result_name'] or 'result.png')
    if not path or not os.path.exists(path):
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


@app.route('/api/result/<filename>')
@login_required
def get_legacy_result(filename):
    """Serve legacy output files and newly stored files by basename."""
    clean_name = secure_filename(filename)
    for directory in (RESULT_DIR, LEGACY_OUTPUT_DIR):
        path = os.path.join(directory, clean_name)
        if os.path.exists(path):
            return send_file(path, mimetype='image/png')
    return jsonify({'error': '文件不存在'}), 404


@app.route('/api/health')
def health():
    return jsonify({'status': 'ok'})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8655, debug=False)
