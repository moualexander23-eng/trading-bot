from bot.roostoo_client import RoostooClient, format_decimal


def test_signature_matches_roostoo_docs_example():
    # Example from https://github.com/roostoo/Roostoo-API-Documents
    c = RoostooClient("USEAPIKEYASMYID", "S1XP1e3UZj6A7H5fATj0jNhqPxxdSJYdInClVN65XAbvqqMKjVHjA7PZj4W12oep")
    params = {"pair": "BNB/USD", "quantity": "2000", "side": "BUY", "timestamp": "1580774512000", "type": "MARKET"}
    headers, total = c.sign(params)
    assert total == "pair=BNB/USD&quantity=2000&side=BUY&timestamp=1580774512000&type=MARKET"
    assert headers["MSG-SIGNATURE"] == "20b7fd5550b67b3bf0c1684ed0f04885261db8fdabd38611e9e6af23c19b7fff"
    assert headers["RST-API-KEY"] == "USEAPIKEYASMYID"


def test_signature_is_order_insensitive():
    c = RoostooClient("k", "s")
    a = c.sign({"b": "2", "a": "1"})
    b = c.sign({"a": "1", "b": "2"})
    assert a == b


def test_format_decimal_rounds_down_without_scientific_notation():
    assert format_decimal(0.123456789, 5) == "0.12345"
    assert format_decimal(1234.99, 0) == "1234"
    assert format_decimal(0.00000439, 8) == "0.00000439"
    assert format_decimal(2.0, 2) == "2.00"
