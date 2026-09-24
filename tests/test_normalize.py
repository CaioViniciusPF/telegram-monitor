from tgmonitor.matching.normalize import normalize


def test_accents_and_case() -> None:
    assert normalize("PELÍCULA Câmera Ação") == "pelicula camera acao"


def test_whitespace_collapsed() -> None:
    assert normalize("  Galaxy\n\tS25   Ultra ") == "galaxy s25 ultra"


def test_styled_unicode_letters() -> None:
    # Canais de promoção usam letras "matemáticas" para negrito.
    assert normalize("𝐆𝐚𝐥𝐚𝐱𝐲 𝐒𝟐𝟓") == "galaxy s25"


def test_idempotent() -> None:
    s = "Àçúcar  ÇÃO"
    assert normalize(normalize(s)) == normalize(s)
