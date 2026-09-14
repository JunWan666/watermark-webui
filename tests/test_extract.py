import io
import os
import shutil
import sqlite3

import cv2
import numpy as np
import pytest
from blind_watermark import WaterMark
from werkzeug.security import generate_password_hash


WATERMARK = 'OK'
PWD_IMG = '1234'
PWD_WM = '5678'


@pytest.fixture(scope='session')
def watermarked_assets(tmp_path_factory):
    directory = tmp_path_factory.mktemp('watermarked-assets')
    height, width = 512, 640
    y, x = np.mgrid[:height, :width]
    image = np.stack(
        (
            (x * 3 + y) % 256,
            (x + y * 2) % 256,
            ((x // 16 % 2) * 120 + (y // 16 % 2) * 80) % 256,
        ),
        axis=2,
    ).astype(np.uint8)
    original = directory / 'original.png'
    reference = directory / 'reference.png'
    crop = directory / 'crop.png'
    crop_scaled = directory / 'crop-scaled.png'
    unrelated = directory / 'unrelated.png'
    cv2.imwrite(str(original), image)

    bwm = WaterMark(password_img=int(PWD_IMG), password_wm=int(PWD_WM))
    bwm.read_img(str(original))
    bwm.read_wm(WATERMARK, mode='str')
    bwm.embed(str(reference))
    wm_shape = len(bwm.wm_bit)

    reference_image = cv2.imread(str(reference))
    cropped = reference_image[31:481, :]
    cv2.imwrite(str(crop), cropped)
    cv2.imwrite(str(crop_scaled), cv2.resize(cropped, (576, 405)))
    rng = np.random.default_rng(20260904)
    cv2.imwrite(str(unrelated), rng.integers(0, 256, (220, 260, 3), dtype=np.uint8))
    return {
        'original': original,
        'reference': reference,
        'crop': crop,
        'crop_scaled': crop_scaled,
        'unrelated': unrelated,
        'wm_shape': wm_shape,
    }


def upload(path, filename=None):
    return io.BytesIO(path.read_bytes()), filename or path.name


def create_user(app_env, username='tester'):
    timestamp = app_env.now_iso()
    with app_env.db_connection() as db:
        cursor = db.execute(
            'INSERT INTO users(username, password_hash, created_at, updated_at) VALUES (?, ?, ?, ?)',
            (username, generate_password_hash('password'), timestamp, timestamp),
        )
        return cursor.lastrowid


def login_as(client, user_id):
    with client.session_transaction() as session:
        session['user_id'] = user_id


def insert_embed_record(app_env, user_id, assets, result_path=None):
    original_path = os.path.join(app_env.UPLOAD_DIR, f'user-{user_id}-original.png')
    result_path = result_path or os.path.join(app_env.RESULT_DIR, f'user-{user_id}-reference.png')
    shutil.copyfile(assets['original'], original_path)
    if not os.path.exists(result_path):
        shutil.copyfile(assets['reference'], result_path)
    with app_env.db_connection() as db:
        cursor = db.execute(
            """INSERT INTO records(
                user_id, operation, original_name, original_path, result_name, result_path,
                watermark, wm_shape, pwd_img, pwd_wm, note, original_size, result_size,
                original_width, original_height, result_width, result_height, created_at
            ) VALUES (?, 'embed', ?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, 640, 512, 640, 512, ?)""",
            (
                user_id,
                'original.png',
                original_path,
                os.path.basename(result_path),
                result_path,
                WATERMARK,
                assets['wm_shape'],
                PWD_IMG,
                PWD_WM,
                os.path.getsize(original_path),
                os.path.getsize(result_path),
                app_env.now_iso(),
            ),
        )
        return cursor.lastrowid


def recovery_form(assets, target_key='crop'):
    return {
        'reference_source': 'manual',
        'reference_image': upload(assets['reference']),
        'image': upload(assets[target_key]),
        'wm_shape': str(assets['wm_shape']),
        'pwd_img': PWD_IMG,
        'pwd_wm': PWD_WM,
    }


def test_normal_extract_regression(app_env, client, watermarked_assets):
    user_id = create_user(app_env)
    login_as(client, user_id)
    embed_response = client.post(
        '/api/embed',
        data={
            'image': upload(watermarked_assets['original']),
            'text': WATERMARK,
            'password': PWD_IMG,
        },
        content_type='multipart/form-data',
    )
    assert embed_response.status_code == 200
    embed_payload = embed_response.get_json()
    with app_env.db_connection() as db:
        embedded_path = db.execute(
            'SELECT result_path FROM records WHERE id = ?',
            (embed_payload['record_id'],),
        ).fetchone()['result_path']

    extract_response = client.post(
        '/api/extract',
        data={
            'image': upload(type(watermarked_assets['reference'])(embedded_path)),
            'wm_shape': str(embed_payload['wm_shape']),
            'password': PWD_IMG,
        },
        content_type='multipart/form-data',
    )
    assert extract_response.status_code == 200
    assert extract_response.get_json()['watermark'] == WATERMARK


def test_manual_crop_recovery(app_env, client, watermarked_assets):
    login_as(client, create_user(app_env))
    response = client.post(
        '/api/extract/recover',
        data=recovery_form(watermarked_assets),
        content_type='multipart/form-data',
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['watermark'] == WATERMARK
    assert payload['score'] > 0.95
    assert payload['loc'] == [0, 31, 640, 481]
    assert payload['scale'] == pytest.approx(1.0, abs=0.002)
    assert (payload['recovered_width'], payload['recovered_height']) == (640, 512)


def test_crop_then_scale_recovery(app_env, client, watermarked_assets):
    login_as(client, create_user(app_env))
    response = client.post(
        '/api/extract/recover',
        data=recovery_form(watermarked_assets, 'crop_scaled'),
        content_type='multipart/form-data',
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['watermark'] == WATERMARK
    assert payload['score'] > 0.90
    assert payload['loc'] == [0, 31, 640, 481]
    assert payload['scale'] == pytest.approx(640 / 576, abs=0.002)


def test_history_embed_record_supplies_reference_and_secrets(app_env, client, watermarked_assets):
    user_id = create_user(app_env)
    record_id = insert_embed_record(app_env, user_id, watermarked_assets)
    login_as(client, user_id)
    response = client.post(
        '/api/extract/recover',
        data={
            'reference_source': 'history',
            'history_record_id': str(record_id),
            'image': upload(watermarked_assets['crop']),
            'wm_shape': '999',
            'pwd_img': '1',
            'pwd_wm': '2',
        },
        content_type='multipart/form-data',
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['watermark'] == WATERMARK
    with app_env.db_connection() as db:
        record = db.execute('SELECT * FROM records WHERE id = ?', (payload['record_id'],)).fetchone()
    assert record['extract_mode'] == 'recover'
    assert record['reference_source'] == 'history'
    assert record['reference_record_id'] == record_id
    assert record['wm_shape'] == watermarked_assets['wm_shape']
    assert record['note'] == ''
    assert record['recover_score'] > 0.90
    assert record['recover_loc']
    assert record['recover_scale'] > 0


def test_manual_reference_is_saved_with_recovery_record(app_env, client, watermarked_assets):
    login_as(client, create_user(app_env))
    response = client.post(
        '/api/extract/recover',
        data={**recovery_form(watermarked_assets), 'note': '手动参考图'},
        content_type='multipart/form-data',
    )
    payload = response.get_json()
    assert response.status_code == 200
    detail = client.get(f"/api/records/{payload['record_id']}").get_json()['record']
    assert detail['operation_label'] == '恢复提取'
    assert detail['reference_source'] == 'manual'
    assert detail['note'] == '手动参考图'
    assert detail['reference_url']
    assert detail['recovered_url']
    assert client.get(detail['reference_url']).status_code == 200
    assert client.get(detail['recovered_url']).status_code == 200


def test_invalid_image_and_no_match_are_rejected_and_cleaned(app_env, client, watermarked_assets):
    login_as(client, create_user(app_env))
    invalid = client.post(
        '/api/extract/recover',
        data={
            **recovery_form(watermarked_assets),
            'image': (io.BytesIO(b'not an image'), 'broken.png'),
        },
        content_type='multipart/form-data',
    )
    assert invalid.status_code == 400
    assert invalid.get_json()['code'] == 'INVALID_TARGET_IMAGE'
    assert list(os.scandir(app_env.UPLOAD_DIR)) == []
    assert list(os.scandir(app_env.RESULT_DIR)) == []

    no_match = client.post(
        '/api/extract/recover',
        data=recovery_form(watermarked_assets, 'unrelated'),
        content_type='multipart/form-data',
    )
    assert no_match.status_code == 422
    assert no_match.get_json()['code'] == 'RECOVERY_NO_MATCH'
    assert list(os.scandir(app_env.UPLOAD_DIR)) == []
    assert list(os.scandir(app_env.RESULT_DIR)) == []


def test_low_confidence_result_returns_warning(app_env, client, watermarked_assets, monkeypatch):
    login_as(client, create_user(app_env))

    def estimate_fixture(**_kwargs):
        return (0, 0, 640, 512), (512, 640), 0.3, 1.0

    def recover_fixture(template_file, output_file_name, **_kwargs):
        shutil.copyfile(template_file, output_file_name)

    monkeypatch.setattr(app_env, 'estimate_crop_parameters', estimate_fixture)
    monkeypatch.setattr(app_env, 'recover_crop', recover_fixture)
    response = client.post(
        '/api/extract/recover',
        data={
            **recovery_form(watermarked_assets),
            'image': upload(watermarked_assets['reference']),
        },
        content_type='multipart/form-data',
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload['watermark'] == WATERMARK
    assert payload['low_confidence'] is True
    assert payload['warning'] == '参考图与待提取图片可能无法匹配'


def test_target_larger_than_reference_and_invalid_parameters_are_rejected(app_env, client, watermarked_assets):
    login_as(client, create_user(app_env))
    valid, encoded = cv2.imencode('.png', np.zeros((513, 641, 3), dtype=np.uint8))
    assert valid
    too_large = client.post(
        '/api/extract/recover',
        data={
            **recovery_form(watermarked_assets),
            'image': (io.BytesIO(encoded.tobytes()), 'too-large.png'),
        },
        content_type='multipart/form-data',
    )
    assert too_large.status_code == 400
    assert too_large.get_json()['code'] == 'TARGET_LARGER_THAN_REFERENCE'
    assert list(os.scandir(app_env.UPLOAD_DIR)) == []

    invalid_parameters = client.post(
        '/api/extract/recover',
        data={
            **recovery_form(watermarked_assets),
            'pwd_img': 'not-a-number',
        },
        content_type='multipart/form-data',
    )
    assert invalid_parameters.status_code == 400
    assert invalid_parameters.get_json()['code'] == 'RECOVERY_REQUEST_INVALID'
    assert list(os.scandir(app_env.UPLOAD_DIR)) == []


def test_other_users_history_record_is_not_accessible(app_env, client, watermarked_assets):
    owner_id = create_user(app_env, 'owner')
    other_id = create_user(app_env, 'other')
    record_id = insert_embed_record(app_env, owner_id, watermarked_assets)
    login_as(client, other_id)
    response = client.post(
        '/api/extract/recover',
        data={
            'reference_source': 'history',
            'history_record_id': str(record_id),
            'image': upload(watermarked_assets['crop']),
        },
        content_type='multipart/form-data',
    )
    assert response.status_code == 404
    assert response.get_json()['code'] == 'REFERENCE_RECORD_NOT_FOUND'
    assert client.get(f'/api/records/{record_id}/result').status_code == 404


def test_history_reference_path_must_stay_in_result_storage(app_env, client, watermarked_assets):
    user_id = create_user(app_env)
    outside_path = str(watermarked_assets['reference'])
    record_id = insert_embed_record(app_env, user_id, watermarked_assets, result_path=outside_path)
    login_as(client, user_id)
    response = client.post(
        '/api/extract/recover',
        data={
            'reference_source': 'history',
            'history_record_id': str(record_id),
            'image': upload(watermarked_assets['crop']),
        },
        content_type='multipart/form-data',
    )
    assert response.status_code == 404
    assert response.get_json()['code'] == 'REFERENCE_FILE_NOT_FOUND'


def test_batch_delete_is_atomic_and_scoped_to_current_user(app_env, client, watermarked_assets):
    owner_id = create_user(app_env, 'batch-owner')
    other_id = create_user(app_env, 'batch-other')
    first_id = insert_embed_record(app_env, owner_id, watermarked_assets)
    second_id = insert_embed_record(app_env, owner_id, watermarked_assets)
    other_record_id = insert_embed_record(app_env, other_id, watermarked_assets)
    owner_original = os.path.join(app_env.UPLOAD_DIR, f'user-{owner_id}-original.png')
    owner_result = os.path.join(app_env.RESULT_DIR, f'user-{owner_id}-reference.png')
    other_result = os.path.join(app_env.RESULT_DIR, f'user-{other_id}-reference.png')
    login_as(client, owner_id)

    invalid = client.delete('/api/records/batch', json={'ids': []})
    assert invalid.status_code == 400
    assert invalid.get_json()['code'] == 'RECORD_IDS_REQUIRED'

    mixed = client.delete('/api/records/batch', json={'ids': [first_id, other_record_id]})
    assert mixed.status_code == 404
    assert mixed.get_json()['code'] == 'RECORDS_NOT_FOUND'
    with app_env.db_connection() as db:
        assert db.execute('SELECT COUNT(*) FROM records WHERE id IN (?, ?)', (first_id, second_id)).fetchone()[0] == 2

    deleted = client.delete('/api/records/batch', json={'ids': [first_id, second_id, second_id]})
    assert deleted.status_code == 200
    assert deleted.get_json()['deleted'] == 2
    with app_env.db_connection() as db:
        assert db.execute('SELECT COUNT(*) FROM records WHERE user_id = ?', (owner_id,)).fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM records WHERE id = ?', (other_record_id,)).fetchone()[0] == 1
    assert not os.path.exists(owner_original)
    assert not os.path.exists(owner_result)
    assert os.path.exists(other_result)


def test_extract_page_contains_both_modes(client):
    response = client.get('/extract')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert '普通提取' in html
    assert '恢复提取' in html
    assert '不是未添加水印的原始图片' in html
    assert 'id="historyReferencePicker"' in html
    assert html.index('id="historyReferencePicker"') < html.index('class="recovery-input-grid"')
    assert html.count('id="fuRR"') == 1
    assert html.index('id="recoverHistoryRecord"') < html.index('id="fuRR"')
    assert 'data-recovery-source' not in html
    assert '不使用历史记录，直接上传参考图' in html
    assert html.count('图片和水印密码') >= 3
    assert 'id="clearE"' in html and 'id="clearX"' in html and 'id="clearR"' in html
    assert '已有账户？返回登录' not in html
    assert 'id="authSwitch"' not in html
    assert 'class="record-fact"' in html
    assert 'data-record-preview=' in html
    assert 'class="record-meta-item"' not in html
    assert 'actions-heading' not in html
    assert 'id="selectAllRecords"' in html
    assert 'id="batchDeleteRecords"' in html
    assert '/api/records/batch' in html
    assert 'detail-images-recovery' in html
    assert 'min(1480px, calc(100vw - 32px))' in html


def test_first_user_initialization_then_login_only(client):
    initial = client.get('/api/auth/status').get_json()
    assert initial == {'authenticated': False, 'has_users': False, 'user': None}

    created = client.post('/api/auth/register', json={'username': 'first-user', 'password': 'secret1'})
    assert created.status_code == 200
    assert client.get('/api/auth/status').get_json()['authenticated'] is True

    assert client.post('/api/auth/logout').status_code == 200
    after_logout = client.get('/api/auth/status').get_json()
    assert after_logout == {'authenticated': False, 'has_users': True, 'user': None}
    assert client.post('/api/auth/register', json={'username': 'second-user', 'password': 'secret2'}).status_code == 409


def test_database_migration_preserves_existing_records(app_env):
    os.remove(app_env.DB_PATH)
    db = sqlite3.connect(app_env.DB_PATH)
    try:
        db.executescript(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE records (
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
            INSERT INTO users VALUES (1, 'legacy', 'hash', '2026-01-01', '2026-01-01');
            INSERT INTO records VALUES (
                1, 1, 'embed', 'old.png', 'old-input.png', 'old-result.png',
                'old-output.png', 'legacy-watermark', 42, '1234', '5678',
                'keep me', 10, 20, 100, 80, 100, 80, '2026-01-01'
            );
            """
        )
        db.commit()
    finally:
        db.close()

    app_env.init_db()
    with app_env.db_connection() as db:
        record = db.execute('SELECT * FROM records WHERE id = 1').fetchone()
        columns = {row['name'] for row in db.execute('PRAGMA table_info(records)')}
    assert record['watermark'] == 'legacy-watermark'
    assert record['note'] == 'keep me'
    assert record['extract_mode'] == 'normal'
    assert {'reference_path', 'recovered_path', 'recover_score', 'recover_loc', 'recover_scale'} <= columns
