"""WhatsApp export parser (#11). Synthetic chats and names only."""

import io
import zipfile
from datetime import datetime

import pytest

from app.services.whatsapp import (
    PANAMA_TZ,
    WaExportError,
    dedupe_hash,
    media_kind,
    parse_chat_text,
    parse_export,
)
from app.services.whatsapp import export as wa_export
from scripts import wa_parse

LRM = "\u200e"
NNBSP = "\u202f"
NBSP = "\u00a0"


def _at(*args):
    return datetime(*args, tzinfo=PANAMA_TZ)


ANDROID_ES = "\n".join(
    [
        "12/9/23, 14:05 - Los mensajes y las llamadas están cifrados de extremo a extremo.",
        "12/9/23, 14:05 - Ana Prueba creó el grupo",
        "12/9/23, 14:06 - Ana Prueba: Hola a todos",
        "13/9/23, 9:15 - Beto Ficticio: Resultados del laboratorio",
        "segunda línea",
        "",
        "tercera línea",
        "13/9/23, 9:16 - Beto Ficticio: " + LRM + "LAB-20230913-WA0001.pdf (archivo adjunto)",
        "13/9/23, 9:17 - Beto Ficticio: PTT-20230913-WA0002.opus (archivo adjunto)",
        "13/9/23, 9:18 - \u202a+507 6000-0000\u202c: IMG-20230913-WA0003.jpg (archivo adjunto)",
        "foto de la receta",
        "13/9/23, 9:19 - Ana Prueba: <Multimedia omitido>",
    ]
)


def test_android_spanish_24h():
    result = parse_chat_text(ANDROID_ES)
    assert (result.style, result.date_order, result.clock) == ("dash", "dmy", "24h")
    assert result.unparsed_lines == []
    msgs = result.messages
    assert len(msgs) == 8
    assert msgs[0].is_system and msgs[0].sender is None
    assert msgs[1].is_system and msgs[1].sender is None
    assert msgs[2].sender == "Ana Prueba" and msgs[2].text == "Hola a todos"
    assert msgs[2].sent_at == _at(2023, 9, 12, 14, 6)
    assert str(msgs[2].sent_at.utcoffset()) == "-1 day, 19:00:00"  # UTC-5
    assert msgs[3].text == "Resultados del laboratorio\nsegunda línea\n\ntercera línea"
    assert msgs[3].line_no == 4
    pdf, audio, image, omitted = msgs[4:]
    assert (pdf.media_filename, pdf.media_kind, pdf.text) == ("LAB-20230913-WA0001.pdf", "pdf", "")
    assert (audio.media_kind, audio.media_omitted) == ("audio", False)
    assert image.sender == "+507 6000-0000"
    assert (image.media_kind, image.text) == ("image", "foto de la receta")
    assert omitted.media_omitted and omitted.media_filename is None
    assert omitted.media_kind == "other" and omitted.text == ""


ANDROID_EN_12H = "\n".join(
    [
        f"9/12/23, 2:05{NNBSP}PM - Ana Prueba: hello",
        f"9/12/23, 12:30{NNBSP}AM - Ana Prueba: past midnight",
        f"9/12/23, 12:45{NNBSP}PM - Beto Ficticio: noon",
        f"9/13/2023, 11:59{NNBSP}PM - Beto Ficticio: VID-20230913-WA0004.mp4 (file attached)",
        f"9/14/23, 1:00{NNBSP}AM - Ana Prueba: <Media omitted>",
    ]
)


def test_android_english_12h_month_first():
    result = parse_chat_text(ANDROID_EN_12H)
    assert (result.date_order, result.clock) == ("mdy", "12h")
    stamps = [m.sent_at for m in result.messages]
    assert stamps == [
        _at(2023, 9, 12, 14, 5),
        _at(2023, 9, 12, 0, 30),
        _at(2023, 9, 12, 12, 45),
        _at(2023, 9, 13, 23, 59),
        _at(2023, 9, 14, 1, 0),
    ]
    assert result.messages[3].media_kind == "video"
    assert result.unparsed_lines == []


def test_spanish_am_pm_variants():
    chat = "\n".join(
        [
            f"12/9/23, 2:05{NNBSP}p.{NBSP}m. - Ana Prueba: uno",
            "12/9/23, 9:05 a. m. - Ana Prueba: dos",
            "12/9/23, 12:10 a.m. - Ana Prueba: tres",
            "12/9/23, 3:00 pm - Ana Prueba: cuatro",
        ]
    )
    result = parse_chat_text(chat)
    assert [m.sent_at.hour for m in result.messages] == [14, 9, 0, 15]
    assert [m.text for m in result.messages] == ["uno", "dos", "tres", "cuatro"]


IOS_ES = "\n".join(
    [
        f"{LRM}[12/9/23, 14:05:33] Familia Prueba: {LRM}Los mensajes y las llamadas están cifrados.",
        "[12/9/23, 14:06:01] Ana Prueba: Buenos días",
        "con dos líneas",
        f"[13/9/23, 8:00:00] Beto Ficticio: {LRM}<adjunto: 00000012-PHOTO-2023-09-13-08-00-00.jpg>",
        f"[13/9/23, 8:01:10] Beto Ficticio: {LRM}Informe.pdf • {LRM}2 páginas "
        f"{LRM}<adjunto: 00000013-Informe.pdf>",
        f"[13/9/23, 8:02:00] Ana Prueba: {LRM}<adjunto: 00000014-AUDIO-2023-09-13-08-02-00.opus>",
        f"[13/9/23, 8:03:00] Ana Prueba: {LRM}imagen omitida",
    ]
)


def test_ios_spanish_bracketed():
    result = parse_chat_text(IOS_ES)
    assert (result.style, result.date_order, result.clock) == ("bracketed", "dmy", "24h")
    assert result.unparsed_lines == []
    msgs = result.messages
    assert msgs[0].is_system and msgs[0].sender == "Familia Prueba"
    assert LRM not in msgs[0].text
    assert msgs[1].sent_at == _at(2023, 9, 12, 14, 6, 1)
    assert msgs[1].text == "Buenos días\ncon dos líneas" and not msgs[1].is_system
    assert msgs[2].media_filename == "00000012-PHOTO-2023-09-13-08-00-00.jpg"
    assert msgs[2].media_kind == "image" and not msgs[2].is_system
    assert msgs[3].media_filename == "00000013-Informe.pdf"
    # The document descriptor only repeats the file: not part of the text.
    assert msgs[3].media_kind == "pdf" and msgs[3].text == ""
    assert msgs[4].media_kind == "audio"
    assert msgs[5].media_omitted and msgs[5].media_kind == "image"


def test_ios_english_12h():
    chat = "\n".join(
        [
            f"[9/12/23, 2:05:33{NNBSP}PM] Ana Prueba: hi",
            f"[9/13/23, 8:00:00{NNBSP}AM] Beto Ficticio: {LRM}<attached: 00000020-PHOTO.jpg>",
            f"[9/13/23, 8:01:00{NNBSP}AM] Beto Ficticio: {LRM}audio omitted",
        ]
    )
    result = parse_chat_text(chat)
    assert (result.date_order, result.clock) == ("mdy", "12h")
    assert result.messages[0].sent_at == _at(2023, 9, 12, 14, 5, 33)
    assert result.messages[1].media_filename == "00000020-PHOTO.jpg"
    assert result.messages[2].media_omitted and result.messages[2].media_kind == "audio"


# WhatsApp Web (desktop) export: an undated preamble line, bracketed 12 h
# timestamps with seconds, "<… omitted>" markers, "[forwarded]" prefixes, and
# documents that keep their name after the marker.
WEB = "\n".join(
    [
        "Messages and calls are end-to-end encrypted. Only people in this chat can read them.",
        "[6/17/25, 3:45:12 PM] Ana Prueba: Hola",
        "[6/17/25, 3:46:00 PM] Ana Prueba: <voice message omitted>",
        "[6/17/25, 3:47:00 PM] Beto Ficticio: <image omitted> receta nueva",
        "[6/18/25, 9:00:00 AM] Beto Ficticio: [forwarded] <audio omitted>",
        "[6/18/25, 9:01:00 AM] Beto Ficticio: <document omitted> Lab Prueba 0001.pdf",
        "[6/18/25, 9:02:00 AM] Ana Prueba: [forwarded] texto reenviado",
        "[6/18/25, 9:03:00 AM] Ana Prueba: <video omitted>",
        "[6/18/25, 9:04:00 AM] Ana Prueba: <sticker omitted>",
    ]
)


def test_whatsapp_web_export():
    result = parse_chat_text(WEB)
    assert (result.style, result.date_order, result.clock) == ("bracketed", "mdy", "12h")
    assert result.unparsed_lines == [1]  # the undated preamble
    msgs = result.messages
    assert len(msgs) == 8 and not any(m.is_system for m in msgs)
    assert msgs[0].sent_at == _at(2025, 6, 17, 15, 45, 12)
    assert (msgs[1].media_kind, msgs[1].media_omitted, msgs[1].text) == ("audio", True, "")
    assert (msgs[2].media_kind, msgs[2].text) == ("image", "receta nueva")
    assert msgs[3].forwarded and msgs[3].media_kind == "audio" and msgs[3].text == ""
    assert msgs[4].media_omitted and msgs[4].media_filename == "Lab Prueba 0001.pdf"
    assert msgs[4].media_kind == "pdf" and msgs[4].text == ""
    assert msgs[5].forwarded and msgs[5].text == "texto reenviado" and not msgs[5].media_kind
    assert msgs[6].media_kind == "video" and msgs[7].media_kind == "image"


def test_ambiguous_order_resolved_across_file():
    # The first lines read both ways; a later date only fits day/month...
    chat = "1/2/24, 10:00 - Ana Prueba: a\n3/2/24, 10:00 - Ana Prueba: b\n25/2/24, 10:00 - Ana: c"
    assert parse_chat_text(chat).date_order == "dmy"
    assert parse_chat_text(chat).messages[0].sent_at == _at(2024, 2, 1, 10)
    # ...and here only month/day.
    chat = "1/2/24, 10:00 - Ana Prueba: a\n3/2/24, 10:00 - Ana Prueba: b\n2/25/24, 10:00 - Ana: c"
    assert parse_chat_text(chat).date_order == "mdy"


def test_ambiguous_order_tie_breaks_on_chronology():
    # Every date fits both orders; read as m/d they go backwards in time.
    chat = "\n".join(
        f"{d}/{m}/24, 10:00 - Ana Prueba: x" for d, m in [(5, 1), (6, 1), (1, 2), (2, 2)]
    )
    assert parse_chat_text(chat).date_order == "dmy"
    chat = "\n".join(
        f"{m}/{d}/24, 10:00 - Ana Prueba: x" for d, m in [(5, 1), (6, 1), (1, 2), (2, 2)]
    )
    assert parse_chat_text(chat).date_order == "mdy"
    # Fully ambiguous: day/month (Spanish locale).
    assert parse_chat_text("1/1/24, 10:00 - Ana: x").date_order == "dmy"


def test_ambiguous_order_tie_breaks_on_the_reference_time():
    # A short sync export (WhatsApp Web, US order): every date is 10/8 or
    # 10/9, readable as 8-9 October or 10 August / 10 September, and both
    # go forward in time. The import's time picks October.
    chat = "\n".join(
        [
            "[10/8/26, 7:55:12 PM] Ana Prueba: a",
            "[10/8/26, 11:59:59 PM] Beto Ficticio: b",
            "[10/9/26, 8:01:03 AM] Ana Prueba: c",
            "[10/9/26, 2:51:40 PM] Beto Ficticio: d",
        ]
    )
    reference = _at(2026, 10, 9, 15)
    result = parse_chat_text(chat, reference=reference)
    assert result.date_order == "mdy"
    assert result.messages[0].sent_at.date().isoformat() == "2026-10-08"
    assert result.messages[-1].sent_at.date().isoformat() == "2026-10-09"
    # A day/month file of the same shape (8-9 October) stays day/month.
    chat = chat.replace("10/8/26", "8/10/26").replace("10/9/26", "9/10/26")
    assert parse_chat_text(chat, reference=reference).date_order == "dmy"
    # Without a reference, the old tie-break (day/month) is unchanged.
    assert parse_chat_text("1/1/24, 10:00 - Ana: x").date_order == "dmy"


def _sync_chat(first: str, second: str) -> str:
    """A short WhatsApp Web export over two dates (bracketed, 12 h, seconds)."""
    return "\n".join(
        [
            f"[{first}/26, 7:55:12 PM] Ana Prueba: a",
            f"[{first}/26, 11:59:59 PM] Beto Ficticio: b",
            f"[{second}/26, 8:01:03 AM] Ana Prueba: c",
            f"[{second}/26, 2:51:40 PM] Beto Ficticio: d",
        ]
    )


# (first date, second date, reference, expected order, expected first/last day)
TIE_CASES = [
    # #117: ends on a day whose number equals its month; both readings end
    # on the same day, so the shorter span decides (not 10 Sep - 10 Oct).
    ("10/9", "10/10", (2026, 10, 10, 15), "mdy", "2026-10-09", "2026-10-10"),
    ("11/10", "11/11", (2026, 11, 11, 15), "mdy", "2026-11-10", "2026-11-11"),
    ("12/11", "12/12", (2026, 12, 12, 15), "mdy", "2026-12-11", "2026-12-12"),
    # The same days in a day/month (Spanish) export stay day/month.
    ("9/10", "10/10", (2026, 10, 10, 15), "dmy", "2026-10-09", "2026-10-10"),
    ("10/11", "11/11", (2026, 11, 11, 15), "dmy", "2026-11-10", "2026-11-11"),
    # A single day that reads the same both ways: day/month, same dates.
    ("10/10", "10/10", (2026, 10, 10, 15), "dmy", "2026-10-10", "2026-10-10"),
    # #102: the two readings end on different days; the reference decides.
    ("10/8", "10/9", (2026, 10, 9, 15), "mdy", "2026-10-08", "2026-10-09"),
    ("8/10", "9/10", (2026, 10, 9, 15), "dmy", "2026-10-08", "2026-10-09"),
]


@pytest.mark.parametrize(("first", "second", "reference", "order", "start", "end"), TIE_CASES)
def test_ambiguous_order_tie_table(first, second, reference, order, start, end):
    result = parse_chat_text(_sync_chat(first, second), reference=_at(*reference))
    assert result.date_order == order
    assert result.messages[0].sent_at.date().isoformat() == start
    assert result.messages[-1].sent_at.date().isoformat() == end


def test_long_unambiguous_file_ignores_every_tie_break():
    # A day > 12 decides; neither a hint, a floor nor the reference changes it.
    chat = "\n".join(f"{d}/9/26, 10:00 - Ana Prueba: x{d}" for d in range(1, 29))
    for kwargs in (
        {},
        {"reference": _at(2026, 9, 28, 12)},
        {"date_order": "mdy", "floor": _at(2026, 9, 20, 9), "reference": _at(2026, 9, 28)},
    ):
        result = parse_chat_text(chat, **kwargs)
        assert result.date_order == "dmy"
        assert len(result.messages) == 28
    # The hashes of a decisive file never depend on the tie-break inputs.
    assert [m.dedupe_hash for m in parse_chat_text(chat).messages] == [
        m.dedupe_hash for m in parse_chat_text(chat, date_order="mdy").messages
    ]


def test_date_order_hint_breaks_a_tie_only():
    chat = _sync_chat("10/9", "10/10")
    reference = _at(2026, 10, 10, 15)
    assert parse_chat_text(chat, reference=reference, date_order="dmy").date_order == "dmy"
    assert parse_chat_text(chat, date_order="mdy").date_order == "mdy"
    # An unknown hint is ignored.
    assert parse_chat_text(chat, reference=reference, date_order="xyz").date_order == "mdy"
    # A decisive file wins over the hint.
    decisive = "1/2/24, 10:00 - Ana Prueba: a\n2/25/24, 10:00 - Ana: c"
    assert parse_chat_text(decisive, date_order="dmy").date_order == "mdy"


def test_floor_outranks_the_reference_and_the_span():
    # A 10/8 - 10/9 export processed with a reference of 11 Sep: October
    # would be "the future", so the reference alone picks 10 Aug / 10 Sep.
    chat = _sync_chat("10/8", "10/9")
    reference = _at(2026, 9, 11, 12)
    assert parse_chat_text(chat, reference=reference).date_order == "dmy"
    # The chat's newest stored message is on 7 Sep: day/month would put a
    # message on 10 Aug, weeks before it, so month/day wins.
    floor = _at(2026, 9, 7, 20)
    assert parse_chat_text(chat, reference=reference, floor=floor).date_order == "mdy"
    # A floor older than FLOOR_RECENT before the reference is ignored.
    stale = _at(2026, 6, 1, 20)
    assert parse_chat_text(chat, reference=reference, floor=stale).date_order == "dmy"
    # Messages a little before the floor (the overlap of a sync) are fine.
    chat = _sync_chat("10/9", "10/10")
    result = parse_chat_text(chat, reference=_at(2026, 10, 10, 15), floor=_at(2026, 10, 9, 23))
    assert result.date_order == "mdy"


def test_reference_never_overrides_a_decisive_file():
    # Only month/day fits (month 25 is impossible), whatever the reference.
    chat = "1/2/24, 10:00 - Ana Prueba: a\n2/25/24, 10:00 - Ana: c"
    assert parse_chat_text(chat, reference=_at(2024, 1, 2, 12)).date_order == "mdy"


def test_year_first_and_dotted_dates():
    result = parse_chat_text("2024-02-25, 10:00 - Ana Prueba: x")
    assert result.date_order == "ymd" and result.messages[0].sent_at == _at(2024, 2, 25, 10)
    result = parse_chat_text("[25.02.2024, 10:00:05] Ana Prueba: x")
    assert result.messages[0].sent_at == _at(2024, 2, 25, 10, 0, 5)


def test_unparsed_lines_are_counted_not_lost():
    chat = "\n".join(
        [
            "texto suelto antes del primer mensaje",
            "12/9/23, 14:05 - Ana Prueba: hola",
            "40/9/23, 14:06 - Ana Prueba: fecha imposible",
            "[12/9/23, 14:07:00] Ana Prueba: otro estilo",
        ]
    )
    result = parse_chat_text(chat)
    assert result.unparsed_lines == [1, 3, 4]
    assert len(result.messages) == 1
    assert "fecha imposible" in result.messages[0].text  # kept as text
    assert parse_chat_text("nada de whatsapp\notra línea").unparsed_lines == [1, 2]


def test_media_kind_by_extension():
    assert [media_kind(n) for n in ("a.OPUS", "b.jpeg", "c.pdf", "d.mov", "e.docx", "f")] == [
        "audio",
        "image",
        "pdf",
        "video",
        "other",
        "other",
    ]


def test_dedupe_hash_stable_and_normalized():
    at = _at(2024, 2, 25, 10)
    base = dedupe_hash(at, "Ana Prueba", "hola  mundo", False)
    assert base == dedupe_hash(at, "Ana Prueba", f"{LRM}hola mundo\n", False)
    assert base != dedupe_hash(at, "Ana Prueba", "hola mundo", True)
    assert base != dedupe_hash(at, "Beto Ficticio", "hola mundo", False)
    assert base != dedupe_hash(_at(2024, 2, 25, 10, 1), "Ana Prueba", "hola mundo", False)
    # Same message parsed from two overlapping exports → same hash.
    one = parse_chat_text("25/2/24, 10:00 - Ana Prueba: hola mundo").messages[0]
    two = parse_chat_text("24/2/24, 9:00 - Beto: x\n25/2/24, 10:00 - Ana Prueba: hola mundo")
    assert one.dedupe_hash == two.messages[1].dedupe_hash == base


def test_identical_messages_in_one_export_get_distinct_hashes():
    chat = "25/2/24, 10:00 - Ana Prueba: ok\n25/2/24, 10:00 - Ana Prueba: ok"
    first, second = parse_chat_text(chat).messages
    assert first.dedupe_hash != second.dedupe_hash
    assert first.dedupe_hash == dedupe_hash(first.sent_at, "Ana Prueba", "ok", False)


# The same synthetic messages as an Android phone export (the stored
# baseline: dash, M/D/YY, 12 h, minutes only, "<Media omitted>") and as a
# WhatsApp Web export (bracketed, seconds, "<voice message omitted>"),
# including identical messages repeated within one minute.
PHONE_EXPORT = "\n".join(
    [
        f"10/14/26, 9:41{NNBSP}AM - Ana Prueba: Hola",
        f"10/14/26, 9:41{NNBSP}AM - Beto Ficticio: ok",
        f"10/14/26, 9:41{NNBSP}AM - Beto Ficticio: ok",
        f"10/14/26, 9:42{NNBSP}AM - Ana Prueba: <Media omitted>",
        f"10/14/26, 9:42{NNBSP}AM - Ana Prueba: <Media omitted>",
        f"10/14/26, 9:42{NNBSP}AM - Ana Prueba: <Media omitted>",
        f"10/14/26, 1:05{NNBSP}PM - Beto Ficticio: <Media omitted>",
        "receta nueva",
        f"10/15/26, 8:00{NNBSP}AM - Ana Prueba: varias",
        "líneas",
    ]
)
WEB_EXPORT = "\n".join(
    [
        f"[10/14/26, 9:41:05{NNBSP}AM] Ana Prueba: Hola",
        f"[10/14/26, 9:41:20{NNBSP}AM] Beto Ficticio: ok",
        f"[10/14/26, 9:41:59{NNBSP}AM] Beto Ficticio: ok",
        f"[10/14/26, 9:42:01{NNBSP}AM] Ana Prueba: <voice message omitted>",
        f"[10/14/26, 9:42:30{NNBSP}AM] Ana Prueba: <voice message omitted>",
        f"[10/14/26, 9:42:31{NNBSP}AM] Ana Prueba: <image omitted>",
        f"[10/14/26, 1:05:44{NNBSP}PM] Beto Ficticio: <image omitted> receta nueva",
        f"[10/15/26, 8:00:09{NNBSP}AM] Ana Prueba: varias",
        "líneas",
    ]
)


def test_phone_and_web_exports_hash_equally_at_the_minute():
    phone = parse_chat_text(PHONE_EXPORT)
    web = parse_chat_text(WEB_EXPORT)
    assert (phone.style, web.style) == ("dash", "bracketed")
    assert len(phone.messages) == len(web.messages) == 8
    assert [m.dedupe_hash for m in phone.messages] == [m.dedupe_hash for m in web.messages]
    # Same-minute repeats stay distinct within each export...
    assert len({m.dedupe_hash for m in web.messages}) == 8
    # ...the stored sent_at keeps the export's seconds; only the hash drops them.
    assert web.messages[1].sent_at == _at(2026, 10, 14, 9, 41, 20)
    assert phone.messages[1].sent_at == _at(2026, 10, 14, 9, 41)
    # The baseline's minute-only hashes are what they always were.
    assert phone.messages[0].dedupe_hash == dedupe_hash(
        _at(2026, 10, 14, 9, 41), "Ana Prueba", "Hola", False
    )
    assert dedupe_hash(_at(2026, 10, 14, 9, 41, 59), "Ana Prueba", "Hola", False) == (
        phone.messages[0].dedupe_hash
    )
    # A different minute is a different message.
    assert dedupe_hash(_at(2026, 10, 14, 9, 42), "Ana Prueba", "Hola", False) != (
        phone.messages[0].dedupe_hash
    )


@pytest.mark.parametrize(
    ("without_media", "with_media"),
    [
        # Android: generic "<Media omitted>" vs the real file.
        (
            "Ana Prueba: <Multimedia omitido>",
            "Ana Prueba: IMG-20240225-WA0001.jpg (archivo adjunto)",
        ),
        # A document keeps its name in both.
        (
            "Ana Prueba: <document omitted> Lab Prueba.pdf",
            "Ana Prueba: Lab Prueba.pdf (file attached)",
        ),
        # Caption survives both ways.
        ("Ana Prueba: <image omitted> receta", "Ana Prueba: IMG-1.jpg (file attached)\nreceta"),
        # iOS documents: "<name> • N páginas" descriptor, Spanish and English.
        (
            f"Ana Prueba: {LRM}Informe.pdf • {LRM}2 páginas {LRM}documento omitido",
            f"Ana Prueba: {LRM}Informe.pdf • {LRM}2 páginas {LRM}<adjunto: 00000013-Informe.pdf>",
        ),
        (
            f"Ana Prueba: {LRM}Report.pdf • {LRM}1 page {LRM}document omitted",
            f"Ana Prueba: {LRM}Report.pdf • {LRM}1 page {LRM}<attached: 00000021-Report.pdf>",
        ),
        # Desktop (Web) documents: the name after "<document omitted>"; the
        # with-media forms are assumed to follow iOS (descriptor before the
        # tag) or to repeat the name after it.
        (
            "Ana Prueba: <document omitted> Lab Prueba.pdf",
            f"Ana Prueba: Lab Prueba.pdf • 3 pages {LRM}<attached: 00000005-Lab Prueba.pdf>",
        ),
        (
            "Ana Prueba: <document omitted> Lab Prueba.pdf • 3 pages",
            "Ana Prueba: <attached: 00000005-Lab Prueba.pdf> Lab Prueba.pdf",
        ),
    ],
)
def test_dedupe_hash_matches_with_and_without_media(without_media, with_media):
    one = parse_chat_text(f"[25/2/24, 10:00:00] {without_media}").messages[0]
    two = parse_chat_text(f"[25/2/24, 10:00:00] {with_media}").messages[0]
    assert one.media_omitted and not two.media_omitted
    assert one.dedupe_hash == two.dedupe_hash
    if two.media_kind == "pdf":
        assert one.media_kind == "pdf" and one.text == two.text == ""
    # ...but not with a plain text message of the same text.
    plain = parse_chat_text(f"[25/2/24, 10:00:00] Ana Prueba: {two.text}").messages[0]
    assert plain.dedupe_hash != two.dedupe_hash


def test_attached_caption_that_is_not_a_descriptor_is_kept():
    msg = parse_chat_text(
        "[25/2/24, 10:00:00] Ana Prueba: mira esto <adjunto: 00000012-PHOTO-1.jpg>"
    ).messages[0]
    assert msg.text == "mira esto" and msg.media_kind == "image"


def test_ios_sequence_prefix_does_not_change_the_hash():
    first = parse_chat_text("[25/2/24, 10:00:00] Ana Prueba: <adjunto: 00000012-PHOTO-1.jpg>")
    second = parse_chat_text("[25/2/24, 10:00:00] Ana Prueba: <adjunto: 00000099-PHOTO-1.jpg>")
    assert first.messages[0].dedupe_hash == second.messages[0].dedupe_hash


# --- zip --------------------------------------------------------------------


def _zip(path, members: dict[str, bytes | str]):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_parse_export_resolves_media_members(tmp_path):
    path = _zip(
        tmp_path / "export.zip",
        {
            "WhatsApp Chat with Familia Prueba.txt": "\ufeff" + ANDROID_ES,
            "LAB-20230913-WA0001.pdf": b"%PDF-1.4 fake",
            "media/IMG-20230913-WA0003.jpg": b"\xff\xd8 fake",
            "otro.vcf": b"BEGIN:VCARD",
            "../evil.txt": b"x",
        },
    )
    report = parse_export(str(path))
    assert report.chat_member == "WhatsApp Chat with Familia Prueba.txt"
    by_kind = {m.media_kind: m for m in report.messages if m.media_kind}
    assert by_kind["pdf"].media_member == "LAB-20230913-WA0001.pdf"
    assert by_kind["image"].media_member == "media/IMG-20230913-WA0003.jpg"
    assert by_kind["audio"].media_member is None
    assert report.missing_media == 1  # the .opus
    assert report.unreferenced_media == 1  # the .vcf
    assert report.unsafe_members == 1
    assert report.member_count == 5


def test_parse_export_finds_ios_chat_and_accepts_file_objects(tmp_path):
    buffer = io.BytesIO()
    _zip(buffer, {"_chat.txt": IOS_ES, "notas.txt": "no es el chat"})
    buffer.seek(0)
    report = parse_export(buffer)
    assert report.chat_member == "_chat.txt" and len(report.messages) == 6


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ({"foto.jpg": b"x"}, "no contiene el chat"),
        ({"a.txt": "x", "b.txt": "y"}, "varios .txt"),
    ],
)
def test_parse_export_rejects_exports_without_a_chat(tmp_path, members, message):
    with pytest.raises(WaExportError, match=message):
        parse_export(str(_zip(tmp_path / "x.zip", members)))


def test_parse_export_rejects_non_zip_and_oversized_chat(tmp_path, monkeypatch):
    bogus = tmp_path / "x.zip"
    bogus.write_bytes(b"not a zip")
    with pytest.raises(WaExportError, match="zip válido"):
        parse_export(str(bogus))
    monkeypatch.setattr(wa_export, "MAX_CHAT_BYTES", 100)
    path = _zip(tmp_path / "big.zip", {"_chat.txt": "a" * 101})
    with pytest.raises(WaExportError, match="demasiado grande"):
        parse_export(str(path))


# --- CLI --------------------------------------------------------------------


def test_cli_stats_prints_counts_only(tmp_path, capsys):
    path = _zip(tmp_path / "export.zip", {"_chat.txt": WEB})
    assert wa_parse.main([str(path), "--stats"]) == 0
    out = capsys.readouterr().out
    assert "format: bracketed, date order mdy, clock 12h" in out
    assert "messages: 8 (8 from members, 0 system), 2 distinct senders" in out
    assert "media: 0 attached, 6 omitted (1 with a file name)" in out
    assert "audio 2, image 2, pdf 1, video 1, other 0" in out
    assert "date range: 2025-06-17 .. 2025-06-18" in out
    assert "unparsed lines: 1 (1 before the first message; line numbers: 1)" in out
    for secret in ("Ana", "Beto", "Hola", "receta", "Lab Prueba", "end-to-end"):
        assert secret not in out


def test_cli_stats_breaks_a_tie_with_the_reference_time(tmp_path, capsys):
    # A short sync export whose dates read both ways (10/8-10/9): the CLI
    # must report the order the import would choose, not plain day/month.
    chat = "\n".join(
        [
            "[10/8/26, 7:55:12 PM] Ana Prueba: a",
            "[10/9/26, 8:01:03 AM] Beto Ficticio: b",
        ]
    )
    path = _zip(tmp_path / "sync.zip", {"_chat.txt": chat})
    assert wa_parse.main([str(path), "--stats", "--reference", "2026-10-09T15:00"]) == 0
    out = capsys.readouterr().out
    assert "date order mdy" in out
    assert "date range: 2026-10-08 .. 2026-10-09" in out
    # A reference in September makes October the future: day/month wins.
    assert wa_parse.main([str(path), "--stats", "--reference", "2026-09-10T12:00"]) == 0
    assert "date order dmy" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        wa_parse.main([str(path), "--stats", "--reference", "ayer"])


def test_cli_stats_defaults_the_reference_to_now(tmp_path, capsys, monkeypatch):
    seen = {}

    def fake_parse(path, *, reference=None):
        seen["reference"] = reference
        raise wa_parse.WaExportError("x")

    monkeypatch.setattr(wa_parse, "parse_export", fake_parse)
    path = _zip(tmp_path / "x.zip", {"_chat.txt": "x"})
    assert wa_parse.main([str(path), "--stats"]) == 1
    assert seen["reference"].tzinfo is not None
    assert abs((datetime.now(PANAMA_TZ) - seen["reference"]).total_seconds()) < 60


def test_cli_errors(tmp_path, capsys):
    assert wa_parse.main([str(tmp_path / "missing.zip"), "--stats"]) == 1
    with pytest.raises(SystemExit):
        wa_parse.main([str(tmp_path / "missing.zip")])
