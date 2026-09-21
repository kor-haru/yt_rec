"""기존 녹화 설정을 수정하고 백엔드의 저장 결과를 기다리는 모달."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..recording.options import (
    NOTIFICATION_RECEIVERS,
    QUALITY_PRESETS,
    RecordingOptions,
    output_free_bytes,
    validate_output_dir,
)
from ..state.store import AppState
from .formatting import format_bytes


class SettingsDialog(QDialog):
    def __init__(self, state: AppState, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._state = state
        self._pending = False
        self.setWindowTitle("설정 — yt-rec")
        self.setObjectName("SettingsDialog")
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        directory_row = QHBoxLayout()
        self.output_edit = QLineEdit(self)
        self.output_edit.setObjectName("outputDirectory")
        self.output_edit.setAccessibleName("녹화 저장 위치")
        directory_row.addWidget(self.output_edit)
        self.browse_button = QPushButton("폴더 선택…", self)
        self.browse_button.clicked.connect(self._browse)
        directory_row.addWidget(self.browse_button)
        form.addRow("저장 위치", directory_row)
        self.space_label = QLabel(self)
        form.addRow("남은 용량", self.space_label)
        self.quality_combo = QComboBox(self)
        self.quality_combo.setObjectName("maxHeight")
        for label, height in QUALITY_PRESETS.items():
            self.quality_combo.addItem(label, height)
        form.addRow("최대 화질", self.quality_combo)
        quality_note = QLabel("선택값은 화질 상한입니다. 방송 화질이 더 낮으면 원래 화질로 녹화합니다.", self)
        quality_note.setWordWrap(True)
        form.addRow(quality_note)
        self.max_recordings_spin = QSpinBox(self)
        self.max_recordings_spin.setRange(1, 16)
        self.max_recordings_spin.setSuffix("개")
        form.addRow("동시 녹화 수 (1~16)", self.max_recordings_spin)
        poll_note = QLabel("방송 알림을 받으면 해당 영상만 확인합니다. 주기적으로 방송을 조회하지 않습니다.", self)
        poll_note.setWordWrap(True)
        form.addRow(poll_note)
        self.receiver_combo = QComboBox(self)
        self.receiver_combo.setObjectName("notificationReceiver")
        for value, label in NOTIFICATION_RECEIVERS.items():
            self.receiver_combo.addItem(label, value)
        form.addRow("알림 수신기", self.receiver_combo)
        receiver_note = QLabel(
            "Chrome 수신기는 yt-rec 전용 프로필로 Chrome을 띄웁니다. 개인 프로필은 쓰지 않습니다. "
            "수신기를 바꾸면 앱을 다시 실행해야 적용됩니다.", self)
        receiver_note.setWordWrap(True)
        form.addRow(receiver_note)
        self.autostart_check = QCheckBox("컴퓨터 로그인 시 자동 시작", self)
        self.start_hidden_check = QCheckBox("시작할 때 창을 숨기고 트레이로 실행", self)
        self.minimize_to_tray_check = QCheckBox("최소화하면 트레이로 보내기", self)
        self.notifications_check = QCheckBox("녹화 및 오류 알림 표시", self)
        for checkbox in (self.autostart_check, self.start_hidden_check,
                         self.minimize_to_tray_check, self.notifications_check):
            form.addRow(checkbox)
        self.retention_spin = QSpinBox(self)
        self.retention_spin.setRange(1, 365)
        self.retention_spin.setSuffix("일")
        form.addRow("로그 보관 기간 (1~365일)", self.retention_spin)
        self.recording_note = QLabel("저장 위치와 화질 변경은 다음 녹화부터 적용됩니다. 진행 중인 녹화는 기존 설정으로 계속됩니다.", self)
        self.recording_note.setWordWrap(True)
        layout.addWidget(self.recording_note)
        self.error_label = QLabel(self)
        self.error_label.setObjectName("settingsError")
        self.error_label.setWordWrap(True)
        self.error_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self
        )
        self.save_button = self.buttons.button(QDialogButtonBox.StandardButton.Save)
        self.save_button.setText("저장")
        self.cancel_button = self.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        self.cancel_button.setText("취소")
        self.buttons.accepted.connect(self._save)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.output_edit.textChanged.connect(self._validate)
        state.settings_changed.connect(self._settings_changed)
        state.settings_save_failed.connect(self._save_failed)
        self._restore(state.settings)

    @property
    def state(self) -> AppState:
        return self._state

    def _restore(self, options: RecordingOptions | None) -> None:
        self.setEnabled(True)
        if options is None:
            self.error_label.setText("설정을 불러오는 중입니다. 실제 백엔드가 실행된 앱에서 설정할 수 있습니다.")
            self.save_button.setEnabled(False)
            return
        self.output_edit.setText(str(options.output_dir))
        index = self.quality_combo.findData(options.max_height)
        if index < 0:
            self.quality_combo.addItem(f"{options.max_height}p", options.max_height)
            index = self.quality_combo.count() - 1
        self.quality_combo.setCurrentIndex(index)
        self.max_recordings_spin.setValue(options.max_recordings)
        receiver_index = self.receiver_combo.findData(options.notification_receiver)
        if receiver_index >= 0:
            self.receiver_combo.setCurrentIndex(receiver_index)
        self.autostart_check.setChecked(options.autostart)
        self.start_hidden_check.setChecked(options.start_hidden)
        self.minimize_to_tray_check.setChecked(options.minimize_to_tray)
        self.notifications_check.setChecked(options.notifications_enabled)
        self.retention_spin.setValue(options.log_retention_days)
        self._validate()

    def _browse(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "녹화 저장 폴더 선택", self.output_edit.text())
        if selected:
            self.output_edit.setText(selected)

    def _validate(self) -> bool:
        try:
            directory = validate_output_dir(self.output_edit.text())
            self.space_label.setText(format_bytes(output_free_bytes(directory)))
        except (OSError, ValueError) as exc:
            self.space_label.setText("확인할 수 없음")
            self.error_label.setText(str(exc))
            self.save_button.setEnabled(False)
            return False
        self.error_label.clear()
        ready = self._state.settings is not None and self._state.backend_attached
        self.save_button.setEnabled(ready and not self._pending)
        return ready

    def _save(self) -> None:
        if self._pending or not self._validate():
            return
        self._pending = True
        self.setEnabled(False)
        self.error_label.setText("설정을 저장하는 중입니다…")
        sent = self._state.update_settings(
            output_dir=self.output_edit.text(),
            max_height=self.quality_combo.currentData(),
            max_recordings=self.max_recordings_spin.value(),
            notification_receiver=self.receiver_combo.currentData(),
            autostart=self.autostart_check.isChecked(),
            start_hidden=self.start_hidden_check.isChecked(),
            minimize_to_tray=self.minimize_to_tray_check.isChecked(),
            notifications_enabled=self.notifications_check.isChecked(),
            log_retention_days=self.retention_spin.value(),
        )
        if not sent:
            self._save_failed("백엔드에 설정을 전달하지 못했습니다. 앱을 다시 실행해 주세요.")

    def _settings_changed(self, options: RecordingOptions) -> None:
        if self._pending:
            self._pending = False
            self.accept()
        else:
            self._restore(options)

    def _save_failed(self, message: str) -> None:
        if not self._pending:
            return
        self._pending = False
        self.setEnabled(True)
        self.error_label.setText(f"저장하지 못했습니다: {message}")

    def reject(self) -> None:
        if not self._pending:
            super().reject()
