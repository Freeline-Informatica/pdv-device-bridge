from pdv_device_bridge.scale_parser import parse_weight_payload


def test_parse_weight_with_comma_and_kg_unit() -> None:
    payload = b"\x02ST,GS,+ 0,350kg\r\n"

    parsed = parse_weight_payload(payload)

    assert parsed is not None
    assert parsed.grams == 350
    assert parsed.kilograms == 0.35
    assert parsed.stable is True


def test_parse_weight_with_noise_and_grams_unit() -> None:
    payload = b"noise US,NT +1234g ###"

    parsed = parse_weight_payload(payload)

    assert parsed is not None
    assert parsed.grams == 1234
    assert parsed.kilograms == 1.234
    assert parsed.stable is False


def test_parse_weight_without_unit_defaults_to_kg() -> None:
    payload = b"ST +1.250"

    parsed = parse_weight_payload(payload)

    assert parsed is not None
    assert parsed.grams == 1250


def test_parse_weight_empty_payload_returns_none() -> None:
    assert parse_weight_payload(b"") is None


def test_parse_weight_invalid_bytes_returns_none() -> None:
    assert parse_weight_payload(b"\xff\xfe\x00") is None
