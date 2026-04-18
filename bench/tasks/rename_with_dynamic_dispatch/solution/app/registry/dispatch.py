"""String-keyed dispatch table. Handlers are looked up at runtime."""
from app.handlers import quote


HANDLERS = {
    "quote_price": quote.quote_price,
}


def dispatch(name: str, *args):
    return HANDLERS[name](*args)
