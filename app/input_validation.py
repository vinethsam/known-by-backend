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


def validate_input_text(value: str, *, max_length: int, multiline: bool = False) -> None:
    if len(value) > max_length:
        raise ValueError("Input text exceeds the maximum length")
    allowed_controls = "\t\n\r" if multiline else ""
    if any(
        (unicodedata.category(char) in {"Cc", "Cs"} and char not in allowed_controls)
        or char in "\ufffe\uffff"
        for char in value
    ):
        raise ValueError("Input text contains unsupported control characters")


def normalized_input_text(value: str, *, max_length: int) -> str:
    value = repair_mojibake(value)
    validate_input_text(value, max_length=max_length)
    return value
