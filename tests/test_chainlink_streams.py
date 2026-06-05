from polyedge.chainlink_streams import validate_feed_id_shape


def test_validate_feed_id_shape_accepts_32_byte_hex_with_suffix() -> None:
    feed_id = "0x" + ("0" * 60) + "75b8"
    assert validate_feed_id_shape(feed_id, "75b8") == []


def test_validate_feed_id_shape_rejects_truncated_public_id() -> None:
    issues = validate_feed_id_shape("0x0003...75b8", "75b8")
    assert "feed ID should be a 32-byte hex value, 66 chars including 0x" in issues
    assert "feed ID contains non-hex characters" in issues

