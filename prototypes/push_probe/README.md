# YouTube 푸시 가능성 시험 (#38)

이 코드는 **가능성 확인용**이다. 녹화 앱에 연결되지 않으며 녹화·다운로드·기존
OAuth 로그인·기존 Chrome 프로필을 사용하지 않는다. #39의 즉시 녹화 통합은 별도 단계다.

## 현재 확인한 것과 아직 모르는 것

- Windows의 Qt 6.11.1에서 로컬 서비스 워커가 만든 **합성 알림**은
  `notificationPresenter → 비동기 WebChannel → getNotifications({tag}) → Notification.data`
  경로로 전달된다. 합성 데이터에서 영상·채널 ID 후보를 확인한다.
- 실제 YouTube 로그인, 알림 권한, 서비스 워커의 푸시 구독, 실제 라이브 알림 수신,
  YouTube의 데이터 형식은 **아직 검증하지 않았다**.
- `notificationPresenter`는 일반 웹 알림에도 호출된다. 콜백 자체나 로컬
  `showNotification()` 성공을 FCM 푸시 수신 증거로 취급하면 안 된다.
- ID가 있더라도 예정/시작/종료 의미는 아직 판정하지 않는다. 표시한 후보로
  녹화를 시작하지 않는다. 실제 데이터 구조와 방송 상태 검증을 먼저 해야 한다.

## 개발자가 합성 시험을 실행하려면

저장소 루트에서 다음 명령을 실행한다. 본 앱의 가상환경 대신 이 폴더 안의
별도 `.venv`에 Qt WebEngine을 포함한 시험 의존성을 설치한다.

```powershell
uv sync --project prototypes/push_probe --python 3.12 --frozen
uv run --project prototypes/push_probe --frozen pytest prototypes/push_probe/test_probe.py -s
```

시험은 임시 폴더의 전용 프로필과 `127.0.0.1`의 임의 포트만 사용한다.
공개 디버깅 포트는 열지 않는다. 출력되는 `evidence-synthetic.json`은 합성 증거이며
실제 계정 토큰·알림 원문은 포함하지 않는다. 시험의 20초 제한은 종료용일 뿐,
알림을 찾기 위한 반복 조회가 아니다.

## 사람이 실제 YouTube 확인을 할 단계

**조율 담당자가 실제 시험 시작을 안내한 후에만** 다음 명령으로 시험 창을 연다.

```powershell
uv run --project prototypes/push_probe --frozen python prototypes/push_probe/probe.py
```

1. 제목이 **“yt-rec 푸시 가능성 시험 — 녹화하지 않음”**인 새 창인지 확인한다.
   프로필은 이 폴더의 `.profile-youtube`에만 저장된다. 이전 브라우저의 로그인은 가져오지 않는다.
2. 이 새 YouTube 화면에서 정상적인 사이트 로그인 절차를 직접 진행한다.
   기존 앱의 Google OAuth 연결과는 별개다. 비밀번호·인증코드를 에이전트에게 전달하지 않는다.
3. Google이 내장 브라우저 로그인을 차단하면 **그 자리에서 중단한다**.
   사용자 에이전트 변경, 쿠키 복사, 보안 옵션 해제로 우회하지 않는다.
   정상 Chrome 기반 별도 수신 경로는 후속 검토 대상이며 이 시험에는 구현되지 않았다.
4. YouTube가 웹 알림을 요청하면 시험 창의 권한 안내에서 허용 여부를 선택한다.
   프로그램이 YouTube에 존재하지 않는 푸시 구독을 임의로 만들어 주지는 않는다.
5. **서비스 워커·알림 권한·푸시 구독 확인 (한 번)**을 누른다. 버튼을 누른
   시점에만 등록·구독 여부를 확인하며 구독 endpoint나 키는 출력하지 않는다.
6. 실제 사이트 알림이 오면 결과가 하단과 터미널에 나타난다. 실패·누락·모호한
   tag는 상태로 남기며 임의의 첫 알림을 고르지 않는다. 실제 예정 알림과 시작
   알림 각각에서 데이터 매핑이 확인되어야 다음 단계로 진행할 수 있다.
7. 창을 닫으면 시험이 끝난다. 기존 녹화와 본 앱은 계속 실행된다.

## 구현 경계

- 영구 이름이 있는 독립 Qt 프로필과 `setPushServiceEnabled(True)`를 사용한다.
- 알림 콜백마다 현재 페이지와 알림의 origin을 확인한다. 같은 origin의
  `getRegistration()`과 정확한 tag의 `getNotifications()`를 각각 한 번 호출한다.
- 태그가 비었거나, 조회 결과가 0/2건 이상이거나, 제목·본문이 교체됐거나,
  알림이 닫히거나, 페이지가 이동하면 후보를 채택하지 않는다.
- JavaScript Promise를 `runJavaScript`의 반환값으로 받지 않는다. 페이지의
  MainWorld와 분리된 ApplicationWorld의 Qt WebChannel로 비동기 결과를 받는다.
- 주기 타이머, DOM 탐색, 피드/API 폴링, DevTools 연결, sandbox 해제,
  브라우저 프로필 복사, 녹화 엔진 호출은 없다.
- 알림의 원문 데이터·URL·토큰은 출력하지 않는다. 제한된 스키마 키와 엄격한
  영상/채널 ID 후보만 보고하며 여러 후보가 있으면 `unresolved`로 남긴다.

근거: [Qt 푸시 활성화](https://doc.qt.io/qt-6/qwebengineprofile.html#setPushServiceEnabled),
[Qt 공식 푸시 예제](https://doc.qt.io/qt-6/qtwebengine-webenginewidgets-push-notifications-example.html),
[Qt 알림의 공개 필드](https://doc.qt.io/qt-6/qwebenginenotification.html),
[Notifications 표준](https://notifications.spec.whatwg.org/#dom-serviceworkerregistration-getnotifications),
[비동기 실행 반환값 제약](https://doc.qt.io/qt-6/qwebenginepage.html#runJavaScript),
[WebChannel의 실행 World](https://doc.qt.io/qt-6/qwebenginepage.html#setWebChannel).
