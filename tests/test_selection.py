from __future__ import annotations

from yt_rec.backend.selection import FileSelectionStore, MemorySelectionStore


def test_메모리_선택은_중복을_제거한다() -> None:
    store = MemorySelectionStore(["UC1", "UC1", "UC2"])
    assert store.load() == ("UC1", "UC2")
    store.save(["UC3", "UC3"])
    assert store.load() == ("UC3",)


def test_파일_선택은_재실행_후에도_남는다(tmp_path) -> None:
    path = tmp_path / "watched_channels.json"
    store = FileSelectionStore(path)
    assert store.load() == ()
    store.save(["UC1", "UC2"])
    again = FileSelectionStore(path)
    assert again.load() == ("UC1", "UC2")
