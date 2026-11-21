import re
import unicodedata

from app.utils.options import BENGALI_DIGITS, ZERO_WIDTH, normalize_option_label

CLASS_WORDS = {
    unicodedata.normalize("NFC", key): value
    for key, value in {
        "প্রথম": "1",
        "দ্বিতীয়": "2",
        "তৃতীয়": "3",
        "চতুর্থ": "4",
        "পঞ্চম": "5",
        "ষষ্ঠ": "6",
        "সপ্তম": "7",
        "অষ্টম": "8",
        "নবম": "9",
        "দশম": "10",
        "একাদশ": "11",
        "দ্বাদশ": "12",
        "first": "1",
        "second": "2",
        "third": "3",
        "fourth": "4",
        "fifth": "5",
        "sixth": "6",
        "seventh": "7",
        "eighth": "8",
        "ninth": "9",
        "tenth": "10",
        "eleventh": "11",
        "twelfth": "12",
        "one": "1",
        "two": "2",
        "three": "3",
        "four": "4",
        "five": "5",
        "six": "6",
        "seven": "7",
        "eight": "8",
        "nine": "9",
        "ten": "10",
        "eleven": "11",
        "twelve": "12",
        "i": "1",
        "ii": "2",
        "iii": "3",
        "iv": "4",
        "v": "5",
        "vi": "6",
        "vii": "7",
        "viii": "8",
        "ix": "9",
        "x": "10",
        "xi": "11",
        "xii": "12",
    }.items()
}
CLASS_NOISE = {
    unicodedata.normalize("NFC", word)
    for word in (
        "শ্রেণি",
        "শ্রেণী",
        "শ্রেনী",
        "class",
        "std",
        "standard",
    )
}


def normalize_student_key(value: str | None) -> str:
    return " ".join((value or "").strip().casefold().split())


def normalize_roll_no(value: str | None) -> str:
    normalized = normalize_student_key(value)
    if normalized.isascii() and normalized.isdigit():
        return str(int(normalized))
    return normalized


def normalize_section(value: str | None) -> str:
    normalized = normalize_option_label(value)
    return normalize_student_key(normalized or value)


def normalize_class_name(value: str | None) -> str:
    text = (
        unicodedata.normalize("NFC", normalize_student_key(value))
        .translate(BENGALI_DIGITS)
        .translate(ZERO_WIDTH)
    )
    tokens = [
        token
        for token in re.split(r"[\s\-_.,:;()/]+", text)
        if token and unicodedata.normalize("NFC", token) not in CLASS_NOISE
    ]
    if len(tokens) == 1:
        token = tokens[0]
        match = re.fullmatch(r"(\d+)(?:st|nd|rd|th|ম|র্থ|ষ্ঠ)?", token)
        if match:
            return str(int(match.group(1)))
        if token in CLASS_WORDS:
            return CLASS_WORDS[token]
    return " ".join(tokens)
