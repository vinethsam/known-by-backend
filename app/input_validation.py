"""Small shared input bounds; preserve Unicode and original spreadsheet text."""

import unicodedata

MAX_INPUT_CELL_CHARS = 32767
MAX_INPUT_HEADER_CHARS = 200
MAX_INPUT_FILENAME_CHARS = 1024
MAX_INPUT_URL_CHARS = 4096


_MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "â€™", "â€œ", "â€", "ðŸ", "ï»¿", "�")


def repair_mojibake(value: str) -> str:
    """Repair a narrow UTF-8-as-Latin-1/CP1252 failure without changing valid Unicode."""

    repaired = value
    for _ in range(2):
        current_score = sum(repaired.count(marker) for marker in _MOJIBAKE_MARKERS)
        if current_score == 0:
            break
        candidates = []
        for encoding in ("latin-1", "cp1252"):
            try:
                candidate = repaired.encode(encoding).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            candidate_score = sum(candidate.count(marker) for marker in _MOJIBAKE_MARKERS)
            if candidate_score < current_score and candidate.count("�") <= repaired.count("�"):
                candidates.append((candidate_score, candidate))
        if not candidates:
            break
        repaired = min(candidates, key=lambda item: (item[0], len(item[1]), item[1]))[1]
    return unicodedata.normalize("NFC", repaired)


def validate_input_text(
    value: str, *, max_length: int, multiline: bool = False, allow_controls: bool = False
) -> None:
    if len(value) > max_length:
        raise ValueError("Input text exceeds the maximum length")
    allowed_controls = "\t\n\r" if multiline else ""
    if any(
        unicodedata.category(char) == "Cs"
        or (unicodedata.category(char) == "Cc" and not allow_controls and char not in allowed_controls)
        or char in "\ufffe\uffff"
        for char in value
    ):
        raise ValueError("Input text contains unsupported control characters")


def sanitize_input_text(value: str, *, multiline: bool = False) -> str:
    """Repair encoding before removing controls from processing text.

    For example, UTF-8 Á misdecoded as Latin-1 contains U+0081; removing that
    control first would destroy the accent. Raw input is retained separately in
    JSON metadata. Do not remove Unicode format characters (including joiners),
    collapse ordinary spaces, or guess replacements for invalid Unicode scalars.
    """
    value = repair_mojibake(value)
    cleaned = []
    for char in value:
        if char in "\t\n\r":
            cleaned.append(char if multiline else " ")
        elif char in "\v\f\x85":
            cleaned.append("\n" if multiline else " ")
        elif unicodedata.category(char) != "Cc":
            cleaned.append(char)
    result = "".join(cleaned)
    validate_input_text(result, max_length=len(result), multiline=multiline)
    return result


def normalized_input_text(value: str, *, max_length: int) -> str:
    value = sanitize_input_text(value)
    validate_input_text(value, max_length=max_length)
    return value
