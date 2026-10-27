"""Expression selection for V3 conversation turns."""

import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Expression:
    """One emotion label and its strength for a conversation turn.

    Attributes:
        label: An emotion label from the avatar's ``available_emotions``.
        strength: How strongly the avatar shows it, from 0 to 1. Zero is a valid
            selection, not a default. The value is rounded to float32, as sent.
    """

    label: str
    strength: float

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label:
            raise ValueError("expression label must be a nonempty string")
        strength = self.strength
        if (
            isinstance(strength, bool)
            or not isinstance(strength, (int, float))
            or not math.isfinite(strength)
            or not 0 <= strength <= 1
        ):
            raise ValueError(
                "expression strength must be a finite number between 0 and 1"
            )
        object.__setattr__(
            self, "strength", struct.unpack("<f", struct.pack("<f", strength))[0]
        )


def validate_expressions(expressions: Sequence[Expression]) -> tuple[Expression, ...]:
    """Checks a turn's expression list: nonempty, ``Expression`` items, unique labels."""
    if isinstance(expressions, (str, bytes)) or not isinstance(expressions, Sequence):
        raise ValueError("expressions must be a sequence of Expression")
    selected = tuple(expressions)
    if not selected:
        raise ValueError("at least one expression is required")
    if not all(isinstance(expression, Expression) for expression in selected):
        raise ValueError("expressions must be a sequence of Expression")
    if len({expression.label for expression in selected}) != len(selected):
        raise ValueError("expression labels must be unique")
    return selected
