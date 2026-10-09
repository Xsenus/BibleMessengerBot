"""Raw MAX limits preserve complete Scripture, entities and page-specific speech."""
from app.services.formatting import plain_text, split_wire_message
from app.services.message_languages import pages


def test_long_chapter_keeps_every_character_and_balanced_formatting():
    text = '<b>Chapter 1</b>\n\n'+''.join(f'<b>[{n}]</b> '+'Word &amp; &lt;quote&gt; 😀 '*45+'\n\n' for n in range(1,70))
    pieces = pages(text,'max')
    assert len(pieces)>10
    assert all(len(piece)<=3600 for piece in pieces)
    assert ''.join(plain_text(piece) for piece in pieces)==plain_text(text)
    assert '[69]' in plain_text(pieces[-1])


def test_max_counts_reopened_markup_and_entities_not_only_visible_length():
    text='<a href="https://example.test/source">'+('&amp;&lt;'*900)+'</a>'
    result=split_wire_message(text,4000)
    assert len(result)>1
    assert all(len(part)<=4000 for part in result)
    assert ''.join(plain_text(p) for p in result)==plain_text(text)


def test_telegram_rich_card_retains_existing_page_threshold():
    text='<b>Chapter</b>\n'+('Visible text '*500)
    assert pages(text)==[text]
    assert len(pages(text,'max'))>1
