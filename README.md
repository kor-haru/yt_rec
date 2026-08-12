# yt-rec

선택한 YouTube 채널의 라이브를 자동으로 감지해 방송 시작 지점부터 녹화하는 standalone 데스크톱 앱.

## 상태

초기 개발 단계. 기능 요건과 착수 순서는 [이슈](https://github.com/kor-haru/yt_rec/issues)에서 관리한다.

## 기술 선택

| 항목 | 선택 | 이유 |
|---|---|---|
| 언어 | Python (3.11 이상) | 유지보수자가 전체 동작을 읽고 파악할 수 있어야 한다 |
| GUI | PySide6 (Qt Widgets) | LGPL. QML은 사용하지 않는다 — 순수 Python으로만 화면을 구성한다 |
| 미디어 | yt-dlp, ffmpeg | 외부 실행 파일로 호출한다 |
| 환경·의존성·빌드 | uv | 잠금 파일로 세 OS에서 같은 의존성 트리를 재현한다 |

### 제약

- **QtWebEngine을 사용하지 않는다.** Chromium을 포함하게 되어 배포 요건과 충돌한다. OAuth는 시스템 기본 브라우저와 로컬 루프백 서버로 처리하므로 임베디드 웹뷰가 필요 없다.
- **QML을 사용하지 않는다.** Qt Widgets만 사용한다.
- **GUI는 파일시스템이나 외부 프로세스를 직접 폴링하지 않는다.** 상태 변경은 백엔드가 시그널로 통지한다. 특히 녹화 중인 파일 크기를 `os.stat`으로 읽으면 안 된다 — Windows는 쓰기 핸들이 열린 파일의 크기를 디렉터리 엔트리에 즉시 반영하지 않아 실제보다 훨씬 작은 값이 표시된다.

## 개발

환경 구성과 의존성 관리는 [uv](https://docs.astral.sh/uv/)로 통일한다. pip이나
`python -m venv`를 직접 쓰지 않는다.

uv가 없다면 먼저 설치한다.

```bash
# Windows (PowerShell)
irm https://astral.sh/uv/install.ps1 | iex
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

저장소를 받은 뒤 다음 한 줄이면 끝난다. 가상환경 생성, Python 확보, 의존성
설치, 프로젝트 자체의 editable 설치까지 `uv sync`가 다 한다.

```bash
uv sync
uv run pytest
```

의존성 버전은 `uv.lock`에 고정돼 있어 어느 머신에서든 같은 트리가 재현된다.
잠금 파일은 저장소에 포함하며 직접 편집하지 않는다. 의존성을 바꿀 때는
`pyproject.toml`을 고치고 `uv sync`(또는 `uv lock`)를 실행해 잠금 파일을 함께
커밋한다.

| 명령 | 용도 |
|---|---|
| `uv sync` | 개발 환경 구성 (런타임 + 개발 의존성) |
| `uv sync --no-dev` | 런타임 의존성만. 배포물 검증용 |
| `uv run pytest` | 테스트 실행 |
| `uv add <패키지>` | 런타임 의존성 추가 |
| `uv add --dev <패키지>` | 개발 의존성 추가 |

### 실행

```bash
uv run yt-rec                        # 백엔드 없이 기동 — 상단 배지에 `연결 안 됨`
uv run yt-rec --stub empty           # 빈 상태 더미
uv run yt-rec --stub populated       # 채널·녹화·완료 더미
uv run yt-rec --stub scenario        # 시작 → 진행 → 완료 → 오류 재생
uv run yt-rec --stub flood           # 초당 100건 진행 이벤트 부하
```

`uv run python -m yt_rec` 로도 같은 진입점이 뜬다.

### 화면 코드가 지켜야 할 계약

화면은 `yt_rec.state` 만 참조한다. 백엔드 구현을 직접 부르지 않는다.

| 계층 | 위치 | 역할 |
|---|---|---|
| 상태 모델 | `yt_rec.state.models` | GUI가 그리는 불변 데이터 |
| 이벤트 | `yt_rec.state.events` | 백엔드 → 상태 계층 통지 |
| 저장소 | `yt_rec.state.store.AppState` | 이벤트 적용, Qt 시그널 방출, 갱신 빈도 제한 |
| 스텁 | `yt_rec.state.stub.StubEventSource` | 백엔드 없이 화면을 개발·테스트하는 하니스 |

- 백엔드는 `EventSource.event_ready` 로 이벤트를 밀거나 `AppState.post_event()` 를
  호출한다. 작업 스레드에서 불러도 안전하며, Qt 큐 연결이 GUI 스레드로 넘긴다.
- 진행 중 녹화의 크기·경과 시간은 `Recording.reported_bytes` / `reported_elapsed`
  를 그대로 쓴다. `os.stat`·`Path.stat`·`getsize` 로 다시 재지 않는다.
- 갱신은 기본 200ms 마다 한 번으로 묶인다. 초당 수백 건이 들어와도 화면 갱신은
  초당 5회를 넘지 않는다.
- 보조 문구 색은 스타일시트에 고정하지 않고 `ui.widgets.set_muted()` 를 쓴다.
  `palette(dark)` 같은 값은 다크 테마에서 배경과 겹쳐 글자가 사라진다.

## 라이선스

미정.
