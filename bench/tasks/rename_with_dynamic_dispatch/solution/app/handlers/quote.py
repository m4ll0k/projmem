"""Quote-handler. Exposed via the dispatch dict."""


def quote_price(qty: int) -> float:
    return qty * 1.25
