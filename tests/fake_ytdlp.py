"""테스트용 가짜 yt-dlp.

실제 네트워크 없이 엔진의 마무리 경로(정지 판정, 중간 파일 병합, 파일명 결정)를
돌려 보기 위한 것이다. 계획 파일(JSON)에 적힌 대로 출력을 찍고 파일을 남긴다.

사용법: ``python fake_ytdlp.py <plan.json> <진짜 yt-dlp 인자들...>``

계획 파일 항목:
    argv_out    받은 인자를 JSON 으로 적어 둘 경로 (선택)
    lines       표준출력에 찍을 줄 목록. 숫자를 넣으면 그만큼(초) 쉰다.
    files       {만들 파일 이름: 복사해 올 원본 경로}
    sleep       모든 줄을 찍은 뒤 잠들 시간(초). 정지 판정 시험용.
    exit_code   종료 코드
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path


def main() -> int:
    plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    argv = sys.argv[2:]

    if plan.get("argv_out"):
        Path(plan["argv_out"]).write_text(
            json.dumps(argv, ensure_ascii=False), encoding="utf-8"
        )

    for name, source in (plan.get("files") or {}).items():
        shutil.copyfile(source, Path.cwd() / name)

    for line in plan.get("lines") or []:
        if isinstance(line, (int, float)):
            time.sleep(float(line))
            continue
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    sleep = float(plan.get("sleep") or 0)
    if sleep:
        # 진행이 멈춘 상태. 바깥에서 정지로 판정하고 끊어야 한다.
        time.sleep(sleep)

    return int(plan.get("exit_code") or 0)


if __name__ == "__main__":
    raise SystemExit(main())
