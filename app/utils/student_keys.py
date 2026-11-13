def normalize_student_key(value: str | None) -> str:
    return " ".join((value or "").strip().casefold().split())


def normalize_roll_no(value: str | None) -> str:
    normalized = normalize_student_key(value)
    if normalized.isascii() and normalized.isdigit():
        return str(int(normalized))
    return normalized
