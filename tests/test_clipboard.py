from yt_rec.clipboard import youtube_video_id


def test_watch_live_and_short_link():
    assert youtube_video_id("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert youtube_video_id("https://youtu.be/dQw4w9WgXcQ?t=3") == "dQw4w9WgXcQ"
    assert youtube_video_id("앞 https://m.youtube.com/live/abcdefghijk 뒤") == "abcdefghijk"


def test_ignores_shorts_channel_and_blank():
    assert youtube_video_id("https://www.youtube.com/shorts/dQw4w9WgXcQ") is None
    assert youtube_video_id("https://www.youtube.com/@channel") is None
    assert youtube_video_id("") is None
    assert youtube_video_id("그냥 메모") is None
