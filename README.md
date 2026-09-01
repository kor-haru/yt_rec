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

## 사용법

### 시작하기 전에

yt-rec는 아직 **설치 파일이 없는 초기 개발 버전**이다. 지금은 프로젝트 파일을
받은 뒤 PowerShell에서 실행해야 한다. 이 안내는 Windows 10과 Windows 11을
기준으로 한다.

준비물은 다음과 같다.

- 안정적인 인터넷 연결
- 감시할 채널을 구독한 Google/YouTube 계정
- 녹화 파일을 저장할 충분한 디스크 공간
- Windows에 기본으로 포함된 PowerShell

Google에서 내려받은 OAuth JSON 파일에는 비밀 값이 들어 있다. 이 파일과 환경
변수 값을 Git, GitHub, 메신저, 이메일, 스크린샷으로 공유하지 않는다. yt-rec가
로그인 뒤 받은 토큰은 Windows 자격 증명 관리자에 저장한다.

### 1. 프로젝트 파일 받기

개발 도구에 익숙하지 않다면 ZIP 파일을 받는 방법이 가장 쉽다.

1. [main 브랜치 ZIP 파일](https://github.com/kor-haru/yt_rec/archive/refs/heads/main.zip)을 받는다.
2. 받은 파일을 마우스 오른쪽 버튼으로 누르고 **모두 압축 풀기**를 선택한다.
3. 압축을 푼 `yt_rec-main` 폴더를 연다.
4. Windows 11은 폴더의 빈 곳을 마우스 오른쪽 버튼으로 누르고 **터미널에서 열기**를
   선택한다. Windows 10은 파일 탐색기 주소 표시줄에 `powershell`을 입력하고
   Enter를 누른다.

PowerShell에 다음 명령을 입력했을 때 `True`가 나오면 올바른 폴더다.

```powershell
Test-Path .\pyproject.toml
```

Git을 쓰는 사람은 ZIP 대신 다음과 같이 받을 수 있다. `git` 명령이 없다면
[Git for Windows](https://git-scm.com/download/win)를 먼저 설치한다.

```powershell
Set-Location "$HOME\Downloads"
git clone https://github.com/kor-haru/yt_rec.git
Set-Location .\yt_rec
```

이후의 모든 명령은 `pyproject.toml`이 있는 프로젝트 폴더에서 실행한다.

### 2. uv 설치하기

[uv](https://docs.astral.sh/uv/getting-started/installation/)는 yt-rec에 필요한
Python과 프로그램 구성 요소를 준비하는 도구다. PowerShell에서 다음 명령을 한 번
실행한다.

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

설치가 끝나면 PowerShell을 닫았다가 프로젝트 폴더에서 다시 연다. 다음 명령에
버전 번호가 나오면 설치된 것이다.

```powershell
uv --version
```

설치 과정이 막히면 [uv 설치 프로그램 안내](https://docs.astral.sh/uv/configuration/installer/)를 확인한다.

### 3. yt-rec 구성 요소 설치하기

프로젝트 폴더에서 다음 명령을 실행한다.

```powershell
uv sync
```

처음 실행할 때는 Python과 여러 구성 요소를 내려받으므로 시간이 걸릴 수 있다.
완료될 때까지 PowerShell 창을 닫지 않는다.

### 4. 녹화 도구 설치하기

실제 녹화에는 `yt-dlp`, `ffmpeg`, `ffprobe`라는 외부 프로그램이 필요하다.
Python 패키지가 아니라 Windows 실행 파일이므로 따로 설치해야 한다.

PowerShell에서 다음 두 명령을 차례로 실행한다. 설치 확인 창이 나타나면 내용을
확인하고 진행한다.

```powershell
winget install --id yt-dlp.yt-dlp -e --source winget
winget install --id Gyan.FFmpeg -e --source winget
```

PowerShell을 닫았다가 프로젝트 폴더에서 다시 연 뒤 세 명령을 확인한다.

```powershell
yt-dlp --version
ffmpeg -version
ffprobe -version
```

세 명령 모두 버전 정보를 보여야 한다. WinGet을 쓸 수 없다면
[yt-dlp 공식 배포 파일](https://github.com/yt-dlp/yt-dlp#release-files)과
[FFmpeg 공식 Windows 다운로드 안내](https://ffmpeg.org/download.html#build-windows)를 따른다.
설치 위치와 `PATH`가 어렵다면
[yt-dlp Windows FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#on-windows-how-should-i-set-up-ffmpeg-and-yt-dlp-where-should-i-put-the-exe-files)를
참고한다. `ffprobe.exe`도 빠뜨리면 안 된다.

### 5. 가짜 데이터로 화면 먼저 확인하기

Google 로그인 전에 GUI가 정상적으로 열리는지 확인한다.

```powershell
uv run yt-rec --stub populated
```

`--stub`은 **가짜 데이터만 보여 주는 안전한 화면 시험 모드**다. Google에
로그인하지 않고, 채널을 조회하지 않으며, 실제 녹화 파일도 만들지 않는다.
샘플 채널·녹화·완료 항목이 보이면 기본 실행 환경이 준비된 것이다.

창을 닫은 뒤 다음 명령으로 녹화 시작부터 완료와 오류까지 변하는 화면도 볼 수 있다.

```powershell
uv run yt-rec --stub scenario
```

### 6. Google Cloud에서 로그인 파일 준비하기

yt-rec는 구독 채널과 현재 라이브를 읽기 위해 YouTube Data API를 사용한다. 다음
설정은 처음 한 번만 준비하면 된다.
메뉴 번역이나 위치는 바뀔 수 있다. 현재 영문 이름은 `Overview`, `Branding`,
`Audience`, `Clients`, `Data Access`이며, 보이지 않으면 아래 공식 링크에서 연다.

1. [Google Cloud Console](https://console.cloud.google.com/)에 로그인하고 새
   프로젝트를 만들거나 사용할 프로젝트를 선택한다.
2. [YouTube Data API v3 활성화 안내](https://developers.google.com/youtube/v3/guides/auth/installed-apps#enable-apis-for-your-project)에
   따라 **YouTube Data API v3**를 찾아 **Enable(사용)**을 누른다.
3. **Google Auth Platform**의 `Overview`에서 앱 이름과 사용자 지원 이메일을
   입력한다. 개인 Gmail 계정은 사용자 유형으로 `External`을 고른다. 자세한 항목은
   [Google Auth Platform 시작 안내](https://support.google.com/cloud/answer/15544987)를 참고한다.
4. `Audience`가 `Testing`이면 로그인할 Google 계정을 **Test users**에 추가한다.
   테스트 상태의 승인은 7일 뒤 만료될 수 있다. 관련 제한은
   [Audience 안내](https://support.google.com/cloud/answer/15549945)를 참고한다.
5. `Data Access`에는 다음 읽기 전용 범위만 추가한다. 다른 YouTube 권한은 이 앱에
   필요하지 않다. [Data Access 안내](https://support.google.com/cloud/answer/15549135)
   에서 범위 추가 방법을 볼 수 있다.

   ```text
   https://www.googleapis.com/auth/youtube.readonly
   ```

6. `Clients`에서 **Create client → Desktop app**을 선택한다. 이름을 붙여 만든 뒤
   즉시 JSON 파일을 내려받는다. 자세한 순서는
   [데스크톱 OAuth 클라이언트 만들기](https://developers.google.com/workspace/guides/create-credentials#desktop-app)를
   따른다.

클라이언트 비밀 값과 전체 JSON은 생성할 때만 내려받을 수 있다. 잃어버렸다면 새
클라이언트를 만든다. 관리 방법은
[Google의 클라이언트 비밀 값 안내](https://support.google.com/cloud/answer/15549257#client-secret-handling-and-visibility)와
[OAuth 보안 정책](https://developers.google.com/identity/protocols/oauth2/policies)을
참고한다.

### 7. OAuth JSON 파일 놓기

PowerShell에서 다음 명령을 실행하면 yt-rec의 사용자 설정 폴더가 열리고, 폴더가
없으면 먼저 만들어진다.

```powershell
New-Item -ItemType Directory -Force "$env:APPDATA\yt-rec" | Out-Null
explorer "$env:APPDATA\yt-rec"
```

파일 탐색기에서 파일 이름을 바꾸기 전에 확장명 표시를 켠다. Windows 11은
**보기(View) → 표시(Show) → 파일 이름 확장명(File name extensions)**, Windows
10은 **보기(View) 탭 → 파일 이름 확장명(File name extensions)**을 선택한다.
그래야 이름이 `client_secrets.json.json`으로 잘못 바뀌는 것을 볼 수 있다.

Google에서 받은 JSON 파일을 열린 폴더로 옮기고 이름을 정확히
`client_secrets.json`으로 바꾼다. 최종 위치는 다음과 같아야 한다.

```text
%APPDATA%\yt-rec\client_secrets.json
```

PowerShell에서 위치와 이름을 확인한다.

```powershell
Test-Path "$env:APPDATA\yt-rec\client_secrets.json"
```

결과가 `True`여야 한다.

파일을 다른 안전한 폴더에 보관하려면, 대신 그 파일 경로를 현재 PowerShell에
지정할 수 있다. 아래 경로는 실제 JSON 파일 경로로 바꾼다.

```powershell
$env:YT_REC_GOOGLE_CLIENT_SECRETS = "D:\안전한 폴더\다운로드한 파일.json"
```

이 환경 변수는 현재 PowerShell을 닫으면 사라지므로 다음 실행 때 다시 지정해야
한다. 비밀 값이 유출됐다면 Google Cloud에서 기존 값을 폐기하고 새 값으로 교체한다.

### 8. 실제 모드로 실행하기

프로젝트 폴더에서 다음 명령을 실행한다.

```powershell
uv run yt-rec
```

`--stub`이 없으므로 이번에는 실제 Google 로그인과 자동 녹화 기능이 동작한다.
PowerShell 창은 앱이 실행되는 동안 함께 열어 둔다.

### 9. 계정과 자동 녹화 설정하기

1. 창 오른쪽 위의 **계정**을 누른 뒤 **연결**을 누른다.
2. 시스템 기본 브라우저가 열리면 사용할 Google 계정을 고른다. 프로젝트가
   `Testing` 상태이면 테스트 또는 미확인 앱 경고가 나타날 수 있다. 계속하기 전에
   방금 만든 앱 이름이 맞고 요청 권한이 YouTube 읽기 전용
   (`youtube.readonly`)뿐인지 확인한다. 앱 이름이 다르거나 다른 권한도 요구하면
   진행하지 말고 창을 닫는다. 확인한 뒤 승인하고 앱으로 돌아온다.
3. 메인 화면에서 **채널 관리**를 누른다.
4. 자동 녹화할 구독 채널을 체크한다. 체크할 때마다 바로 저장되므로 별도 저장
   버튼은 없다.
5. 창 위쪽 상태가 **감시 중 N채널**로 바뀌었는지 확인한다. 별도의 감시 시작
   버튼은 없다.
6. 선택한 채널에서 라이브가 발견되면 yt-rec가 자동으로 방송 시작 지점부터 받으며
   **녹화 중**에 진행 상황을 표시한다.
7. 오류 수가 늘거나 동작을 자세히 보고 싶으면 오른쪽 위의 **로그**를 연다.
   수준 필터, 메시지 검색, 선택한 행 복사를 사용할 수 있다.
8. 앱을 끝낼 때는 메인 창을 닫는다. 진행 중 녹화가 있으면 받은 부분을 마무리하는
   동안 시간이 걸릴 수 있으므로 PowerShell을 강제로 닫지 않는다.

### 10. 녹화 파일과 복구 결과 확인하기

기본 저장 위치는 프로젝트 폴더 아래의 `recordings` 폴더다.
기본 화질 상한은 1080p다.

```powershell
explorer .\recordings
```

결과 표시는 다음 의미다.

- **정상**: 재생 검증을 통과한 최종 녹화 파일이다.
- **부분 복구**: 재생 가능한 파일은 만들었지만 일부 방송 구간이 빠졌을 수 있다.
- **실패**: 다운로드, 병합 또는 검증을 끝내지 못했다. 복구 가능한 중간 파일은
  `recordings\.yt-rec` 아래에 남겨 두며, 앱을 다음에 실행할 때 자동 복구를
  시도한다. 이 폴더를 임의로 지우지 않는다.

긴 방송은 디스크를 빠르게 채운다. 녹화 전후로 `recordings` 폴더가 있는 드라이브의
남은 공간을 확인한다.

### 11. 자주 발생하는 문제

#### `uv`, `yt-dlp`, `ffmpeg`, `ffprobe` 명령을 찾을 수 없음

PowerShell을 모두 닫았다가 다시 연다. 그래도 안 되면 2단계와 4단계의 설치
명령을 다시 실행하고 각 `--version` 명령부터 확인한다.

#### GUI가 열리지 않음

현재 폴더에서 `Test-Path .\pyproject.toml`이 `True`인지 확인한 뒤 다음 명령을
차례로 실행한다.

```powershell
uv sync
uv run yt-rec --help
uv run yt-rec --stub populated
```

`애플리케이션 제어 정책에서 이 파일을 차단했습니다`라는 문구가 나오면 Windows나
회사·학교의 보안 정책이 uv의 Python 실행을 막은 것이다. 보안 기능을 임의로 끄지
말고 PC 관리자에게 허용 방법을 문의한다.

#### Google 연결 뒤 다시 `연결 안 됨`으로 돌아옴

- `%APPDATA%\yt-rec\client_secrets.json`의 이름과 위치를 확인한다.
- YouTube Data API v3 활성화와 `Audience`의 Test users를 확인한다.
- 테스트 승인이 만료됐다면 **계정 → 연결**로 다시 로그인한다.
- 자세한 원인은 **로그**에서 확인한다. 비밀 값은 공유하지 않는다.

#### 브라우저 로그인이 끝나지 않음

기본 브라우저와 Windows 방화벽이 로컬 주소 `127.0.0.1` 연결을 막지 않는지
확인한다. 로그인은 3분 안에 끝내야 한다. 시간이 지났다면 앱에서 **연결**을 다시
누른다.

#### 채널이 보이지 않거나 라이브 녹화가 시작되지 않음

**계정**에서 연결 상태를 확인하고 **채널 관리 → 다시 불러오기**를 누른다. 자동
녹화할 채널이 체크되어 있어야 위쪽에 **감시 중 N채널**이 표시된다. API 하루
할당량을 다 썼다면 로그에 quota 오류가 나타나며, 할당량이 다시 생길 때까지
기다려야 한다.
라이브인데도 시작하지 않으면 **로그**에서 네트워크, `yt-dlp`, `ffmpeg` 오류를
확인한다.

### 아직 사용할 수 없는 기능

버튼은 보이지만 **설정**과 **보관함** 화면은 아직 자리표시자다. 고장 난 것이
아니며 각각 [이슈 #11](https://github.com/kor-haru/yt_rec/issues/11)과
[이슈 #10](https://github.com/kor-haru/yt_rec/issues/10)에서 구현할 예정이다.

현재 로그 화면은 앱 안의 조회·필터·검색·복사를 지원한다. 전체 로그 파일,
파일 회전과 보관 기간, 민감값 자동 가림, 트레이 알림은
[이슈 #12](https://github.com/kor-haru/yt_rec/issues/12)의 후속 작업이다. 이 기능이
완성되기 전에는 로그를 공유하기 전에 비밀 값이 없는지 직접 확인한다.

독립 실행형 설치 파일은 [이슈 #5](https://github.com/kor-haru/yt_rec/issues/5)의
후속 작업이다. 그전까지는 이 문서처럼 소스에서 실행해야 한다.

## 녹화 엔진

`yt_rec.recording` 이 video id 하나를 방송 시작 지점부터 녹화해 재생 가능한 단일
파일로 마무리한다. GUI 없이도 돌릴 수 있다.

```python
from pathlib import Path
from yt_rec.recording import RecordingEngine, RecordingOptions

engine = RecordingEngine(RecordingOptions(output_dir=Path("D:/녹화"), max_height=1080))
result = engine.record("VIDEO_ID")      # 방송이 끝날 때까지 블로킹
print(result.status, result.output_path)
```

손으로 돌려 볼 때는 딸린 명령줄을 쓴다.

```bash
uv run python -m yt_rec.recording record <VIDEO_ID> -o "D:/녹화" --max-height 1080
uv run python -m yt_rec.recording verify "D:/녹화/2026-08-11_제목.mp4"
uv run python -m yt_rec.recording recover -o "D:/녹화"
```

동작에서 중요한 결정 세 가지는 실제 라이브 녹화에서 겪은 실패에서 나왔다.

- **조각 재시도 상한은 유한하다.** 방송 종료 시점에 마지막 조각 한두 개가 서버에서
  사라진다. 상한이 없으면 없는 조각을 영원히 다시 요청하며 정지한다(실측 29만 회
  재시도 / 7시간). 유한 상한을 두면 죽은 조각을 건너뛰고 병합까지 끝낸다.
- **메타데이터는 녹화를 시작할 때 확보해 보관한다.** 방송 종료 직후 영상이 멤버
  전용으로 바뀌면 제목을 조회할 수 없어 파일명을 만들지 못한다.
- **중간 파일은 병합 검증에 성공한 뒤에만 지운다.** 정지한 녹화도 영상·음성 중간
  파일이 온전하면 `ffmpeg -c copy` 로 살릴 수 있다.

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

`pytest -m "not integration"` 은 yt-dlp/ffmpeg 없이도 돌아간다. 통합 테스트는
로컬 스텁 서버와 그때그때 만든 합성 클립만 쓰며, 외부 네트워크가 필요 없다.
실제 라이브가 있어야 확인할 수 있는 항목은 [수동 검증 절차](docs/recording-manual-checks.md)에
적어 두었다.

### GUI 개발 참고

GUI 실행과 Google OAuth 준비는 위의 [사용법](#사용법)을 기준으로 한다.
`uv run python -m yt_rec`도 `uv run yt-rec`와 같은 진입점이다. 빈 화면은
`uv run yt-rec --stub empty`, 초당 100건 진행 이벤트 부하는
`uv run yt-rec --stub flood`로 확인한다.

### 화면 코드가 지켜야 할 계약

화면은 `yt_rec.state` 만 참조한다. 백엔드 구현을 직접 부르지 않는다.

| 계층 | 위치 | 역할 |
|---|---|---|
| 상태 모델 | `yt_rec.state.models` | GUI가 그리는 불변 데이터 |
| 이벤트 | `yt_rec.state.events` | 백엔드 → 상태 계층 통지 |
| 명령 | `yt_rec.state.commands` | 화면 → 백엔드 요청 |
| 저장소 | `yt_rec.state.store.AppState` | 이벤트 적용, Qt 시그널 방출, 갱신 빈도 제한, 명령 전달 |
| 스텁 | `yt_rec.state.stub.StubEventSource` | 백엔드 없이 화면을 개발·테스트하는 하니스 |

- 진행 중 녹화의 크기·경과 시간은 `Recording.reported_bytes` / `reported_elapsed`
  를 그대로 쓴다. `os.stat`·`Path.stat`·`getsize` 로 다시 재지 않는다.
- 갱신은 기본 200ms 마다 한 번으로 묶인다. 초당 수백 건이 들어와도 화면 갱신은
  초당 5회를 넘지 않는다.
- 보조 문구 색은 스타일시트에 고정하지 않고 `ui.widgets.set_muted()` 를 쓴다.
  `palette(dark)` 같은 값은 다크 테마에서 배경과 겹쳐 글자가 사라진다.
- 상태·수치를 담은 짧은 문구는 `ElidedLabel` 처럼 말줄임할 수 있는 라벨에 넣는다.
  평범한 `QLabel` 은 폭이 모자라면 넘치는 글자를 아무 표시 없이 잘라 내
  **틀린 값**을 보여 준다(`오류 1234건` → `오류 123`).

#### 스레드

**작업 스레드에서 부를 수 있는 것은 `AppState.post_event()` 하나뿐이다.**
나머지 메서드와 프로퍼티(`apply`, `apply_all`, `flush`, `snapshot`,
`mark_errors_seen`, `send_command`, `connection`, `recordings`, …)를 다른
스레드에서 부르면 그 자리에서 `RuntimeError` 가 난다. 예전에는 조용히 통과한 뒤
시그널이 한 번도 방출되지 않아 화면이 영구 정지했다.

이벤트 주입 경로는 셋이고 순서 의미는 하나로 맞춰 두었다.

| 경로 | 누가 | 언제 적용되나 |
|---|---|---|
| `AppState.apply(event)` / `apply_all(events)` | GUI 스레드 전용 | 즉시(동기) |
| `AppState.post_event(event)` | 어느 스레드든 | 같은 스레드면 즉시, 작업 스레드면 GUI 스레드로 큐잉 |
| `EventSource.event_ready` (`attach` 로 연결) | 어느 스레드든 | Qt 자동 연결 — 같은 스레드면 즉시, 다른 스레드면 큐잉 |

한 문장으로: **같은 스레드에서 보낸 이벤트는 부른 순서대로 즉시 적용되고, 다른
스레드에서 보낸 이벤트는 GUI 이벤트 루프에 도착한 순서대로 적용된다.**

#### 시간대

**모델과 이벤트의 모든 `datetime` 은 시간대를 가진(aware) 값이다.** 어느
시간대인지는 상관없다 — `datetime.now(timezone.utc)` 든
`datetime.now().astimezone()` 이든 좋다.

- **표시하는 쪽은 `ui.formatting.to_local()` 로 로컬로 옮긴 뒤 그린다.**
  `format_timestamp()` / `format_countdown()` 은 이미 그렇게 한다. 새로 시각을
  그리는 코드도 반드시 거쳐야 한다. 안 거치면 로컬 14:47 이 `05:47` 로 표시되고,
  같은 객체의 `last_check_at` 과 `next_check_at` 이 서로 다른 기준으로 그려진다.
- 시간대 없는(naive) 값은 계약 위반이다. 파이썬 표준 규칙대로 **로컬 벽시계
  시각**으로 해석되므로 `datetime.utcnow()` 같은 naive-UTC 는 어긋난 값이 된다.
  `AppState.apply()` 가 `NaiveDatetimeWarning` 으로 알려 준다.
  직접 검사할 때는 `state.events.naive_datetime_fields(event)` 를 쓴다.

#### 화면 → 백엔드 (녹화 중지, 채널 선택, 설정)

화면이 백엔드 객체를 직접 붙잡는 경로는 없다. 저장소가 유일한 창구다.

```python
if not state.stop_recording("rec-1", reason="사용자가 중지했습니다"):
    ...                                    # 백엔드 미연결. command_rejected 로 사유가 온다
state.connect_account()                          # 미연결에서도 백엔드가 붙어 있으면 전달
state.set_watched_channels(["UC...", "UC..."])   # 부분 변경이 아니라 전체 교체
state.update_settings(output_dir=r"D:\recordings", max_quality="1080p")
```

- 세 메서드 모두 `AppState.send_command()` 를 거쳐 `command_requested` 시그널로
  나간다. 백엔드는 그 시그널만 구독한다(작업 스레드면 Qt가 큐 연결로 넘긴다).
- 백엔드가 연결되지 않았으면 보내지 않고 `False` 를 돌려주며
  `command_rejected(command, 사유)` 를 방출한다. 명령이 조용히 사라져
  `눌렀는데 아무 일도 없음` 이 되는 것을 막는다.
- **명령은 요청이지 결과가 아니다.** `True` 는 `전달했다`는 뜻이다. 화면은 명령을
  보낸 뒤 스스로 상태를 바꾸지 말고, 백엔드가 이벤트로 되돌려 주는 결과를 그린다.
  그래야 실패했을 때 화면과 실제가 갈라지지 않는다.
- 새 조작이 필요하면 `state/commands.py` 에 데이터 클래스를 추가하고 `GuiCommand`
  에 붙인다. 화면마다 백엔드에 닿는 방법을 따로 만들지 않는다.
- 창 크기·섹션 접힘처럼 화면에만 있는 표시 상태는 명령이 아니다.
  `ui.settings_store.WindowSettings` 가 로컬에 저장한다.

## 라이선스

미정.
