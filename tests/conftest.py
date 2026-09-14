import os
import tempfile
from pathlib import Path

import pytest


# app.py initializes storage during import. Point that initialization at an
# isolated directory so importing the test suite never touches local user data.
IMPORT_DATA_CONTEXT = tempfile.TemporaryDirectory(prefix='watermark-webui-tests-')
IMPORT_DATA_DIR = Path(IMPORT_DATA_CONTEXT.name)
os.environ['WATERMARK_DATA_DIR'] = str(IMPORT_DATA_DIR)

import app as app_module  # noqa: E402


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    data_dir = tmp_path / 'data'
    upload_dir = data_dir / 'uploads'
    result_dir = data_dir / 'results'
    legacy_dir = tmp_path / 'output'
    for directory in (data_dir, upload_dir, result_dir, legacy_dir):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(app_module, 'DATA_DIR', str(data_dir))
    monkeypatch.setattr(app_module, 'UPLOAD_DIR', str(upload_dir))
    monkeypatch.setattr(app_module, 'RESULT_DIR', str(result_dir))
    monkeypatch.setattr(app_module, 'LEGACY_OUTPUT_DIR', str(legacy_dir))
    monkeypatch.setattr(app_module, 'DB_PATH', str(data_dir / 'watermark.db'))
    app_module.app.config.update(TESTING=True, SECRET_KEY='test-secret')
    app_module.init_db()
    return app_module


@pytest.fixture
def client(app_env):
    return app_env.app.test_client()
