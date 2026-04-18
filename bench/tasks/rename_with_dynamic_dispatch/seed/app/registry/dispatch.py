"""String-keyed dispatch table. Handlers are looked up at runtime."""
from app.handlers import quote


HANDLERS = {
    "compute_price": quote.compute_price,
}


def dispatch(name: str, *args):
    return HANDLERS[name](*args)
