from tgmonitor.telegram.links import internal_channel_id, message_link


def test_public_channel() -> None:
    assert message_link(-1001234567890, 42, "promocanal") == "https://t.me/promocanal/42"


def test_private_channel() -> None:
    assert internal_channel_id(-1001234567890) == 1234567890
    assert message_link(-1001234567890, 42, None) == "https://t.me/c/1234567890/42"


def test_basic_group_has_no_link() -> None:
    assert message_link(-123456789, 42, None) is None
