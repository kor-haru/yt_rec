"""예약 라이브(`upcoming`) 일정 보관과 만기 대기.

푸시 알림이 예약 라이브를 알려 주면 YouTube 가 함께 준 ``scheduledStartTime``
을 여기에 적어 둔다. 예정 시각이 :data:`EARLY_CHECK_WINDOW_SECONDS` 보다 멀면
**창이 열릴 때까지 아무 요청도 하지 않는다.** 창 안에서는 방송자가 예고보다
일찍 켜는 경우를 위해 알림 받은 시점부터 1 분 간격으로 확인하고, 예정 시각이
지나면 늦게 켜는 경우를 위해 정해진 횟수만큼만 다시 확인한다.

이것은 #38 / #39 / #63 이 배제한 폴링이 아니다.

- 푸시라는 *이벤트에서 파생*된다. 주기적으로 전체를 훑지 않는다.
- 대상이 *그 video id 하나*다. 선택 채널 전체가 아니다.
- *유한*하다. 예정 시각 전 :data:`MAX_EARLY_CHECKS` 회, 그 뒤
  :data:`MAX_RECHECKS` 회로 끝난다.
- 시작 시각도 *YouTube 가 알려 준 값*이다. 창은 그 값에서 거꾸로 잰다.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from yt_rec.recording.options import default_settings_path

__all__ = [
    "EARLY_CHECK_WINDOW_SECONDS",
    "MAX_EARLY_CHECKS",
    "MAX_RECHECKS",
    "MAX_SCHEDULE_AHEAD_SECONDS",
    "MAX_WAIT_SECONDS",
    "RECHECK_INTERVAL_SECONDS",
    "ScheduledLive",
    "ScheduleStore",
    "MemoryScheduleStore",
    "FileScheduleStore",
    "ScheduleWaker",
    "bump",
    "default_schedule_path",
]

_LOG = logging.getLogger(__name__)

#: 예정 시각 이후 재확인 간격(초). 정밀도 때문이 아니다 — 엔진이
#: ``--live-from-start`` 를 쓰므로 늦게 붙어도 시작분부터 받는다. 1 분은 단순함
#: 때문에 고른 값이다.
RECHECK_INTERVAL_SECONDS = 60.0

#: 재확인 상한. 넘기면 방송자가 끝내 켜지 않은 것으로 보고 포기한다.
MAX_RECHECKS = 30

#: 예정 시각보다 이만큼 앞에서부터 확인을 시작한다(초). 방송자가 예고보다 일찍
#: 켜면 예정 시각까지 기다리는 만큼 통째로 놓치기 때문이다(#103).
#: YouTube 예고 알림은 보통 30 분 전에 온다(실측: 11:00 예정에 10:30:07 수신).
#: 60 분이면 그 알림을 받은 순간이 창 안에 들어오고도 여유가 남는다. 더 넓히면
#: 얻는 것 없이 조회만 늘어난다 — 알림이 오기도 전에는 확인할 이유가 없다.
EARLY_CHECK_WINDOW_SECONDS = 60 * 60.0

#: 이른 확인 상한. 창 하나를 1 분 간격으로 훑는 횟수다. :data:`MAX_RECHECKS` 와
#: **예산을 따로 센다** — 이른 확인을 다 써도 예정 시각 이후 몫은 그대로 남는다.
#: 예약 1 건당 최대 비용은 알림 1 + 이른 확인 60 + 재확인 30 = 91 units 다
#: (하루 한도 10,000).
MAX_EARLY_CHECKS = int(EARLY_CHECK_WINDOW_SECONDS // RECHECK_INTERVAL_SECONDS)

#: 한 번에 기다리는 최대 시간(초). 사흘 뒤 예약까지 타이머를 들고 있지 않는다.
#: 이 시간이 지나면 남은 시간을 다시 계산해서 잔다.
MAX_WAIT_SECONDS = 24 * 60 * 60.0

#: 이보다 먼 예약은 받지 않는다. 잘못된 값이 디스크에 쌓이는 것을 막는다.
MAX_SCHEDULE_AHEAD_SECONDS = 30 * 24 * 60 * 60.0

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")


@dataclass(frozen=True)
class ScheduledLive:
    """예정 시각을 기다리는 예약 라이브 한 건."""

    video_id: str
    #: YouTube 가 알려 준 예정 시작 시각(epoch 초). 본문 문구가 아니다.
    scheduled_at: float
    #: 이 예약을 만든 알림의 수신 시각.
    received_at: float
    #: 예정 시각 이후 이미 확인한 횟수.
    attempts: int = 0
    #: 원본 알림이 합성 입력이었는가. 로그를 정직하게 남기려고 함께 적는다.
    synthetic: bool = False
    #: 예정 시각 *전에* 이미 확인한 횟수. 옛 예약 파일에는 없으므로 0 이 기본이다.
    early_attempts: int = 0

    def __post_init__(self) -> None:
        if not _VIDEO_ID.fullmatch(self.video_id):
            raise ValueError("예약에는 YouTube 영상 ID 하나만 허용됩니다")
        for name in ("scheduled_at", "received_at"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name}: 올바른 시각이 아닙니다")
            object.__setattr__(self, name, value)
        for name in ("attempts", "early_attempts"):
            count = int(getattr(self, name))
            if count < 0:
                raise ValueError(f"{name} 는 음수일 수 없습니다")
            object.__setattr__(self, name, count)

    def early_due_at(self) -> float | None:
        """예정 시각 전 다음 확인 시각. 창 밖이거나 예산을 다 썼으면 ``None``.

        알림을 창 안에서 받았으면 그 시점부터, 창보다 일찍 받았으면 창이 열릴
        때부터 1 분 간격이다. 알림 자체가 이미 한 번 물었으므로 1 분을 띄운다.
        """
        if self.early_attempts >= MAX_EARLY_CHECKS:
            return None
        opens = max(
            self.received_at + RECHECK_INTERVAL_SECONDS,
            self.scheduled_at - EARLY_CHECK_WINDOW_SECONDS,
        )
        due = opens + self.early_attempts * RECHECK_INTERVAL_SECONDS
        return due if due < self.scheduled_at else None

    def due_at(self) -> float:
        """다음 확인 시각. 창이 열린 뒤에는 예정 시각 전에도 확인한다."""
        early = self.early_due_at()
        if early is not None:
            return early
        return self.scheduled_at + self.attempts * RECHECK_INTERVAL_SECONDS

    @property
    def exhausted(self) -> bool:
        return self.attempts >= MAX_RECHECKS

    def to_dict(self) -> dict[str, Any]:
        return {
            "video_id": self.video_id,
            "scheduled_at": self.scheduled_at,
            "received_at": self.received_at,
            "attempts": self.attempts,
            "synthetic": self.synthetic,
            "early_attempts": self.early_attempts,
        }

    @classmethod
    def from_dict(cls, data: Any) -> ScheduledLive:
        if not isinstance(data, dict):
            raise ValueError("예약 항목은 JSON 객체여야 합니다")
        return cls(
            video_id=str(data.get("video_id") or ""),
            scheduled_at=float(data.get("scheduled_at") or 0.0),
            received_at=float(data.get("received_at") or 0.0),
            attempts=int(data.get("attempts") or 0),
            synthetic=bool(data.get("synthetic")),
            early_attempts=int(data.get("early_attempts") or 0),
        )


class ScheduleStore(Protocol):
    def load(self) -> tuple[ScheduledLive, ...]: ...

    def save(self, schedules: Sequence[ScheduledLive]) -> None: ...


class MemoryScheduleStore:
    """테스트용. 재시작을 견디지 않는다."""

    def __init__(self, schedules: Sequence[ScheduledLive] = ()) -> None:
        self._items = tuple(schedules)

    def load(self) -> tuple[ScheduledLive, ...]:
        return self._items

    def save(self, schedules: Sequence[ScheduledLive]) -> None:
        self._items = tuple(schedules)


def default_schedule_path() -> Path:
    return default_settings_path().with_name("upcoming_lives.json")


class FileScheduleStore:
    """예약을 JSON 으로 남긴다.

    예고와 실제 시작 사이가 20 분 넘게 벌어지는 일이 흔하다(#82 실측 21 분
    44 초). 그 사이 앱이 재시작되면 메모리 예약은 사라지므로 디스크에 둔다.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_schedule_path()

    def load(self) -> tuple[ScheduledLive, ...]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ()
        values = raw.get("schedules") if isinstance(raw, dict) else raw
        if not isinstance(values, list):
            return ()
        items: list[ScheduledLive] = []
        for entry in values:
            try:
                items.append(ScheduledLive.from_dict(entry))
            except (TypeError, ValueError):
                # 한 줄이 깨졌다고 나머지 예약까지 버리지 않는다.
                _LOG.warning("예약 라이브 항목 하나를 읽지 못해 건너뜁니다")
        return tuple(items)

    def save(self, schedules: Sequence[ScheduledLive]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schedules": [item.to_dict() for item in schedules]}
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


class ScheduleWaker:
    """다음 예정 시각 하나만 겨냥해 깨어나는 대기자.

    주기 타이머가 아니다. 예약이 없으면 무기한 잠들어 아무 일도 하지 않고,
    예약이 생기면 :meth:`wake` 로 깨워 다시 계산한다. 한 번에 기다리는 시간은
    :data:`MAX_WAIT_SECONDS` 로 묶여 있다.

    ``on_due`` 는 대기 스레드에서 직접 돌므로 막히면 안 되고, 호출마다 다음
    만기를 앞으로 밀어야 한다(그러지 않으면 같은 만기를 계속 깨운다).
    """

    def __init__(
        self,
        *,
        due_at: Callable[[], float | None],
        on_due: Callable[[], object],
        clock: Callable[[], float] = time.time,
        wait: Callable[[float | None], Any] | None = None,
    ) -> None:
        self._due_at = due_at
        self._on_due = on_due
        self._clock = clock
        self._event = threading.Event()
        self._wait = wait if wait is not None else (lambda timeout: self._event.wait(timeout))
        self._stopping = False
        self._thread: threading.Thread | None = None

    def next_wait(self) -> float | None:
        """다음 깨어날 때까지 기다릴 초. 예약이 없으면 ``None`` (무기한)."""
        due = self._due_at()
        if due is None:
            return None
        return max(0.0, min(due - self._clock(), MAX_WAIT_SECONDS))

    def wake(self) -> None:
        """예약이 바뀌었으니 다시 계산하라고 알린다. 막히지 않는다."""
        self._event.set()

    def run_once(self) -> bool:
        """한 번 기다린 뒤 만기를 알린다. 종료 요청이면 ``False``."""
        self._wait(self.next_wait())
        self._event.clear()
        if self._stopping:
            return False
        try:
            self._on_due()
        except Exception as exc:  # 대기 스레드는 어떤 실패로도 죽지 않는다.
            _LOG.warning("예약 라이브 확인에 실패했습니다: %s", exc)
            self._wait(RECHECK_INTERVAL_SECONDS)
            self._event.clear()
        return not self._stopping

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stopping = False
        self._thread = threading.Thread(
            target=self._loop, name="yt-rec-schedule", daemon=True
        )
        self._thread.start()

    def request_stop(self) -> None:
        """종료를 요청하고 곧바로 돌아온다. GUI 스레드에서 부른다."""
        self._stopping = True
        self._event.set()

    def stop(self, *, timeout: float | None = 5.0) -> None:
        self.request_stop()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def _loop(self) -> None:
        while self.run_once():
            pass


def bump(entry: ScheduledLive, now: float | None = None) -> ScheduledLive:
    """확인 한 번을 소모한 사본. 이른 확인과 재확인은 예산이 따로다.

    ``now`` 를 주면 앱이 꺼져 있는 동안 지나간 이른 확인 몫은 함께 버린다.
    창 앞부분을 통째로 놓쳤다고 지난 1 분마다 요청을 하나씩 태우지 않는다.
    """
    early = entry.early_due_at()
    if early is None:
        return replace(entry, attempts=entry.attempts + 1)
    missed = 0 if now is None else int(max(0.0, now - early) // RECHECK_INTERVAL_SECONDS)
    spent = min(entry.early_attempts + 1 + missed, MAX_EARLY_CHECKS)
    return replace(entry, early_attempts=spent)
