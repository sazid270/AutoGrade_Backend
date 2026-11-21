import re
import unicodedata

ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)
BENGALI_DIGITS = str.maketrans("০১২৩৪৫৬৭৮৯", "0123456789")
BENGALI_LABELS = {
    "ক": "A",
    "খ": "B",
    "গ": "C",
    "ঘ": "D",
    "ঙ": "E",
    "চ": "F",
    "ছ": "G",
    "জ": "H",
    "ঝ": "I",
    "ঞ": "J",
}
OPTION_LETTERS = set("ABCDEFGHIJ")
LABEL_WORDS = {"শাখা", "সেট", "SECTION", "SEC", "SET"}
TRANSLITERATED_LABELS = {
    "KA": "A",
    "KHA": "B",
    "GA": "C",
    "GHA": "D",
    "NGA": "E",
    "CA": "F",
    "CHA": "F",
    "CHHA": "G",
    "JA": "H",
    "JHA": "I",
    "NYA": "J",
}


def normalize_option_label(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFC", value).translate(ZERO_WIDTH)
    text = re.sub(r"[\s()\[\]{}.,:;|\-–—_/'\"।]+", " ", text)
    tokens = [token for token in text.split() if token.upper() not in LABEL_WORDS]
    if len(tokens) != 1:
        return None
    label = tokens[0].rstrip("\u09cd")
    label = BENGALI_LABELS.get(label, label.upper())
    label = TRANSLITERATED_LABELS.get(label, label)
    return label if label in OPTION_LETTERS else None


def normalize_options(values) -> tuple[list[str], bool]:
    letters: set[str] = set()
    bad = False
    for value in values or []:
        letter = normalize_option_label(value)
        if letter is None:
            bad = True
        else:
            letters.add(letter)
    return sorted(letters), bad
