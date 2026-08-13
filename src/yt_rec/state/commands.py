"""화면 → 백엔드 명령.

:mod:`~yt_rec.state.events` 가 백엔드 → 화면 한 방향이라면 이 모듈은 그 반대
방향의 **유일한** 경로다. 화면이 백엔드 객체를 직접 붙잡거나 자기만의 시그널을
새로 만들어 쓰면, 화면마다 백엔드에 닿는 방법이 달라져 계약이 흩어진다.

사용법 — 화면 쪽::

    # 저장소가 유일한 창구다. 백엔드 객체를 화면이 직접 들고 있지 않는다.
    if not state.stop_recording("rec-1"):
        ...  # 보내지 못했다. command_rejected 로 사유가 함께 온다.

사용법 — 백엔드 쪽::

    state.command_requested.connect(self.on_command)   # 작업 스레드면 Qt가 큐 연결

    def on_command(self, command):
        match command:
            case StopRecording(recording_id=rid):
                ...
            case SetWatchedChannels(channel_ids=ids):
                ...
            case UpdateSettings(values=values):
                ...
        # 결과는 반드시 이벤트로 되돌려 보낸다. 명령을 받았다고 화면이 상태를
        # 바꾸지는 않는다 — 상태를 정하는 곳은 백엔드 하나다.

명령은 `요청`이다. 성공을 뜻하지 않는다. 화면은 명령을 보낸 뒤 스스로 상태를
바꾸지 말고, 백엔드가 :mod:`~yt_rec.state.events` 로 되돌려 주는 통지를 기다려
그린다. 그래야 실패했을 때 화면과 실제가 갈라지지 않는다.

명령 종류는 지금 필요한 세 가지뿐이다. 화면 이슈가 새 조작을 요구하면 그때
데이터 클래스를 추가하고 :data:`GuiCommand` 에 붙인다. 추측으로 미리 늘리지
않는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = [
    "StopRecording",
    "SetWatchedChannels",
    "UpdateSettings",
    "GuiCommand",
]


@dataclass(frozen=True, slots=True)
class StopRecording:
    """진행 중인 녹화 한 건을 멈춰 달라 (#9 녹화 카드의 `중지`).

    백엔드는 멈춘 뒤 :class:`~yt_rec.state.events.RecordingFinished` 를 보낸다.
    화면이 카드를 직접 지우면 실제로 멈추지 못했을 때 사라진 것처럼 보인다.
    """

    recording_id: str
    reason: str = ""
    """사용자에게 보여 줄 중지 사유. 로그와 완료 이력의 ``note`` 로 쓰인다."""


@dataclass(frozen=True, slots=True)
class SetWatchedChannels:
    """자동 녹화 대상 채널 목록을 이 목록으로 교체해 달라 (#8 채널 관리).

    부분 변경(추가/삭제)이 아니라 **전체 교체**다. 화면이 보고 있는 목록이
    그대로 진실이 되므로, 두 화면이 동시에 고쳐도 마지막 것만 남고 어긋난
    중간 상태가 생기지 않는다. 이벤트 쪽
    :class:`~yt_rec.state.events.ChannelsChanged` 도 같은 방식이다.
    """

    channel_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UpdateSettings:
    """기능 설정을 바꿔 달라 (#11 설정 다이얼로그).

    담긴 키만 바꾼다. 없는 키는 건드리지 않는다. 설정 항목이 아직 확정되지
    않았으므로 키 이름은 #11 이 정하고 여기서는 담는 그릇만 정한다.

    창 크기·섹션 접힘처럼 **화면에만 있는 표시 상태는 여기 넣지 않는다**.
    그것은 :class:`~yt_rec.ui.settings_store.WindowSettings` 가 로컬에
    저장하며 백엔드와 무관하다.
    """

    values: Mapping[str, object] = field(default_factory=dict)
    """읽기 전용으로 다룬다. 보낸 뒤 고치면 받는 쪽이 무엇을 볼지 알 수 없다."""


GuiCommand = StopRecording | SetWatchedChannels | UpdateSettings
"""화면이 백엔드에 보낼 수 있는 명령 전체."""
