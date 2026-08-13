"""프로젝트 기술 제약 검증.

README `제약` 절과 이슈 #6·#7의 수용 기준을 자동으로 지킨다. 이후 화면
이슈들이 병렬로 들어와도 이 테스트가 회귀를 막는다.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
import tokenize
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "yt_rec"

#: 파일시스템·외부 프로세스 금지가 걸리는 계층. 화면과 상태뿐이다.
#:
#: 제약의 대상은 `GUI` 다. 녹화 백엔드(``recording/``)는 종료된 파일의 유효성을
#: ``stat`` 으로 확인하고 yt-dlp·ffmpeg 를 직접 띄우는 것이 제 일이므로 여기에
#: 넣지 않는다. 금지된 것은 *화면과 상태 계층*이 파일시스템이나 외부 프로세스를
#: 직접 만지는 일이다.
GUI_ROOTS = (SRC_ROOT / "ui", SRC_ROOT / "state")

#: 진행 중 녹화 표시 경로. 크기·경과 시간을 그리는 모든 모듈.
RECORDING_DISPLAY_MODULES = (
    SRC_ROOT / "ui" / "dashboard.py",
    SRC_ROOT / "ui" / "formatting.py",
    SRC_ROOT / "ui" / "main_window.py",
    SRC_ROOT / "ui" / "widgets.py",
    SRC_ROOT / "state" / "store.py",
    SRC_ROOT / "state" / "models.py",
    SRC_ROOT / "state" / "events.py",
    SRC_ROOT / "state" / "commands.py",
)

#: 파일 크기를 직접 재는 호출. Windows에서 진행 중 파일의 크기가 실제보다
#: 훨씬 작게 나오므로 화면 표시 경로에 있어서는 안 된다.
FORBIDDEN_STAT_ATTRS = frozenset(
    {"stat", "lstat", "getsize", "st_size", "fstat", "stat_result"}
)

FORBIDDEN_STAT_PATTERN = re.compile(
    r"\bos\.stat\b|\bos\.lstat\b|\bos\.fstat\b|\bos\.path\.getsize\b"
    r"|\.stat\(\)|\bgetsize\b|\bst_size\b"
)


def python_sources() -> list[Path]:
    return sorted(SRC_ROOT.rglob("*.py"))


def gui_sources() -> list[Path]:
    """화면·상태 계층의 파이썬 모듈. :data:`GUI_ROOTS` 아래 전부."""
    return sorted(path for root in GUI_ROOTS for path in root.rglob("*.py"))


_LITERAL_TOKENS = {tokenize.COMMENT, tokenize.STRING}
for _name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
    _type = getattr(tokenize, _name, None)
    if _type is not None:
        _LITERAL_TOKENS.add(_type)


def code_text(path: Path) -> str:
    """주석과 문자열 리터럴을 공백으로 지운 코드 본문.

    이 저장소의 docstring 은 `os.stat 을 쓰지 말라`, `QtWebEngine 을 쓰지
    않는다` 같은 금지 문구를 일부러 담고 있다. 설명이 검사에 걸리면 안 되므로
    실제 코드만 남긴다. 줄·열 위치는 그대로 보존한다.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    with path.open("rb") as handle:
        tokens = list(tokenize.tokenize(handle.readline))
    for token in tokens:
        if token.type not in _LITERAL_TOKENS:
            continue
        (start_row, start_col), (end_row, end_col) = token.start, token.end
        for row in range(start_row, end_row + 1):
            if row - 1 >= len(lines):
                continue
            line = lines[row - 1]
            begin = start_col if row == start_row else 0
            finish = end_col if row == end_row else len(line)
            finish = min(finish, len(line))
            begin = min(begin, finish)
            lines[row - 1] = line[:begin] + " " * (finish - begin) + line[finish:]
    return "\n".join(lines)


def test_소스가_비어_있지_않다() -> None:
    assert python_sources(), "src/yt_rec 아래에 파이썬 모듈이 없다"


def test_화면_계층_검사_대상이_비어_있지_않다() -> None:
    """범위를 좁힌 검사가 조용히 `아무것도 안 보는 검사`가 되지 않게 한다.

    경로 오타나 디렉터리 이동으로 :func:`gui_sources` 가 빈 목록을 돌려주면
    아래 금지 검사들이 전부 무조건 통과해 버린다. 표시 경로 모듈이 모두 이
    범위에 들어 있는지까지 확인한다.
    """
    sources = set(gui_sources())
    assert sources, f"{[str(r) for r in GUI_ROOTS]} 아래에 파이썬 모듈이 없다"
    missing = [str(p.relative_to(REPO_ROOT)) for p in RECORDING_DISPLAY_MODULES if p not in sources]
    assert not missing, f"표시 경로 모듈이 검사 범위에서 빠졌다: {missing}"


# ----------------------------------------------------------------------
# GUI는 파일 크기를 stat으로 읽지 않는다 (#7)
# ----------------------------------------------------------------------
@pytest.mark.parametrize("path", RECORDING_DISPLAY_MODULES, ids=lambda p: p.name)
def test_진행_중_녹화_표시_경로에_stat_계열_호출이_없다(path: Path) -> None:
    """`os.stat`, `Path.stat`, `getsize` 로 크기를 산출하지 않아야 한다."""
    assert path.exists(), f"{path} 가 없다"
    source = path.read_text(encoding="utf-8")

    # 1) 문자열 검색 — 이슈 `테스트 방식`이 요구한 형태.
    hits = [
        f"{path.name}:{no}"
        for no, line in enumerate(code_text(path).splitlines(), start=1)
        if FORBIDDEN_STAT_PATTERN.search(line)
    ]
    assert not hits, f"{path.name} 에 stat 계열 호출이 있다: {hits}"

    # 2) AST 검색 — 문자열 검색이 놓치는 형태까지 잡는다.
    tree = ast.parse(source, filename=str(path))
    bad: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_STAT_ATTRS:
            bad.append(f"{path.name}:{node.lineno} .{node.attr}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_STAT_ATTRS:
            bad.append(f"{path.name}:{node.lineno} {node.id}")
    assert not bad, f"stat 계열 접근이 있다: {bad}"


def test_화면과_상태_계층_전체에_stat_계열_호출이_없다() -> None:
    """표시 경로 목록에 아직 없는 새 모듈까지 함께 막는다.

    :data:`RECORDING_DISPLAY_MODULES` 는 손으로 적은 목록이라 화면 이슈가
    모듈을 새로 만들면 빠진다. 계층 전체를 훑어 그 틈을 메운다. 범위는
    :data:`GUI_ROOTS` 까지다 — 녹화 백엔드의 파일 검증은 막지 않는다.
    """
    bad: list[str] = []
    for path in gui_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_STAT_ATTRS:
                bad.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not bad, f"stat 계열 접근: {bad}"


def test_GUI가_파일시스템_모듈을_직접_쓰지_않는다() -> None:
    """상태 계층과 화면 계층은 os / pathlib 를 import 하지 않는다.

    녹화 백엔드는 파일을 만들고 옮기는 것이 일이므로 대상이 아니다.
    """
    offenders: list[str] = []
    for path in gui_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] in {"os", "pathlib", "shutil", "glob"}:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} {name}")
    assert not offenders, f"파일시스템 모듈 import: {offenders}"


# ----------------------------------------------------------------------
# QtWebEngine 금지 (#6, README)
# ----------------------------------------------------------------------
def pyproject() -> dict:
    return tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def runtime_requirements() -> list[str]:
    """배포물에 들어가는 런타임 의존성."""
    return list(pyproject()["project"].get("dependencies", []))


def declared_requirements() -> list[str]:
    """런타임 + 개발 + extra 를 모두 합친 선언 의존성."""
    data = pyproject()
    project = data["project"]
    reqs = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        reqs.extend(extra)
    for group in data.get("dependency-groups", {}).values():
        reqs.extend(item for item in group if isinstance(item, str))
    return reqs


def test_선언된_의존성에_QtWebEngine이_없다() -> None:
    offenders = [r for r in declared_requirements() if "webengine" in r.lower()]
    assert not offenders, f"QtWebEngine 계열 의존성: {offenders}"


def test_PySide6_Addons가_의존성에_없다() -> None:
    """Addons 를 끌어오면 QtWebEngine 계열이 따라온다. Essentials 만 쓴다."""
    offenders = [r for r in declared_requirements() if "pyside6-addons" in r.lower()]
    assert not offenders, f"PySide6-Addons 의존성: {offenders}"
    assert any("pyside6-essentials" in r.lower() for r in declared_requirements())


def test_QtWebEngine_모듈이_설치되어_있지_않다() -> None:
    for name in (
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineQuick",
    ):
        assert importlib.util.find_spec(name) is None, f"{name} 이 설치돼 있다"


def pyside6_root() -> Path:
    spec = importlib.util.find_spec("PySide6")
    assert spec is not None and spec.submodule_search_locations
    return Path(list(spec.submodule_search_locations)[0])


def test_Chromium_라이브러리가_설치되어_있지_않다() -> None:
    """QtWebEngine 의 실체인 Chromium(``Qt6WebEngineCore``)이 없어야 한다.

    배포 요건과 충돌하는 것은 Chromium 그 자체다. PySide6-Essentials 는
    Qt 전체 API 의 타입 스텁(``.pyi``)과 Designer 폼 편집기용 플러그인
    (``plugins/designer/qwebengineview.dll``, 48 KB)을 함께 넣지만, 둘 다
    Chromium 을 포함하지 않고 앱 실행 경로에도 들어오지 않는다.
    """
    root = pyside6_root()
    chromium = [p.name for p in root.glob("**/Qt6WebEngine*")]
    assert not chromium, f"Chromium 라이브러리가 설치돼 있다: {chromium}"

    extensions = [p.name for p in root.glob("QtWebEngine*.pyd")]
    extensions += [p.name for p in root.glob("QtWebEngine*.so")]
    assert not extensions, f"QtWebEngine 확장 모듈이 설치돼 있다: {extensions}"


def test_WebEngine_관련_설치_용량이_무시할_수준이다() -> None:
    """진짜 Chromium 이 들어오면 수백 MB 가 된다. 상한으로 걸러 낸다.

    여기서 ``stat`` 을 쓰는 것은 금지 대상이 아니다. 금지된 것은 *진행 중
    녹화의 크기*를 stat 으로 읽는 일이고, 이것은 이미 설치가 끝난 wheel 의
    디스크 용량을 재는 검사 코드다.
    """
    root = pyside6_root()
    total = sum(p.stat().st_size for p in root.glob("**/*") if "webengine" in p.name.lower())
    assert total < 1_000_000, f"WebEngine 관련 파일이 {total:,} 바이트다"


def test_소스가_QtWebEngine을_참조하지_않는다() -> None:
    """docstring 의 금지 문구는 제외하고 실제 코드만 본다."""
    offenders = [
        str(path.relative_to(REPO_ROOT))
        for path in python_sources()
        if "WebEngine" in code_text(path)
    ]
    assert not offenders, f"QtWebEngine 참조: {offenders}"


# ----------------------------------------------------------------------
# QML 금지 (#6, README)
# ----------------------------------------------------------------------
def test_QML을_쓰지_않는다() -> None:
    """Qt Widgets 만 쓴다. QML/QtQuick 모듈 import 도 .qml 파일도 없어야 한다."""
    forbidden = {"PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets"}
    offenders: list[str] = []
    for path in python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name in forbidden for name in names):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert not offenders, f"QML 계열 import: {offenders}"
    assert not list(SRC_ROOT.rglob("*.qml")), "QML 파일이 있다"


# ----------------------------------------------------------------------
# GUI가 백엔드를 폴링하지 않는다 (#7)
# ----------------------------------------------------------------------
def test_GUI가_외부_프로세스를_직접_돌리지_않는다() -> None:
    """화면·상태 계층은 yt-dlp·ffmpeg·HTTP 를 직접 부르지 않는다.

    호출은 녹화 백엔드가 하고, 결과는 이벤트로 올라온다.
    """
    offenders: list[str] = []
    for path in gui_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name.split(".")[0] in {"subprocess", "requests", "urllib", "http"}:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} {name}")
    assert not offenders, f"GUI 계층이 직접 외부를 호출한다: {offenders}"


def test_상태_저장소는_타이머로_백엔드를_조회하지_않는다() -> None:
    """저장소의 타이머는 방출을 묶는 용도 하나뿐이다."""
    source = (SRC_ROOT / "state" / "store.py").read_text(encoding="utf-8")
    assert source.count("QTimer(") == 1


# ----------------------------------------------------------------------
# 화면 이슈(#8~#12)가 읽어야 하는 계약이 README 에 적혀 있다
# ----------------------------------------------------------------------
def readme() -> str:
    return (REPO_ROOT / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "phrase",
    [
        # 스레드 계약
        "post_event",
        "RuntimeError",
        # 시간대 계약
        "to_local",
        "NaiveDatetimeWarning",
        # 명령 경로
        "yt_rec.state.commands",
        "send_command",
        "stop_recording",
        "set_watched_channels",
        "update_settings",
        "command_rejected",
    ],
)
def test_README에_계약이_적혀_있다(phrase: str) -> None:
    """다섯 화면이 병렬로 올라온다. 계약이 코드에만 있으면 각자 발명한다.

    독스트링에 있는 것과 `무엇을 읽어야 하는지 아는 것`은 다르다.
    """
    assert phrase in readme(), f"README 에 `{phrase}` 설명이 없다"


def test_최소_크기를_다시_잡는_곳이_생성과_표시_두_곳뿐이다() -> None:
    """표시 내용이 바뀔 때 창 최소 크기를 다시 계산하면 창이 스스로 넓어진다.

    실측 회귀: ``_refresh_badge`` 가 watch 변경마다 ``_apply_minimum_size()`` 를
    다시 돌려서, 백엔드가 죽은 상태(376px)로 저장하고 재실행 후 연결되면 398px
    로 튀어 **복원된 창 크기가 무효화됐다**(#6 수용 기준 위반).

    픽셀로 확인하려면 그때그때 어느 구성 요소가 최소 너비를 결정하는지에 따라
    결과가 달라진다(글꼴·로케일 의존). 그래서 `언제 다시 잡는가` 를 직접 본다.
    """
    path = SRC_ROOT / "ui" / "main_window.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    callers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "_apply_minimum_size"
            ):
                callers.add(node.name)

    assert callers == {"__init__", "showEvent"}, (
        f"_apply_minimum_size() 를 부르는 곳이 {sorted(callers)} 다. "
        "표시 내용이 바뀔 때 다시 부르면 창이 스스로 넓어진다"
    )


def test_화면이_백엔드에_닿는_경로가_저장소_하나다() -> None:
    """화면 계층이 명령 데이터 클래스를 직접 만들어 시그널에 실지 않는다.

    ``AppState`` 의 도우미(``stop_recording`` 등)만 쓰면 거부 처리와 스레드
    확인이 한 곳에 모인다. 화면이 ``command_requested.emit`` 을 직접 부르면
    그 검사를 우회한다.
    """
    offenders: list[str] = []
    for path in sorted((SRC_ROOT / "ui").rglob("*.py")):
        body = code_text(path)
        for no, line in enumerate(body.splitlines(), start=1):
            if "command_requested.emit" in line:
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{no}")
    assert not offenders, f"화면이 명령 시그널을 직접 방출한다: {offenders}"


def test_진입점이_존재한다() -> None:
    assert (SRC_ROOT / "app.py").exists()
    assert (SRC_ROOT / "__main__.py").exists()
    scripts = pyproject()["project"].get("scripts", {})
    assert scripts.get("yt-rec") == "yt_rec.app:main"


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib 필요")
def test_requires_python이_유지된다() -> None:
    assert pyproject()["project"]["requires-python"] == ">=3.11"


# ----------------------------------------------------------------------
# uv 기반 환경·의존성 관리 (#16)
# ----------------------------------------------------------------------
LOCK_PATH = REPO_ROOT / "uv.lock"


def lock() -> dict:
    assert LOCK_PATH.exists(), "uv.lock 이 저장소에 없다. 의존성이 고정되지 않는다"
    return tomllib.loads(LOCK_PATH.read_text(encoding="utf-8"))


def locked_packages() -> dict[str, str]:
    return {p["name"]: p.get("version", "") for p in lock().get("package", [])}


def test_잠금_파일이_저장소에_있다() -> None:
    """재현 가능한 의존성이 이 이슈의 핵심이다."""
    assert LOCK_PATH.exists()
    assert locked_packages(), "잠금 파일에 패키지가 하나도 없다"


def test_잠금_파일의_requires_python이_pyproject와_일치한다() -> None:
    assert lock()["requires-python"] == pyproject()["project"]["requires-python"]


def test_잠금_파일에_QtWebEngine_계열이_없다() -> None:
    """잠금 파일까지 확인해야 실제로 설치될 트리를 보증할 수 있다."""
    offenders = [
        name
        for name in locked_packages()
        if "webengine" in name.lower() or "pyside6-addons" in name.lower()
    ]
    assert not offenders, f"잠금 파일에 QtWebEngine 계열이 있다: {offenders}"
    # 원본 텍스트에도 없어야 한다(휠 URL 등에 섞여 들어오는 경우 대비).
    assert "webengine" not in LOCK_PATH.read_text(encoding="utf-8").lower()


def test_잠금_파일이_런타임_의존성을_담는다() -> None:
    packages = locked_packages()
    assert "pyside6-essentials" in packages
    assert "yt-rec" in packages


def test_개발_의존성이_런타임과_분리되어_있다() -> None:
    """배포물에 pytest 가 섞이면 안 된다.

    ``[dependency-groups]`` 로 분리하면 ``uv sync`` 는 개발용까지 넣고
    ``uv sync --no-dev`` 는 런타임만 넣는다.
    """
    data = pyproject()
    groups = data.get("dependency-groups", {})
    assert "dev" in groups, "개발 의존성 그룹이 정의되지 않았다"
    assert any("pytest" in item for item in groups["dev"])

    runtime = " ".join(runtime_requirements()).lower()
    assert "pytest" not in runtime, "pytest 가 런타임 의존성에 들어 있다"

    # 잠금 파일도 같은 구분을 유지한다.
    project_entry = next(p for p in lock()["package"] if p["name"] == "yt-rec")
    runtime_names = {d["name"] for d in project_entry.get("dependencies", [])}
    assert "pytest" not in runtime_names
    assert "pyside6-essentials" in runtime_names


def test_uv_빌드_백엔드를_쓴다() -> None:
    """환경·의존성·빌드를 uv 하나로 통일한다(setuptools 설정 잔재 없음)."""
    data = pyproject()
    assert data["build-system"]["build-backend"] == "uv_build"
    assert "setuptools" not in data.get("tool", {})
    assert "setuptools" not in " ".join(data["build-system"]["requires"]).lower()


def test_가상환경이_추적되지_않는다() -> None:
    """``.gitignore`` 에 규칙이 있는지가 아니라 실제로 걸리는지 확인한다."""
    result = subprocess.run(
        ["git", "check-ignore", "-v", ".venv"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, ".venv 가 .gitignore 에 걸리지 않는다"
    assert ".venv" in result.stdout


def test_잠금_파일은_추적_대상이다() -> None:
    """잠금 파일이 실수로 무시되면 재현성이 사라진다."""
    result = subprocess.run(
        ["git", "check-ignore", "-v", "uv.lock"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, f"uv.lock 이 무시된다: {result.stdout}"
