from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtWidgets import QDialog

from backend_fakes import FakeAuth, FakeRecorder, FakeYouTube
from yt_rec.backend.controller import WatchController
from yt_rec.backend.recorder import EngineRecorder
from yt_rec.backend.selection import MemorySelectionStore
from yt_rec.backend.source import BackendSource
from yt_rec.backend.tokens import MemoryTokenStore
from yt_rec.recording.options import RecordingOptions, load_settings, save_settings, validate_output_dir
from yt_rec.state import commands as cmd
from yt_rec.state import events as ev
from yt_rec.state.models import CompletedRecording
from yt_rec.state.store import EventSource
from yt_rec.ui.settings import SettingsDialog


def test_all_settings_roundtrip_and_old_json_defaults(tmp_path):
    path = tmp_path / "settings.json"
    options = RecordingOptions(
        output_dir=tmp_path, max_height=720, max_recordings=5,
        poll_interval_seconds=300, autostart=True, start_hidden=True,
        minimize_to_tray=True, log_retention_days=30, notifications_enabled=False,
    )
    save_settings(options, path)
    assert load_settings(path) == options
    old = RecordingOptions.from_dict({"output_dir": str(tmp_path), "start_hidden": True})
    assert old.max_recordings == 2
    assert old.minimize_to_tray is False and old.start_hidden is True
    save_settings(options.with_(minimize_to_tray=False), path)
    assert load_settings(path).minimize_to_tray is False
    path.write_text("[]", encoding="utf-8")
    assert load_settings(path, default=options) == options


@pytest.mark.parametrize("values", [
    {"max_recordings": 0}, {"max_recordings": -1}, {"max_recordings": 17},
    {"max_recordings": True}, {"poll_interval_seconds": 29},
    {"poll_interval_seconds": 3601}, {"log_retention_days": 0},
    {"autostart": "true"}, {"max_height": 0},
    {"minimize_to_tray": "true"}, {"minimize_to_tray": 1},
    {"minimize_to_tray": 0}, {"minimize_to_tray": None},
])
def test_invalid_settings_rejected(tmp_path, values):
    with pytest.raises(ValueError):
        RecordingOptions(output_dir=tmp_path, **values)
    with pytest.raises(ValueError):
        RecordingOptions.from_dict({"output_dir": str(tmp_path), **values})


def test_output_directory_must_exist_and_be_writable(tmp_path, monkeypatch):
    assert validate_output_dir(tmp_path) == tmp_path.resolve()
    with pytest.raises(ValueError, match="존재"):
        validate_output_dir(tmp_path / "missing")
    def denied(**kwargs):
        raise PermissionError("read only")
    monkeypatch.setattr("yt_rec.recording.options.tempfile.TemporaryFile", denied)
    with pytest.raises(ValueError, match="쓸 수"):
        validate_output_dir(tmp_path)


def make_controller(options, saver, emit, *, recorder=None, youtube=None, clock=None):
    return WatchController(
        emit=emit, auth=FakeAuth(), tokens=MemoryTokenStore(),
        selection=MemorySelectionStore(["UC1"]), recorder=recorder or FakeRecorder(),
        youtube_factory=lambda _: youtube or FakeYouTube(), options=options,
        settings_saver=saver, clock=clock,
    )


def test_save_disconnected_ack_persist_and_failure_does_not_apply(tmp_path, state):
    path = tmp_path / "settings.json"
    original = RecordingOptions(output_dir=tmp_path)
    recorder = FakeRecorder()
    controller = make_controller(original, lambda options: save_settings(options, path),
                                 state.apply, recorder=recorder)
    source = BackendSource(controller, poll_interval_ms=0)
    state.attach(source)
    state.command_requested.connect(source.handle_command)
    source.start()
    errors = []
    state.settings_save_failed.connect(errors.append)
    assert state.settings == original
    assert state.update_settings(max_recordings=4, poll_interval_seconds=300, minimize_to_tray=True)
    assert load_settings(path).max_recordings == 4
    assert state.settings.poll_interval_seconds == 300
    assert state.settings.minimize_to_tray is True
    assert load_settings(path).minimize_to_tray is True
    assert recorder.option_updates[-1]["minimize_to_tray"] is True
    saved_bytes = path.read_bytes()
    applied = len(recorder.option_updates)
    state.update_settings(max_recordings=0)
    assert errors
    assert path.read_bytes() == saved_bytes
    assert len(recorder.option_updates) == applied
    controller._settings_saver = lambda _: (_ for _ in ()).throw(OSError("disk full"))
    state.update_settings(max_recordings=3, minimize_to_tray=False)
    assert errors[-1] == "disk full"
    assert state.settings.max_recordings == 4
    assert state.settings.minimize_to_tray is True
    assert load_settings(path).minimize_to_tray is True
    assert len(recorder.option_updates) == applied
    source.stop()


def test_dialog_cancel_validation_browse_and_ack(tmp_path, state, monkeypatch):
    source = EventSource()
    state.attach(source)
    state.apply(ev.SettingsChanged(RecordingOptions(output_dir=tmp_path)))
    commands = []
    state.command_requested.connect(commands.append)
    dialog = SettingsDialog(state)
    dialog.show()
    assert dialog.isModal()
    assert dialog.save_button.isEnabled()
    assert dialog.minimize_to_tray_check.text() == "최소화하면 트레이로 보내기"
    assert not dialog.minimize_to_tray_check.isChecked()
    dialog.minimize_to_tray_check.setChecked(True)
    assert dialog.space_label.text() != "확인할 수 없음"
    dialog.output_edit.setText(str(tmp_path / "missing"))
    assert not dialog.save_button.isEnabled()
    assert "존재" in dialog.error_label.text()
    monkeypatch.setattr("yt_rec.ui.settings.QFileDialog.getExistingDirectory", lambda *args: str(tmp_path))
    dialog._browse()
    dialog.max_recordings_spin.setValue(0)
    assert dialog.max_recordings_spin.value() == 1
    dialog.reject()
    assert commands == []
    assert state.settings.max_recordings == 2
    assert state.settings.minimize_to_tray is False
    dialog = SettingsDialog(state)
    dialog.show()
    dialog.max_recordings_spin.setValue(3)
    dialog.minimize_to_tray_check.setChecked(True)
    dialog._save()
    assert commands[-1].values["max_recordings"] == 3
    assert commands[-1].values["minimize_to_tray"] is True
    assert state.settings.minimize_to_tray is False
    assert dialog.result() != QDialog.DialogCode.Accepted
    state.apply(ev.SettingsSaveFailed("permission denied"))
    assert dialog.isEnabled()
    assert "permission denied" in dialog.error_label.text()
    dialog._save()
    state.apply(ev.SettingsChanged(state.settings.with_(max_recordings=3, minimize_to_tray=True)))
    assert dialog.result() == QDialog.DialogCode.Accepted
    reopened = SettingsDialog(state)
    assert reopened.minimize_to_tray_check.isChecked()
    reopened.reject()


def open_dialog(state, tmp_path, options=None):
    """백엔드가 붙어 설정이 도착한 상태의 모달."""
    state.attach(EventSource())
    state.apply(ev.SettingsChanged(options or RecordingOptions(output_dir=tmp_path)))
    return SettingsDialog(state)


def test_파일명_규칙을_토큰으로_배치한다(tmp_path, state):
    """사용자가 `[영상고유url키]` 를 손으로 칠 일이 없어야 한다 (#92)."""
    commands = []
    state.command_requested.connect(commands.append)
    dialog = open_dialog(state, tmp_path)
    assert dialog.template_edit.text() == "[YYYY-MM-DD]_[영상제목]"

    dialog.template_edit.setText("")
    dialog.token_combo.setCurrentIndex(dialog.token_combo.findData("[채널명]"))
    assert dialog.template_edit.text() == "[채널명]"
    assert dialog.token_combo.currentIndex() == 0  # 다시 고를 수 있게 되돌아온다

    dialog.template_edit.setText("[YYMMDD]_[채널명]_[영상제목]_([영상고유url키])")
    assert dialog.preview_label.text() == "260921_침착맨_오늘도 한다_(EYEAaG3cxME).mp4"
    dialog._save()
    assert commands[-1].values["filename_template"] == "[YYMMDD]_[채널명]_[영상제목]_([영상고유url키])"


def test_모르는_토큰은_어느_것인지_알려_주고_저장을_막는다(tmp_path, state):
    commands = []
    state.command_requested.connect(commands.append)
    dialog = open_dialog(state, tmp_path)

    dialog.template_edit.setText("[YYMMDD]_[없는거]")
    assert not dialog.save_button.isEnabled()
    assert "[없는거]" in dialog.error_label.text()
    assert dialog.preview_label.text() == "—"
    dialog._save()
    assert commands == []

    dialog.template_edit.setText("   ")
    assert not dialog.save_button.isEnabled()

    dialog.template_edit.setText("[YYMMDD]_[영상제목]")
    assert dialog.save_button.isEnabled()
    assert dialog.error_label.text() == ""


def test_미리보기는_최근_완료된_녹화로_그린다(tmp_path, state):
    kst = timezone(timedelta(hours=9))
    dialog = open_dialog(state, tmp_path)
    state.apply(ev.CompletedChanged((
        CompletedRecording(
            recording_id="zoYkEERlM0w", title="어제 방송", channel_name="행백TV",
            finished_at=datetime(2026, 8, 11, 20, 0, tzinfo=kst),
        ),
    )))

    dialog.template_edit.setText("[YYMMDD] [채널명] [영상제목] ([영상고유url키])")
    assert dialog.preview_label.text() == "260811 행백TV 어제 방송 (zoYkEERlM0w).mp4"


def test_recording_limit_and_new_options_only_affect_next_recording(tmp_path):
    engines = []
    class BlockingEngine:
        def __init__(self, options, on_event=None):
            self.options = options
            self.stopped = threading.Event()
            engines.append(self)
        def clear_stop(self):
            pass
        def request_stop(self):
            self.stopped.set()
        def record(self, video_id):
            assert self.stopped.wait(5)
            return type("Result", (), {"succeeded": True})()
    old = RecordingOptions(output_dir=tmp_path, max_recordings=1)
    recorder = EngineRecorder(old, lambda _: None, engine_cls=BlockingEngine)
    try:
        assert recorder.start("one") is True
        assert recorder.start("two") is False
        recorder.update_options({"output_dir": tmp_path / "next", "max_height": 720, "max_recordings": 2})
        assert recorder.start("two") is True
        assert engines[0].options == old
        assert engines[1].options.output_dir == tmp_path / "next"
        assert engines[1].options.max_height == 720
    finally:
        recorder.stop_all()
        recorder.join_all(5)


def test_requested_poll_interval_is_used_and_quota_floor_preserved(tmp_path):
    now = [datetime(2026, 9, 8, tzinfo=timezone.utc)]
    youtube = FakeYouTube()
    events = []
    controller = make_controller(
        RecordingOptions(output_dir=tmp_path, poll_interval_seconds=300),
        lambda _: None, events.append, youtube=youtube, clock=lambda: now[0],
    )
    controller.handle_command(cmd.ConnectAccount())
    assert controller.poll_interval == 300
    watch = [e for e in events if isinstance(e, ev.WatchStatusChanged)][-1]
    assert watch.next_check_at == now[0] + timedelta(seconds=300)
    controller.handle_command(cmd.UpdateSettings({"poll_interval_seconds": 30}))
    assert controller.poll_interval >= 30
