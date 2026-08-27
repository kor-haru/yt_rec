"""애플리케이션 진입점.

사용법::

    yt-rec                       # 실제 백엔드. 미연결이면 `연결 안 됨`
    python -m yt_rec --stub empty        # 빈 상태 더미
    python -m yt_rec --stub populated    # 채널·녹화·완료 더미
    python -m yt_rec --stub scenario     # 시작→진행→완료→오류 시나리오 재생
    python -m yt_rec --stub flood        # 초당 100건 진행 이벤트 부하
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from .backend import create_backend_source
from .state.store import AppState, EventSource
from .state.stub import PRESETS, StubEventSource, recording_lifecycle
from .ui.main_window import MainWindow
from .ui.settings_store import APPLICATION, ORGANIZATION, WindowSettings

__all__ = ["main", "build_application", "AppContext", "parse_args"]

STUB_MODES = (*PRESETS.keys(), "scenario", "flood")


@dataclass
class AppContext:
    """기동해 놓은 객체 묶음. 참조를 살려 두기 위해 호출자가 들고 있는다."""

    app: QApplication
    state: AppState
    window: MainWindow
    source: EventSource | None = None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="yt-rec",
        description="선택한 YouTube 채널의 라이브를 시작 지점부터 자동 녹화한다.",
    )
    parser.add_argument(
        "--stub",
        choices=STUB_MODES,
        help="백엔드 대신 스텁 이벤트 소스를 붙인다(화면 개발용).",
    )
    parser.add_argument(
        "--emit-interval-ms",
        type=int,
        default=200,
        help="화면 갱신을 묶는 간격. 0이면 묶지 않는다. 기본 200ms.",
    )
    return parser.parse_args(argv)


def build_application(
    argv: list[str] | None = None,
    *,
    app: QApplication | None = None,
) -> AppContext:
    """QApplication과 창을 구성해 돌려준다. 이벤트 루프는 돌리지 않는다."""
    args = parse_args(argv)

    if app is None:
        app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setOrganizationName(ORGANIZATION)
    app.setApplicationName(APPLICATION)
    app.setApplicationDisplayName("yt-rec")

    # 백엔드가 붙기 전까지 연결 상태는 `연결 안 됨`이 기본이다.
    state = AppState(emit_interval_ms=args.emit_interval_ms)
    window = MainWindow(state, settings=WindowSettings())

    source: EventSource | None
    if args.stub:
        stub = StubEventSource()
        state.attach(stub)
        _start_stub(stub, args.stub)
        source = stub
    else:
        source = create_backend_source()
        state.attach(source)
        state.command_requested.connect(source.handle_command)
        source.start()

    return AppContext(app=app, state=state, window=window, source=source)


def _start_stub(source: StubEventSource, mode: str) -> None:
    if mode in PRESETS:
        # 창이 보인 뒤에 주입해야 첫 그리기 경로까지 확인할 수 있다.
        QTimer.singleShot(0, lambda: source.load_preset(mode))
    elif mode == "scenario":
        QTimer.singleShot(0, lambda: source.play(recording_lifecycle()))
    elif mode == "flood":
        # 반복 타이머 하나로 초당 100건을 계속 밀어 넣는다.
        QTimer.singleShot(0, lambda: source.start_flood(hz=100))
    else:  # pragma: no cover - argparse가 먼저 걸러낸다
        raise ValueError(f"알 수 없는 스텁 모드: {mode}")


def main(argv: list[str] | None = None) -> int:
    context = build_application(argv)
    context.window.show()
    try:
        return context.app.exec()
    finally:
        if context.source is not None:
            context.source.stop()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
