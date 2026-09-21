from app.services.formatting import escape, split_message


def test_escape_html():
    assert escape("<tag> &") == "&lt;tag&gt; &amp;"


def test_split_message_prefers_boundaries():
    text = ("A" * 70) + "\n\n" + ("B" * 70)
    chunks = split_message(text, 100)
    assert chunks == ["A" * 70, "B" * 70]


def test_empty_message():
    assert split_message("   ") == []
