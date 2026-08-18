# 녹화 엔진 수동 검증 절차

자동화된 검증은 `pytest` 로 돌린다(`pip install -e ".[dev]"`). 여기 적은 항목은
**실제 라이브 방송이 있어야만** 확인할 수 있어서 자동화하지 않은 것들이다.

자동 테스트가 이미 덮는 것은 여기서 다시 확인하지 않는다.
스텁 서버로 조각을 404 로 만드는 재시도 상한 검증은 `tests/test_retry_limit.py`
가 실제 yt-dlp 를 돌려 확인한다.

## 준비

```bash
pip install -e ".[dev]"
yt-dlp --version   # PATH 에 있어야 한다
ffmpeg -version
```

수동 검증에는 엔진에 딸린 최소 명령줄을 쓴다.

```bash
python -m yt_rec.recording record <VIDEO_ID> -o "D:/녹화" --max-height 1080
python -m yt_rec.recording verify "D:/녹화/2026-08-11_제목.mp4"
python -m yt_rec.recording recover -o "D:/녹화"
```

---

## 1. 방송 시작 지점부터 확보되는지 (#4)

진행 중인 라이브를, 시작한 지 **최소 30분이 지난 시점에** 붙잡아 녹화한다.

1. `python -m yt_rec.recording record <VIDEO_ID> -o <출력 디렉터리>`
2. 방송이 끝날 때까지 둔다.
3. 결과 파일을 열어 **방송 시작 장면부터** 담겨 있는지 본다.
4. 재생 길이가 실제 방송 길이와 비슷한지 본다(붙잡은 시점부터가 아니라).

이 항목이 실패하면 `--live-from-start` 가 빠졌거나 DVR 백로그가 없는 방송이다.

## 2. 화질 상한이 실제 송출과 맞물리는지 (#4)

| 상황 | 설정 | 기대 |
|---|---|---|
| 1080p 송출 | `--max-height 1080` | 1080p 로 저장 |
| 1080p 송출 | `--max-height 720` | 720p 로 저장 |
| 720p 만 송출 | `--max-height 1080` | 실패하지 않고 720p 로 저장 |

확인:

```bash
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,codec_name \
        -of default=nw=1 "결과 파일"
```

세 번째 줄이 이 이슈의 핵심이다. 라이브가 설정보다 낮은 화질로만 송출될 때
**오류 없이** 제공되는 최고 화질로 저장돼야 한다.

## 3. 방송 종료 직후 멤버 전용 전환 (#14)

실측으로 관측된 상황이다. 종료 후 영상이 멤버 전용으로 바뀌면 제목을 더는
조회할 수 없다(`Join this channel to get access to members-only content`).

재현하려면 그런 채널의 방송을 녹화해야 해서 자동화하지 않았다. 대신 다음으로
같은 결론에 이를 수 있다.

1. 녹화를 시작한다.
2. 시작 직후 `<출력 디렉터리>/.yt-rec/<VIDEO_ID>/metadata.json` 이 생겼는지 본다.
   제목·채널·`release_timestamp` 가 들어 있어야 한다. **이 파일이 곧 요건이다.**
3. 방송이 끝난 뒤 저장된 파일 이름이 그 JSON 의 제목과 맞는지 본다.
4. 네트워크를 끊은 상태에서 `python -m yt_rec.recording recover -o <출력 디렉터리>`
   를 돌려도 이름이 제대로 정해지는지 본다(조회 없이 보관된 값만 쓰는지 확인).

## 4. 심야 방송의 날짜 (#14)

00:00 ~ 09:00 (KST) 사이에 시작한 방송을 녹화한다. UTC 로는 전날이다.

- 파일명 날짜가 **로컬 날짜**여야 한다.
- 대조: `metadata.json` 의 `release_timestamp` 를 UTC 로 변환한 날짜와 하루 차이가 난다.

```bash
python -c "import json,datetime as d; m=json.load(open('metadata.json',encoding='utf-8')); t=m['release_timestamp']; print('UTC ', d.datetime.fromtimestamp(t, d.timezone.utc).date()); print('로컬', d.datetime.fromtimestamp(t).date())"
```

시간대를 바꿔 가며 확인하려면 Windows 에서 시스템 시간대를 UTC 로 바꾼 뒤
같은 파일로 `recover` 를 돌려 이름이 달라지는지 보면 된다.

## 5. 긴 녹화의 무결성 (#14)

1시간 이상 녹화한 결과에 대해 다섯 지표를 모두 확인한다. `verify` 명령이 한 번에
전부 계산한다.

```bash
python -m yt_rec.recording verify "결과 파일"
```

직접 확인하려면:

```bash
# 1) 컨테이너 데먹싱 오류 0건 — 아무것도 출력되지 않아야 한다
ffmpeg -v error -i "결과 파일" -c copy -f null -

# 2) 프레임 수 = 재생 길이 x 프레임률
ffprobe -v error -select_streams v:0 -count_frames \
        -show_entries stream=nb_read_frames,avg_frame_rate,duration -of default=nw=1 "결과 파일"

# 3) 역행 타임스탬프 0건 / 4) 최대 프레임 간격 1프레임 이내
ffprobe -v error -select_streams v:0 -show_entries packet=pts_time,dts_time \
        -of csv=p=0 "결과 파일" > packets.csv

# 5) 영상과 음성 길이 차이 — 조각 하나(+여유, 구현 임계 6초) 이내
ffprobe -v error -show_entries stream=index,codec_type,duration -of default=nw=1 "결과 파일"
```

## 6. 프로세스 강제 종료 후 복구 (#4, #14)

1. 녹화 중에 작업 관리자로 앱 프로세스를 강제 종료한다.
2. `<출력 디렉터리>/.yt-rec/<VIDEO_ID>/` 에 `<VIDEO_ID>.f*.mp4` 중간 파일이 남아 있다.
3. `python -m yt_rec.recording recover -o <출력 디렉터리>`
4. 병합된 파일이 출력 디렉터리에 생기고, 검증이 **누락 없이 완전**(complete)이면
   중간 파일이 사라진다. 재생은 되지만 누락이 확인된 PARTIAL 에서는 중간 파일을
   남긴다.
5. 검증에 실패했으면 중간 파일이 **그대로 남아 있어야 한다.**

## 7. 긴 방송에서의 진행률 표시 (README 제약)

녹화 중 진행률·크기는 yt-dlp 출력에서 읽는다. `os.stat` 으로 재면 Windows 에서
쓰기 핸들이 열린 파일의 크기가 디렉터리 엔트리에 반영되지 않아 실제보다 훨씬
작게 보인다.

- 녹화 중 탐색기에서 본 중간 파일 크기와, 엔진이 보고하는 크기를 비교한다.
- 탐색기 쪽이 작게 나오는 것이 정상이다. 엔진 쪽 숫자가 계속 늘어나야 한다.
