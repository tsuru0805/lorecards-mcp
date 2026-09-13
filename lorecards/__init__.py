"""lorecards — a keyword-triggered card book for companion agents."""

from .engine import Card, CardError, consult, list_cards, read_card, render_context, write_card
from .schema import CardSchema, load_schema

__version__ = "0.1.0"
__all__ = ["Card", "CardError", "CardSchema", "consult", "list_cards", "load_schema",
           "read_card", "render_context", "write_card", "__version__"]
