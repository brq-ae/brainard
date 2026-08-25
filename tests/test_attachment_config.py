"""app/config.py -- Settings, focused on the ADR-0012 room-attachment knobs
(ATTACHMENT_MAX_FILE_BYTES / ATTACHMENT_MAX_FILES_PER_ROOM /
ATTACHMENT_GLOBAL_CEILING_BYTES / ATTACHMENT_FREE_DISK_FLOOR_BYTES /
ATTACHMENT_GRACE_PERIOD_DAYS / ATTACHMENT_STORAGE_DIR). Same posture as
tests/test_config.py's LLM timeout tests: `Settings()` is constructed
directly (not the process-wide cached `get_settings()`) so each test gets a
fresh read of whatever env vars it set, and every bound must fail loudly at
construction when out of range rather than being silently clamped.
"""

import pytest
from pydantic import ValidationError

from app.config import Settings


def _clear_attachment_env(monkeypatch):
    for name in (
        "ATTACHMENT_MAX_FILE_BYTES",
        "ATTACHMENT_MAX_FILES_PER_ROOM",
        "ATTACHMENT_GLOBAL_CEILING_BYTES",
        "ATTACHMENT_FREE_DISK_FLOOR_BYTES",
        "ATTACHMENT_GRACE_PERIOD_DAYS",
        "ATTACHMENT_STORAGE_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def test_attachment_defaults(monkeypatch):
    _clear_attachment_env(monkeypatch)

    settings = Settings()

    assert settings.attachment_max_file_bytes == 10 * 1024 * 1024
    assert settings.attachment_max_files_per_room == 10
    assert settings.attachment_global_ceiling_bytes == 500 * 1024 * 1024
    assert settings.attachment_free_disk_floor_bytes == 2 * 1024 * 1024 * 1024
    assert settings.attachment_grace_period_days == 7
    assert settings.attachment_storage_dir == "/data/attachments"


@pytest.mark.parametrize("value", ["0", "-1", "1023", str(100 * 1024 * 1024 + 1)])
def test_max_file_bytes_out_of_range_rejected(monkeypatch, value):
    _clear_attachment_env(monkeypatch)
    monkeypatch.setenv("ATTACHMENT_MAX_FILE_BYTES", value)
    with pytest.raises(ValidationError, match="attachment_max_file_bytes"):
        Settings()


@pytest.mark.parametrize("value", ["1024", str(100 * 1024 * 1024), "10485760"])
def test_max_file_bytes_boundary_and_default_accepted(monkeypatch, value):
    _clear_attachment_env(monkeypatch)
    monkeypatch.setenv("ATTACHMENT_MAX_FILE_BYTES", value)
    settings = Settings()
    assert settings.attachment_max_file_bytes == int(value)


@pytest.mark.parametrize("value", ["0", "-1", "1001"])
def test_max_files_per_room_out_of_range_rejected(monkeypatch, value):
    _clear_attachment_env(monkeypatch)
    monkeypatch.setenv("ATTACHMENT_MAX_FILES_PER_ROOM", value)
    with pytest.raises(ValidationError, match="attachment_max_files_per_room"):
        Settings()


@pytest.mark.parametrize("value", ["1", "1000", "10"])
def test_max_files_per_room_boundary_and_default_accepted(monkeypatch, value):
    _clear_attachment_env(monkeypatch)
    monkeypatch.setenv("ATTACHMENT_MAX_FILES_PER_ROOM", value)
    settings = Settings()
    assert settings.attachment_max_files_per_room == int(value)


@pytest.mark.parametrize("value", ["0", str(1024 * 1024 - 1), str(1024**4 + 1)])
def test_global_ceiling_out_of_range_rejected(monkeypatch, value):
    _clear_attachment_env(monkeypatch)
    monkeypatch.setenv("ATTACHMENT_GLOBAL_CEILING_BYTES", value)
    with pytest.raises(ValidationError, match="attachment_global_ceiling_bytes"):
        Settings()


@pytest.mark.parametrize("value", [str(99 * 1024 * 1024), str(1024**4 + 1)])
def test_free_disk_floor_out_of_range_rejected(monkeypatch, value):
    _clear_attachment_env(monkeypatch)
    monkeypatch.setenv("ATTACHMENT_FREE_DISK_FLOOR_BYTES", value)
    with pytest.raises(ValidationError, match="attachment_free_disk_floor_bytes"):
        Settings()


@pytest.mark.parametrize("value", ["-1", "366"])
def test_grace_period_out_of_range_rejected(monkeypatch, value):
    _clear_attachment_env(monkeypatch)
    monkeypatch.setenv("ATTACHMENT_GRACE_PERIOD_DAYS", value)
    with pytest.raises(ValidationError, match="attachment_grace_period_days"):
        Settings()


@pytest.mark.parametrize("value", ["0", "365", "7"])
def test_grace_period_boundary_and_default_accepted(monkeypatch, value):
    _clear_attachment_env(monkeypatch)
    monkeypatch.setenv("ATTACHMENT_GRACE_PERIOD_DAYS", value)
    settings = Settings()
    assert settings.attachment_grace_period_days == int(value)


def test_storage_dir_empty_string_rejected(monkeypatch):
    _clear_attachment_env(monkeypatch)
    monkeypatch.setenv("ATTACHMENT_STORAGE_DIR", "")
    with pytest.raises(ValidationError, match="attachment_storage_dir"):
        Settings()


def test_storage_dir_override_accepted(monkeypatch):
    _clear_attachment_env(monkeypatch)
    monkeypatch.setenv("ATTACHMENT_STORAGE_DIR", "/custom/path")
    settings = Settings()
    assert settings.attachment_storage_dir == "/custom/path"
