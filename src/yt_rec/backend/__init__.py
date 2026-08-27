"""감시·인증·녹화 백엔드.

화면은 이 패키지를 import 하지 않는다. :class:`~yt_rec.state.store.AppState` 의
이벤트와 명령만 본다. 이 패키지가 그 반대편이다.
"""

from .source import BackendSource, create_backend_source

__all__ = ["BackendSource", "create_backend_source"]
