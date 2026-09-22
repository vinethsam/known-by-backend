"""Small shared input bounds; preserve Unicode and original spreadsheet text."""

import unicodedata

MAX_INPUT_CELL_CHARS = 32767
MAX_INPUT_HEADER_CHARS = 200
MAX_INPUT_FILENAME_CHARS = 1024
MAX_INPUT_URL_CHARS = 4096


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
    value = unicodedata.normalize("NFC", value)
    validate_input_text(value, max_length=max_length)
    return value
