from datetime import date

from app.processing import document_metadata


def test_explicit_publication_metadata_only():
    title, published = document_metadata(
        '<title>Jane Doe</title><meta property="article:published_time" content="2025-01-15T12:00:00Z">'
    )
    assert title == "Jane Doe" and published == date(2025, 1, 15)
    assert document_metadata('<meta property="article:modified_time" content="2025-01-15">')[1] is None
    assert document_metadata('<meta property="article:published_time" content="2999-01-15">')[1] is None
