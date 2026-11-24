"""Match configured wake phrases, preserving the original command suffix.

Chinese same-tone homophones are equivalent only inside the wake prefix.
No fuzzy English matching or replacement of words in the user's command.
"""
import re
from functools import lru_cache

from pypinyin import Style, lazy_pinyin

SEPARATORS = " \t\r\n,，。.!！?？:：、"


@lru_cache(maxsize=256)
def pronunciation(text):
    return tuple(lazy_pinyin(text, style=Style.TONE3, neutral_tone_with_five=True))


def compact_prefix(text, count=None):
    chars, ends = [], []
    for index, char in enumerate(text):
        if char in SEPARATORS:
            continue
        chars.append(char.casefold())
        ends.append(index + 1)
        if count and len(chars) == count:
            break
    return "".join(chars), ends


def match_wake(text, words):
    stripped = text.strip()
    # Prefer a complete longer phrase when users configure both names/forms.
    for word in sorted(words, key=lambda w: len(compact_prefix(w)[0]), reverse=True):
        configured = compact_prefix(word)[0]
        if not configured:
            continue
        # English names remain exact with a word boundary (Muse != museum/MISS).
        if not re.fullmatch(r"[\u3400-\u9fff]+", configured):
            boundary = r"(?![A-Za-z0-9_])" if word.isascii() and word[-1:].isalnum() else ""
            matched = re.match(re.escape(word) + boundary, stripped, re.IGNORECASE)
            if matched:
                return word, stripped[matched.end():].lstrip(SEPARATORS)
            continue
        prefix, ends = compact_prefix(stripped, len(configured))
        if len(prefix) != len(configured) or not re.fullmatch(r"[\u3400-\u9fff]+", prefix):
            continue
        if prefix == configured or pronunciation(prefix) == pronunciation(configured):
            return word, stripped[ends[-1]:].lstrip(SEPARATORS)
    return None, stripped
