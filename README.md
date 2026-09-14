# yt-rec

선택한 YouTube 채널의 방송 알림을 받아 라이브를 녹화하는 standalone 데스크톱 앱.
알림이 도착하면 그 영상만 확인하며, 방송 목록을 주기적으로 조회하지 않는다.

## 상태

Google 계정 연결, 채널 선택, 자동 녹화, 설정, 보관함, 로그와 트레이 동작을 제공한다.
일반 실행은 앱 안의 YouTube 알림 수신기를 사용한다. 앱과 녹화 백엔드의 연결은
가짜 입력으로 검증했으며, 실제 YouTube 알림 전달 관측과 전체 수신 범위는
[수신기 검증 #38](https://github.com/kor-haru/yt_rec/issues/38)과
[알림 자동 녹화 #39](https://github.com/kor-haru/yt_rec/issues/39)에서 별도로 확인한다.
네이티브 실행 파일은 자동 빌드로 검증하며, 정식 서명 배포와 운영체제별 실제 로그인·녹화
검증 현황은 [이슈](https://github.com/kor-haru/yt_rec/issues)에서 관리한다.

## 기술 선택

| 항목 | 선택 | 이유 |
|---|---|---|
| 언어 | Python (3.11 이상) | 유지보수자가 전체 동작을 읽고 파악할 수 있어야 한다 |
| GUI | PySide6 (Qt Widgets) | LGPL. QML은 사용하지 않는다 — 순수 Python으로만 화면을 구성한다 |
| 미디어 | yt-dlp, ffmpeg | 외부 실행 파일로 호출한다 |
| 환경·의존성·빌드 | uv | 잠금 파일로 세 OS에서 같은 의존성 트리를 재현한다 |

### 제약

- **QtWebEngine은 YouTube 알림 수신에만 사용한다.** 앱 전용 Chromium 프로필에서 YouTube가 보내는 네이티브 알림을 받는다. 구독 목록을 읽는 OAuth는 별도로 시스템 기본 브라우저와 로컬 루프백 서버를 사용한다. PySide6 6.11.1 이상(7 미만)이 필요하며 `uv sync`가 잠금 파일의 검증 버전을 설치한다.
- **방송 발견용 폴링이나 숨은 대체 조회는 없다.** 알림 수신에 실패하면 상태와 로그에 표시한다. 수신 실패를 주기 조회로 우회하지 않는다.
- **QML을 사용하지 않는다.** Qt Widgets만 사용한다.
- **GUI는 파일시스템이나 외부 프로세스를 직접 폴링하지 않는다.** 상태 변경은 백엔드가 시그널로 통지한다. 특히 녹화 중인 파일 크기를 `os.stat`으로 읽으면 안 된다 — Windows는 쓰기 핸들이 열린 파일의 크기를 디렉터리 엔트리에 즉시 반영하지 않아 실제보다 훨씬 작은 값이 표시된다.

## 사용법

### 시작하기 전에

실행 파일을 받았다면 아래 **실행 파일로 시작하기**를 따른다. Python이나 별도
미디어 도구를 설치할 필요가 없다. 소스 ZIP을 받았다면 **1. 프로젝트 파일 받기**부터
진행한다. 두 방법 모두 Google OAuth 준비와 첫 로그인은 필요하다.

준비물은 다음과 같다.

- 안정적인 인터넷 연결
- 감시할 채널을 구독한 Google/YouTube 계정
- 녹화 파일을 저장할 충분한 디스크 공간
- 소스에서 직접 실행할 때만: Windows의 PowerShell 또는 macOS의 터미널(zsh)

Google에서 내려받은 OAuth JSON 파일에는 비밀 값이 들어 있다. 이 파일과 환경
변수 값을 Git, GitHub, 메신저, 이메일, 스크린샷으로 공유하지 않는다. yt-rec가
로그인 뒤 받은 토큰은 Windows 자격 증명 관리자, macOS Keychain 또는 Linux
Secret Service에 저장한다. 보안 저장소를 사용할 수 없다면 계정 화면에서
**이번 실행에서만 로그인 유지**를 선택할 수 있다. 이 경우 앱을 다시 열 때 로그인한다.
평문 토큰 파일은 만들지 않는다.

### 실행 파일로 시작하기

1. Windows에서는 제공받은 **yt-rec-win32-x86_64.zip**을 준비한다(Intel/AMD 64비트용).
   GitHub에서 받는 경우 [Desktop bundle 빌드 목록](https://github.com/kor-haru/yt_rec/actions/workflows/desktop.yml)의
   성공한 빌드를 열고 **Artifacts**의 `yt-rec-windows-2022`를 받는다. GitHub 로그인이 필요하다.
   Apple Silicon Mac은 `macos-15`,
   Intel Mac은 `macos-15-intel`, Linux는 자신의 CPU에 맞는 `ubuntu-22.04` 또는
   `ubuntu-24.04-arm`을 선택한다. Linux ARM64 실행 파일은 Ubuntu 24.04 이상이 필요하다.
2. Windows에서는 ZIP을 오른쪽 클릭해 **모두 압축 풀기**를 누른다. 안에 ZIP이나
   `tar.gz`가 한 번 더 있으면 그것도 푼다. 압축 파일 안에서 바로 실행하지 않는다.
3. 풀린 **yt-rec 폴더 전체**를 계속 사용할 위치에 둔다. 예를 들어 `D:\Apps\yt-rec`다.
   안에 있는 `_internal` 폴더와 `README.md`를 그대로 둔다. 실행 파일 하나만 복사하면 작동하지 않는다.
4. 이전 yt-rec가 켜져 있다면 먼저 **앱 → 종료**로 끝내고 녹화 마무리를 기다린다.
   이 메뉴가 없는 구버전은 녹화 중이 아닌지 확인한 뒤 창의 **X**로 닫는다.
   그다음 풀어 둔 폴더의 **yt-rec.exe**를 더블클릭한다. Python·uv·명령어 입력은 필요 없다.
   예전 소스 폴더의 실행 명령이나 바로가기를 사용하면 이전 버전이 열릴 수 있으므로
   이번에 받은 파일을 실행한다. macOS는 **yt-rec.app**, Linux는 **yt-rec**를 실행한다.
   macOS의 앱은 응용 프로그램 폴더에 옮긴 뒤 실행할 수 있다.
5. 창이 열리면 아래 **6. Google 로그인 준비하기**로 이동한다. 이미 OAuth JSON이
   있다면 **7. OAuth JSON 가져오기**부터 시작한다.

자동 빌드 파일은 아직 정식 서명·공증 릴리스가 아니다. 운영체제나 조직 정책이 실행을
차단하면 관리자에게 확인한다. Linux는 그래픽 데스크톱과 Secret Service(예: GNOME
Keyring)가 필요하며, `secret-tool`이 없다면 배포판의 `libsecret-tools` 패키지를 설치한다.

### 창을 닫아도 녹화가 계속되는 이유

시계 옆 트레이(Windows/Linux) 또는 위쪽 메뉴 막대(macOS)에 yt-rec 아이콘이 있으면
창의 **X**는 창만 숨긴다. 아이콘을 누르면 다시 열 수 있다. 앱을 완전히 끝내려면
아이콘 메뉴의 **종료** 또는 메인 창의 **앱 → 종료**를 누른다. 녹화 중이라면 확인 창이
나오고, 받은 영상의 저장·병합이 끝날 때까지 종료 안내가 표시된다.

트레이를 지원하지 않는 환경에서는 창을 닫으면 종료 절차로 들어간다. **시작 시 창 숨김**을
켜도 트레이가 없으면 창이 보인다. 설정의 **로그인 시 자동 시작**은 다음 컴퓨터 로그인부터
적용된다. 자동 시작을 켠 뒤 실행 파일이나 소스 폴더를 옮겼다면 설정을 껐다가 다시 켠다.

### 1. 프로젝트 파일 받기

개발 도구에 익숙하지 않다면 ZIP 파일을 받는 방법이 가장 쉽다.

1. [main 브랜치 ZIP 파일](https://github.com/kor-haru/yt_rec/archive/refs/heads/main.zip)을 받는다.
2. 받은 파일을 마우스 오른쪽 버튼으로 누르고 **모두 압축 풀기**를 선택한다.
3. 압축을 푼 `yt_rec-main` 폴더를 연다.
4. 그 폴더에서 터미널을 연다.

   - **Windows 11:** 폴더의 빈 곳을 마우스 오른쪽 버튼으로 누르고 **터미널에서 열기**.
     Windows 10은 파일 탐색기 주소 표시줄에 `powershell`을 입력하고 Enter.
   - **macOS:** `yt_rec-main` 폴더를 마우스 오른쪽 버튼으로 누르고 **폴더에서 새로운
     터미널 시작**. 또는 터미널에서 `cd`로 그 폴더로 이동한다.

폴더가 맞는지 확인한다.

```powershell
# Windows (PowerShell)
Test-Path .\pyproject.toml
```

```bash
# macOS
test -f pyproject.toml && echo ok
```

`True` 또는 `ok`가 나오면 올바른 폴더다.

Git을 쓰는 사람은 ZIP 대신 다음과 같이 받을 수 있다.

```powershell
# Windows — git 이 없으면 Git for Windows (https://git-scm.com/download/win)
Set-Location "$HOME\Downloads"
git clone https://github.com/kor-haru/yt_rec.git
Set-Location .\yt_rec
```

```bash
# macOS — git 이 없으면 xcode-select --install 또는 https://git-scm.com/download/mac
cd ~/Downloads
git clone https://github.com/kor-haru/yt_rec.git
cd yt_rec
```

이후의 모든 명령은 `pyproject.toml`이 있는 프로젝트 폴더에서 실행한다.

### 2. uv 설치하기

[uv](https://docs.astral.sh/uv/getting-started/installation/)는 yt-rec에 필요한
Python과 프로그램 구성 요소를 준비하는 도구다. 다음 명령을 한 번 실행한다.

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

```bash
# macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

설치가 끝나면 터미널을 닫았다가 프로젝트 폴더에서 다시 연다. macOS에서 `uv`를
찾을 수 없으면 같은 터미널에서 `source "$HOME/.local/bin/env"`를 한 뒤
`uv --version`을 다시 실행한다. 다음 명령에 버전 번호가 나오면 설치된 것이다.

```text
uv --version
```

설치 과정이 막히면 [uv 설치 프로그램 안내](https://docs.astral.sh/uv/configuration/installer/)를 확인한다.

### 3. yt-rec 구성 요소 설치하기

프로젝트 폴더에서 다음 명령을 실행한다.

```text
uv sync
```

처음 실행할 때는 Python과 여러 구성 요소를 내려받으므로 시간이 걸릴 수 있다.
완료될 때까지 터미널 창을 닫지 않는다.

### 4. 녹화 도구 설치하기

소스로 실행할 때는 `yt-dlp`, `ffmpeg`, `ffprobe`와 YouTube 다운로드 주소를
해석하는 데 쓰는 `Deno`를 따로 설치해야 한다. 실행 파일 배포판에는 모두 포함되어 있다.

**Windows** — PowerShell에서 다음 세 명령을 차례로 실행한다. 설치 확인 창이
나타나면 내용을 확인하고 진행한다.

```powershell
winget install --id yt-dlp.yt-dlp -e --source winget
winget install --id Gyan.FFmpeg -e --source winget
winget install --id DenoLand.Deno -e --source winget
```

WinGet을 쓸 수 없다면
[yt-dlp 공식 배포 파일](https://github.com/yt-dlp/yt-dlp#release-files)과
[FFmpeg 공식 Windows 다운로드 안내](https://ffmpeg.org/download.html#build-windows),
[Deno 공식 설치 안내](https://docs.deno.com/runtime/getting_started/installation/)를 따른다.
설치 위치와 `PATH`가 어렵다면
[yt-dlp Windows FAQ](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#on-windows-how-should-i-set-up-ffmpeg-and-yt-dlp-where-should-i-put-the-exe-files)를
참고한다. `ffprobe.exe`도 빠뜨리면 안 된다.

**macOS** — [Homebrew](https://brew.sh/)가 없으면 그 사이트 안내대로 먼저 설치한
뒤 다음을 실행한다.

```bash
brew install yt-dlp ffmpeg deno
```

터미널을 닫았다가 프로젝트 폴더에서 다시 연 뒤 네 명령을 확인한다.

```text
yt-dlp --version
ffmpeg -version
ffprobe -version
deno --version
```

네 명령 모두 버전 정보를 보여야 한다. Deno를 찾지 못하면 위 공식 설치 안내의
PATH 설정을 확인한다. 나머지 도구가 PATH에 없다면 환경 변수
`YT_REC_YTDLP`, `YT_REC_FFMPEG`, `YT_REC_FFPROBE`로 실행 파일 전체 경로를
지정할 수 있다.

### 5. 가짜 데이터로 화면 먼저 확인하기

Google 로그인 전에 GUI가 정상적으로 열리는지 확인한다.

```text
uv run yt-rec --stub populated
```

`--stub`은 **가짜 데이터만 보여 주는 안전한 화면 시험 모드**다. Google에
로그인하지 않고, 채널을 조회하지 않으며, 실제 녹화 파일도 만들지 않는다.
샘플 채널·녹화·완료 항목이 보이면 기본 실행 환경이 준비된 것이다.

창을 닫은 뒤 다음 명령으로 녹화 시작부터 완료와 오류까지 변하는 화면도 볼 수 있다.

```text
uv run yt-rec --stub scenario
```

### 6. Google Cloud에서 로그인 파일 준비하기

yt-rec는 구독 채널을 읽고, 방송 알림이 가리키는 영상 한 건이 실제 라이브인지 확인하기 위해 YouTube Data API를 사용한다. 다음
설정은 처음 한 번만 준비하면 된다.
Google Cloud 프로젝트를 관리하는 계정과 실제로 녹화에 사용할 Google 계정은 달라도 된다.
프로젝트 관리자는 OAuth 앱을 한 번 등록하고, 사용자는 로그인 화면에서 자기 계정을 고른다.
메뉴 번역이나 위치는 바뀔 수 있다. 현재 영문 이름은 `Overview`, `Branding`,
`Audience`, `Clients`, `Data Access`이며, 보이지 않으면 아래 공식 링크에서 연다.

1. [Google Cloud Console](https://console.cloud.google.com/)에 로그인하고 새
   프로젝트를 만들거나 사용할 프로젝트를 선택한다.
2. [YouTube Data API v3 활성화 안내](https://developers.google.com/youtube/v3/guides/auth/installed-apps#enable-apis-for-your-project)에
   따라 **YouTube Data API v3**를 찾아 **Enable(사용)**을 누른다.
3. **Google Auth Platform**의 `Overview`에서 앱 이름과 사용자 지원 이메일을
   입력한다. 개인 Gmail 계정은 사용자 유형으로 `External`을 고른다. 자세한 항목은
   [Google Auth Platform 시작 안내](https://support.google.com/cloud/answer/15544987)를 참고한다.
4. 개발 계정과 다른 개인 계정으로 로그인하려면 `Audience`를 **External**로 둔다.
   `Testing`이면 실제로 로그인할 다른 Google 계정을 **Test users**에 추가한다.
   테스트 상태의 승인은 7일 뒤 만료될 수 있다. 관련 제한은
   [Audience 안내](https://support.google.com/cloud/answer/15549945)를 참고한다.
   Test users에 없는 계정까지 이용하게 하려면 앱 게시와 필요한 Google 검증을 준비해야 한다.
   앱의 로그인 버튼만 바꾸어 Google의 대상 사용자 제한을 없앨 수는 없다.
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

### 7. OAuth JSON 가져오기

1. 앱을 연다. 소스로 실행한다면 프로젝트 폴더의 터미널에서 `uv run yt-rec`를 실행한다.
2. 오른쪽 위 **계정 → OAuth JSON 가져오기**를 누른다.
3. Google에서 내려받은 **Desktop app** JSON을 선택한다. 파일 이름은 바꾸지 않아도 된다.
4. 앱이 파일 형식을 확인하고 사용자 설정 폴더에 저장한다. 웹 앱용 파일이나 클라이언트 ID만
   적힌 파일은 사용할 수 없다. Google에서 받은 클라이언트 ID와 secret이 포함된 JSON을 사용한다.

계정 화면의 **Google Auth Platform 열기**로 설정 화면에 갈 수도 있다.
이전에 `YT_REC_GOOGLE_CLIENT_SECRETS` 환경변수로 별도 경로를 지정했다면 앱의 안내에 따라
그 변수를 해제하고 다시 실행한 뒤 가져온다. JSON과 토큰은 GitHub에 올리지 않는다.

### 8. 실제 모드로 실행하기

프로젝트 폴더에서 다음 명령을 실행한다.

```text
uv run yt-rec
```

`--stub`이 없으므로 이번에는 실제 Google 로그인과 자동 녹화 기능이 동작한다.
터미널 창은 앱이 실행되는 동안 함께 열어 둔다.

### 9. 계정과 자동 녹화 설정하기

1. 창 오른쪽 위의 **계정**을 누른 뒤 **연결**을 누른다.
2. 시스템 기본 브라우저가 열리면 사용할 Google 계정을 고른다. 개발 계정과 다른 계정은
   **다른 계정 사용**을 누르고 로그인한다. 프로젝트가
   `Testing` 상태이면 테스트 또는 미확인 앱 경고가 나타날 수 있다. 계속하기 전에
   방금 만든 앱 이름이 맞고 요청 권한이 YouTube 읽기 전용
   (`youtube.readonly`)뿐인지 확인한다. 앱 이름이 다르거나 다른 권한도 요구하면
   진행하지 말고 창을 닫는다. 확인한 뒤 승인하고 앱으로 돌아온다.
3. 메인 화면에서 **채널 관리**를 누른다.
4. 자동 녹화할 구독 채널을 체크한다. 체크할 때마다 바로 저장되므로 별도 저장
   버튼은 없다.
   모두 녹화 대상으로 넣으려면 **구독 채널 전체 선택**을 누른다. **전체 보기**는 목록을 보여 주는 필터이며 채널을 선택하지 않는다.
5. 메인 화면의 **YouTube 로그인**을 누른다. 앱 안에 열린 YouTube에서 앞서 연결한
   구독 계정과 **같은 계정**으로 로그인한다. 첫 번째 로그인은 구독 목록을 읽기 위한
   것이고, 이 로그인은 방송 알림을 받기 위한 것이다. 두 로그인은 자동으로 공유되지 않는다.
6. **YouTube 알림 설정**을 누른다. YouTube의 데스크톱 알림을 켜고, 이 앱이
   `https://www.youtube.com`의 알림 허용 여부를 물으면 주소를 확인한 뒤 **허용**한다.
   녹화할 채널에서도 구독 알림(종 모양)을 **전체**로 설정한다.
7. 메인 화면이나 YouTube 창 위의 **알림 상태 확인**을 누른다. 이 버튼은 현재 등록을
   한 번만 확인하며 권한이나 설정을 바꾸지 않는다. **알림 대기(수신 이력 없음)**은
   수신 준비가 됐다는 뜻이며, 실제 방송 알림이 이미 도착했다는 뜻은 아니다.
   로그인·권한·수신 등록 또는 오류 안내가 남아 있으면 아래 설명과 상세 문구를 확인한다.
   YouTube 창을 닫으면 등록 상태를 한 번 확인해 메인 화면을 갱신한다.
   yt-rec가 켜져 있는 동안 수신기는 계속 동작한다.
8. 선택한 채널의 알림이 도착하면 영상 ID 한 건을 확인하고 라이브 녹화를 시작한다.
   **녹화 중** 카드의 크기와 경과 시간이 늘어나는지 확인한다. 녹화 대상으로 선택하지
   않은 채널, 종료된 영상, 이미 처리한 영상은 녹화하지 않는다.
9. 오류 수가 늘거나 동작을 자세히 보고 싶으면 오른쪽 위의 **로그**를 연다.
   수준 필터, 메시지 검색, 선택한 행 복사를 사용할 수 있다.
10. 계정을 바꾸려면 **계정 → 연결 해제** 후 **연결**을 누르고 다른 계정을 고른다.
    **YouTube 로그인** 창에서도 같은 계정으로 바꾼다. 계정 메뉴의 연결 해제는
    브라우저의 YouTube 로그인까지 지우지 않는다.
11. 앱을 끝낼 때는 **앱 → 종료**를 누른다. 진행 중 녹화가 있으면 받은 부분을 마무리하는
   동안 시간이 걸릴 수 있으므로 터미널을 강제로 닫지 않는다.

알림이 늦거나 오지 않으면 자동 녹화가 늦게 시작되거나 시작되지 않을 수 있다.
앱을 나중에 켜도 이미 지나간 알림을 방송 목록 조회로 보충하지 않는다. 방송 시작 지점부터
받기를 시도하지만 YouTube의 다시보기(DVR) 제공 범위에 따라 앞부분이 빠질 수 있다.
수신 준비 표시나 한 번의 알림 수신으로 모든 방송의 수신·전체 녹화를 보장하지 않는다.

#### 데스크톱 알림이 켜져 있는데도 준비가 안 될 때

YouTube의 스위치 표시, 이 앱 브라우저의 알림 권한, 실제 푸시 수신 등록은 서로 다른
상태다. 스위치가 **켜짐**이어도 등록이 없을 수 있으므로, 앱은 스위치를 끄거나 다시
켰다고 추정하지 않는다. **알림 상태 확인**을 눌러 아래 중 어느 상태인지 확인한다.
설정을 바꾼 직후에도 메인 화면에는 이전 확인 결과가 남아 있을 수 있다. 스위치를
켠 뒤에는 메인 화면의 **알림 상태 확인**을 눌러 결과를 갱신한다. 앱이 자동으로
반복 확인하지 않으며, 이 버튼으로 새 방송 목록을 조회하지도 않는다.

- **알림 권한 필요**: 앱 안의 YouTube 브라우저에서 알림 권한이 허용되지 않았다.
  **YouTube 알림 설정**에서 정상 권한 요청을 확인한다.
- **수신 등록 없음 / 수신기 준비 중**: YouTube 수신 프로그램(서비스 워커)이 없거나
  아직 활성화되지 않았다. 앱 안의 **YouTube 로그인**과 **YouTube 알림 설정**을 확인하고,
  페이지가 열린 뒤 **알림 상태 확인**을 누른다.
- **푸시 등록 없음**: 브라우저 권한과 수신 프로그램은 있지만 푸시 구독 등록이 없다.
  내장 브라우저의 데스크톱 알림이 이미 켜져 있다면, 꺼져 있다고 단정하거나 반복해서
  토글하지 않는다. 상세 문구와 **로그**를 확인한다. Windows 알림을 켜는 것만으로
  이 푸시 등록이 만들어지지는 않는다.
- **수신기 연결 중 / 알림 확인 중**은 작업 중이며, **알림 수신 종료**는 수신기가
  정지한 상태다. **알림 수신 오류**는 확인 실패이며, 권한 부족으로 단정한 표시가 아니다.

Windows 팝업 알림도 확인하려면 메인 화면의 **Windows 알림 설정**을 누른다.
**설정 → 시스템 → 알림**이 열린다. 알림 허용과 방해 금지 항목을 직접 확인할 수 있다.
이 버튼은 설정 화면만 열며 값을 바꾸거나 OS 허용 여부를 검사하지 않는다.
방해 금지는 팝업 표시와 관계가 있지만, 이를 끄거나 설정 화면을 열었다는 사실이
YouTube 푸시 수신 등록 또는 실제 방송 알림 도착을 증명하지는 않는다.
버튼으로 열리지 않으면 Windows 시작 메뉴에서 같은 경로를 직접 연다.
macOS·Linux에서는 해당 운영체제의 알림 설정을 직접 연다.

평소 사용하는 Chrome·Edge와 앱 내장 브라우저의 로그인·알림 권한은 별개다.
일반 브라우저 프로필을 복사하거나 앱 프로필을 초기화하지 않는다.

브라우저 로그인과 알림 등록은 앱 전용 사용자 데이터 폴더에 보관된다. Windows에서는
`%LOCALAPPDATA%\yt-rec\youtube-push`다. 일반 Chrome 프로필을 사용하거나 쿠키를
추출하지 않는다. 이전 시험 앱의 프로필을 사용 중이라면 실행 중에 복사하거나 다른 앱과
동시에 열지 않는다. 이미 녹화 중인 별도 명령행 프로그램이 있으면 같은 채널을 추가로
선택하지 않는다. 서로 다른 앱·저장 위치 사이의 중복 녹화까지 조정하지는 않는다.

#### 받은 알림의 제목과 본문 확인하기

1. 메인 화면에서 **알림 이력**을 누른다.
2. 위 목록에서 확인할 알림을 선택한다. **받은 시각**은 이 컴퓨터의 로컬 시간이며,
   새로 받은 알림이 맨 위에 나온다.
3. 아래 영역에서 제목과 본문 전체를 읽는다. 긴 내용은 아래 영역 안에서 스크롤할 수 있고,
   가운데 경계선을 끌어 읽는 영역을 넓힐 수 있다. 내용은 실행되지 않는 일반 텍스트다.
4. 필요 없는 항목은 **선택 이력 삭제**를 누른 뒤 확인한다. 선택한 알림 한 건만 지워지며
   녹화 파일·진행 중 녹화·채널 선택은 바뀌지 않는다. 삭제한 이력은 되돌릴 수 없다.

이 화면은 **앱 전용 브라우저가 받은 YouTube 알림**을 보관한다. 녹화 대상이 아닌 알림이나
영상 번호를 식별하지 못한 알림도 남는다. 이력이 있다고 녹화가 성공했거나 방송 시작부터
전부 저장됐다는 뜻은 아니다. 수신 준비 상태 확인은 이력에 추가되지 않는다.
아직 받은 알림이 없으면 빈 화면 안내가 나온다. 이 기능을 쓰기 전에 받았던 알림이나
앱이 꺼져 있을 때 다른 브라우저·휴대전화가 받은 알림은 소급해서 만들지 않는다.

최신 **500건**을 앱 종료 후에도 보관하며, 상한을 넘으면 가장 오래된 항목부터 제외한다.
저장 필드는 이력 구분 번호·받은 시각·알림 제목·본문뿐이다. 푸시 원본 데이터, 인증 토큰,
수신 서버 주소(endpoint), 쿠키는 수집하지 않는다. 수신기의 길이 제한(제목 4,096자,
본문 16,384자)을 넘는 비정상 입력은 저장하지 않는다. 이력은 현재 OS 사용자 안에서
공유되며, 로그인 계정을 바꿔도 남으므로 필요하면 해당 항목을 삭제한다.

이력 파일은 녹화 폴더가 아니라 다음 사용자 설정 폴더의 `notification-history.json`이다.

- Windows: `%APPDATA%\yt-rec\notification-history.json`
- macOS: `~/Library/Application Support/yt-rec/notification-history.json`
- Linux: `$XDG_CONFIG_HOME/yt-rec/notification-history.json` (미지정 시 `~/.config/yt-rec/notification-history.json`)

저장 공간·접근 권한 문제나 손상된 이력이 있으면 이력 화면에 경고가 나온다. 읽지 못한
원본을 빈 파일로 덮어쓰지 않는다. 새 알림은 이번 실행에서만 표시될 수 있지만 녹화 처리는
계속되며, 삭제 저장에 실패하면 해당 항목도 그대로 남는다. 제목·본문에는 개인적인 내용이
있을 수 있으므로 이력 파일을 다른 사람에게 그대로 공유하지 않는다.

### 10. 녹화 파일과 복구 결과 확인하기

처음 설치한 앱의 기본 저장 위치는 사용자 홈 폴더의 `Videos/yt-rec`다.
이전 버전에서 저장한 위치는 유지된다. **설정**에서 저장 위치와 남은 용량을 확인하고
**보관함 열기**에서 파일을 찾는다. 기본 화질 상한은 1080p다.

보관함은 제목·채널 검색과 날짜·크기 정렬을 지원한다. 항목을 선택한 뒤 **재생**,
**폴더 열기**, **경로 복사**로 파일을 사용할 수 있다. 다른 폴더로 옮기거나 삭제한 파일은
누락 상태로 표시되며, 앱을 다시 열어도 완료 이력은 남는다.

설정에서는 저장 위치, 최대 화질, 동시 녹화 수, 자동 시작, 시작 시 창 숨김,
최소화 시 트레이로 보내기, 로그 보관 기간과 알림을 바꿀 수 있다. **저장**을 눌러야 적용된다. 녹화 중 저장 위치나
화질을 바꾸면 다음 녹화부터 적용되고, 현재 녹화는 기존 설정으로 마무리된다.
방송 확인 주기 설정은 더 이상 사용하지 않는다. 이전 설정 파일의 값은 호환을 위해
남겨 두지만 일반 실행의 방송 발견에는 쓰이지 않는다. 설정의 **녹화 및 오류 알림 표시**는
앱이 띄우는 결과 알림이며, YouTube에서 방송 알림을 받는 권한과는 별개다.

결과 표시는 다음 의미다.

- **정상**: 재생 검증을 통과한 최종 녹화 파일이다.
- **부분 복구**: 재생 가능한 파일은 만들었지만 일부 방송 구간이 빠졌을 수 있다.
- **실패**: 다운로드, 병합 또는 검증을 끝내지 못했다. 복구 가능한 중간 파일은
  설정한 저장 위치의 `.yt-rec` 아래에 남겨 두며, 앱을 다음에 실행할 때 자동 복구를
  시도한다. 이 폴더를 임의로 지우지 않는다.

긴 방송은 디스크를 빠르게 채운다. 녹화 전후로 저장 폴더가 있는 드라이브의
남은 공간을 확인한다.

#### 최소화한 창을 작업 표시줄에서 숨기기

1. 메인 화면 오른쪽 위의 **설정**을 누른다.
2. **최소화하면 트레이로 보내기**를 체크하고 **저장**을 누른다. 저장이 끝나면
   바로 적용되며, 다음에 앱을 실행해도 선택이 유지된다.
3. 메인 창 오른쪽 위의 **최소화(—)** 버튼을 누른다. 창은 작업 표시줄에서 사라지고
   시계 옆 알림 영역(트레이)에 yt-rec 아이콘이 남는다. 방송 알림 수신과 녹화는 계속된다.
4. 다시 보려면 트레이의 yt-rec 아이콘을 누르거나, 아이콘을 오른쪽 클릭해
   **yt-rec 열기**를 고른다. Windows에서 아이콘이 안 보이면 시계 옆 **숨겨진 아이콘 표시(∧)**를 누른다.

이 설정은 처음에는 꺼져 있다. 체크를 해제하고 저장하면 일반 프로그램처럼 작업
표시줄에 최소화된다. 트레이를 지원하지 않는 환경에서도 작업 표시줄에 남는다.
**시작할 때 창을 숨기고 트레이로 실행**은 앱을 처음 켤 때의 동작이고,
**최소화하면 트레이로 보내기**는 사용 중 최소화 버튼을 눌렀을 때의 동작이다.
닫기(X)는 기존처럼 트레이로 숨기며, 앱을 완전히 끝내려면 **앱 → 종료** 또는
트레이 메뉴의 **종료**를 누른다. 트레이가 없는 환경에서는 닫기(X)가 종료 동작이다.

### 11. 자주 발생하는 문제

#### `uv`, `yt-dlp`, `ffmpeg`, `ffprobe`, `deno` 명령을 찾을 수 없음

터미널을 모두 닫았다가 다시 연다. 그래도 안 되면 2단계와 4단계의 설치 명령을
다시 실행하고 위에 나온 버전 확인 명령부터 확인한다. macOS에서 `uv`만 없다면
`source "$HOME/.local/bin/env"` 또는 Homebrew로 설치한 경우 `brew --prefix`가
PATH에 있는지 확인한다.

#### GUI가 열리지 않음

현재 폴더에 `pyproject.toml`이 있는지 확인한 뒤 다음 명령을 차례로 실행한다.

```text
uv sync
uv run yt-rec --help
uv run yt-rec --stub populated
```

Windows에서 `애플리케이션 제어 정책에서 이 파일을 차단했습니다`라는 문구가
나오면 Windows나 회사·학교의 보안 정책이 uv의 Python 실행을 막은 것이다. 보안
기능을 임의로 끄지 말고 PC 관리자에게 허용 방법을 문의한다.

#### Google 연결 뒤 다시 `연결 안 됨`으로 돌아옴

- Windows: `%APPDATA%\yt-rec\client_secrets.json`
- macOS: `~/Library/Application Support/yt-rec/client_secrets.json`
- YouTube Data API v3 활성화와 `Audience`의 Test users를 확인한다.
- 테스트 승인이 만료됐다면 **계정 → 연결**로 다시 로그인한다.
- 자세한 원인은 **로그**에서 확인한다. 비밀 값은 공유하지 않는다.

#### 브라우저 로그인이 끝나지 않음

기본 브라우저가 로컬 주소 `127.0.0.1` 연결을 막지 않는지 확인한다. Windows는
방화벽, macOS는 **시스템 설정 → 네트워크 → 방화벽**에서 확인한다. 로그인은
3분 안에 끝내야 한다. 시간이 지났다면 앱에서 **연결**을 다시 누른다.

#### 채널이 보이지 않거나 라이브 녹화가 시작되지 않음

**계정**에서 연결 상태를 확인하고 **채널 관리 → 다시 불러오기**를 누른다. 자동
녹화할 채널이 체크되어 있어야 위쪽에 **감시 중 N채널**이 표시된다. API 하루
할당량을 다 썼다면 로그에 quota 오류가 나타나며, 할당량이 다시 생길 때까지
기다려야 한다.
라이브인데도 시작하지 않으면 **로그**에서 네트워크, `yt-dlp`, `ffmpeg` 오류를
확인한다.

### 로그와 오류 알림

상단 **로그** 버튼의 숫자는 아직 확인하지 않은 오류다. 100건 이상은 `99+`로 보이고
버튼에 마우스를 올리면 정확한 건수를 확인할 수 있다. 로그를 열면 미확인 표시가 해제된다.
로그 화면에서 수준·검색어로 필터링하고 선택 행을 복사하거나 전체 로그 폴더를 열 수 있다.
토큰·인증 값은 자동으로 가려지고, 로그 파일은 크기와 설정한 보관 기간에 따라 정리된다.
녹화 실패나 감시 중단 시 트레이 알림을 보내며 **설정**에서 알림을 끌 수 있다.
운영체제의 알림 금지·집중 모드가 켜져 있으면 알림이 안 보일 수 있다.

## 녹화 엔진

`yt_rec.recording` 이 video id 하나를 방송 시작 지점부터 녹화해 재생 가능한 단일
파일로 마무리한다. GUI 없이도 돌릴 수 있다.

```python
from pathlib import Path
from yt_rec.recording import RecordingEngine, RecordingOptions

engine = RecordingEngine(RecordingOptions(output_dir=Path("recordings"), max_height=1080))
result = engine.record("VIDEO_ID")      # 방송이 끝날 때까지 블로킹
print(result.status, result.output_path)
```

손으로 돌려 볼 때는 딸린 명령줄을 쓴다. `-o` 경로는 쓰는 컴퓨터에 맞게 바꾼다.

```bash
uv run python -m yt_rec.recording record <VIDEO_ID> -o recordings --max-height 1080
uv run python -m yt_rec.recording verify "recordings/2026-08-11_제목.mp4"
uv run python -m yt_rec.recording recover -o recordings
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

### 독립 실행 파일 만들기

대상 운영체제와 같은 OS·CPU에서 다음 명령을 실행한다. `uv.lock`의 앱 의존성을 쓰고,
빌드 도구인 PyInstaller도 앱과 같은 `.venv`에 설치한다. 별도 임시 환경에서 실행하면 macOS의
Qt 구성 파일이 서로 다른 위치에 묶일 수 있다. 고정 버전의 yt-dlp, Deno, FFmpeg/ffprobe를 내려받아
SHA256을 확인하고, 라이선스·빌드 기록과 함께 묶는다. macOS는 Xcode Command Line Tools의
clang과 make로 FFmpeg를 소스 빌드한다. 새로운 런타임 의존성은 추가하지 않는다.

```text
uv sync --frozen
uv pip install --python .venv pyinstaller==6.22.2
uv run --frozen --no-sync python packaging/build.py
```

Windows 결과물은 `dist/yt-rec/yt-rec.exe`와 폴더 전체를 담은
`dist/yt-rec-win32-x86_64.zip`이다. 실행 파일 옆 `README.md`에서 사용법을 다시 볼 수 있다.
다른 OS의 완성 파일은 `dist/yt-rec-<OS>-<CPU>.tar.gz`다. 같은 `dist` 폴더의
`smoke-<OS>-<CPU>.json`은 GUI 렌더, 오프라인 Chromium 페이지·JavaScript 실행,
번들 도구 실행, 합성 영상·음성 병합과 재생 검증 결과다. Windows 검사는 PATH에서
개발 도구 경로를 제외하고 실행한다. `yt-rec/_internal/build-manifest.json`에는 소스 커밋,
미커밋 변경 여부, 소스·잠금 파일·도구의 SHA256과 패키지 버전이 남는다.
검사는 임시 폴더와 스텁만 사용하므로 실제 Google 계정이나 사용자 녹화를 건드리지 않는다.
이 검사는 실제 OAuth·라이브 녹화 검증을 대신하지 않는다. 운영체제별 실제 설치·로그인·녹화와
로그아웃 후 자동 시작 확인은 [수동 검증 절차](docs/recording-manual-checks.md)에 따라 수행한다.
공개 배포 전 라이선스와 서명·공증 준비 사항은 [번들 구성 안내](packaging/THIRD_PARTY.md)를 확인한다.

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
