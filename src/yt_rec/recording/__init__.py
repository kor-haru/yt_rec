"""녹화 엔진 공개 인터페이스.

전형적인 사용:

.. code-block:: python

    from pathlib import Path
    from yt_rec.recording import RecordingEngine, RecordingOptions, load_settings

    options = load_settings(default=RecordingOptions(output_dir=Path("D:/녹화")))
    engine = RecordingEngine(options, on_event=print)

    result = engine.record("dQw4w9WgXcQ")   # 방송이 끝날 때까지 블로킹
    print(result.status, result.output_path)

GUI 는 :meth:`RecordingEngine.record` 를 워커 스레드에서 부르고,
:meth:`RecordingEngine.add_listener` 로 받은 사건을 Qt 시그널로 옮긴다.
중단은 :meth:`RecordingEngine.request_stop`, 재시작 후 복구는
:meth:`RecordingEngine.recover_pending`.
"""

from .binaries import BinaryNotFoundError, Toolchain, find_executable, resolve_toolchain
from .engine import RecordingEngine
from .errors import (
    DenialCategory,
    MetadataUnavailableError,
    RecordingError,
    ToolFailure,
    ToolTimeout,
    classify_error,
    is_transient,
)
from .events import (
    FragmentRetried,
    FragmentSkipped,
    LogLine,
    MetadataReady,
    ProgressReported,
    RecordingEvent,
    RecordingFinished,
    RecordingResult,
    RecordingStatus,
    StallDetected,
    StatusChanged,
)
from .merge import (
    MediaVerification,
    SourceSelection,
    find_intermediates,
    merge_streams,
    select_merge_sources,
    verify_media,
)
from .metadata import LiveMetadata, fetch_metadata
from .naming import FORBIDDEN_CHAR_MAP, local_date_from_epoch, sanitize_filename_component
from .options import (
    QUALITY_PRESETS,
    RecordingOptions,
    default_settings_path,
    load_settings,
    save_settings,
)
from .progress import ProgressSnapshot, StallDetector

__all__ = [
    "QUALITY_PRESETS",
    "BinaryNotFoundError",
    "DenialCategory",
    "FORBIDDEN_CHAR_MAP",
    "FragmentRetried",
    "FragmentSkipped",
    "LiveMetadata",
    "LogLine",
    "MediaVerification",
    "MetadataReady",
    "MetadataUnavailableError",
    "ProgressReported",
    "ProgressSnapshot",
    "RecordingEngine",
    "RecordingError",
    "RecordingEvent",
    "RecordingFinished",
    "RecordingOptions",
    "RecordingResult",
    "RecordingStatus",
    "SourceSelection",
    "StallDetected",
    "StallDetector",
    "StatusChanged",
    "ToolFailure",
    "ToolTimeout",
    "Toolchain",
    "classify_error",
    "default_settings_path",
    "fetch_metadata",
    "find_executable",
    "find_intermediates",
    "is_transient",
    "load_settings",
    "local_date_from_epoch",
    "merge_streams",
    "resolve_toolchain",
    "sanitize_filename_component",
    "save_settings",
    "select_merge_sources",
    "verify_media",
]
