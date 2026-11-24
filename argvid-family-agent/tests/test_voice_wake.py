"""Wake-prefix regression tests, including the real 小芯 -> 小心 failure."""
import pytest

from cloud_loop.harness_voice import VoiceGateway
from cloud_loop.voice_wake import match_wake


@pytest.mark.parametrize("word,transcript,question", [
    ("小芯", "小心现在有多少视频", "现在有多少视频"),
    ("小芯", "小新，现在有多少视频？", "现在有多少视频？"),
    ("你好 小芯", "你好，小心，空间呢？", "空间呢？"),
    ("你好小芯", "你 好 小 新现在几点？", "现在几点？"),
    ("小鹿", "小路，请小心处理录像。", "请小心处理录像。"),
    ("小牧", "小木现在几点", "现在几点"),
    ("muse", "Muse，现在几点", "现在几点"),
])
def test_same_pronunciation_is_configurable_and_does_not_rewrite_question(word, transcript, question):
    assert match_wake(transcript, [word]) == (word, question)
    assert VoiceGateway.match(transcript, [word]) == (word, question)


@pytest.mark.parametrize("transcript", [
    "注意小心，现在有多少视频",  # not at the beginning
    "小星，现在有多少视频",  # different syllable
    "小信，现在有多少视频",  # different tone
    "现在有多少视频",
    "MISS现在几点",
    "museum 现在几点",
])
def test_unconfigured_or_merely_similar_sound_does_not_wake(transcript):
    assert match_wake(transcript, ["小芯", "muse"])[0] is None


def test_longest_phrase_wins_and_original_suffix_is_preserved():
    assert match_wake("你好，小心，请小心处理。", ["你好", "你好小芯"]) == (
        "你好小芯", "请小心处理。")
    assert match_wake("小心", ["小芯"]) == ("小芯", "")
