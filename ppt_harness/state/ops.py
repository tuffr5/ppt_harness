"""Ops and turn transactions — DESIGN §1.6.

Every mutation is an invertible op attributed to user, model or lint. Ops group into a
turn-scoped transaction so undo matches what a user thinks they did ("undo that request")
rather than what the model happened to call.

An op carries its own inverse, including the *name* of the inverting op — `add_slide`
inverts to `delete_slide`, not to itself with a flipped patch. Symmetric ops leave
`inverse_op` unset and invert to themselves.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .document import Author


class Op(BaseModel):
    seq: int
    op: str
    target: str
    patch: dict[str, Any]
    inverse: dict[str, Any]
    author: Author
    turn: int
    inverse_op: str | None = None
    """Name of the op that undoes this one. `None` means it inverts to itself."""

    def inverted(self) -> Op:
        return Op(
            seq=-1,
            op=self.inverse_op or self.op,
            target=self.target,
            patch=self.inverse,
            inverse=self.patch,
            author=self.author,
            turn=self.turn,
            inverse_op=self.op if self.inverse_op else None,
        )


class Turn(BaseModel):
    """Atomic unit of undo. Commits whole or rolls back whole."""

    id: int
    ops: list[Op] = Field(default_factory=list)
    committed: bool = False


class OpLog:
    """Append-only log with single-writer semantics.

    The writer lock is what keeps a user dragging a shape mid-turn from corrupting the
    model's assumptions.
    """

    def __init__(self) -> None:
        self._ops: list[Op] = []
        self._turns: list[Turn] = []
        self._locked_by: Author | None = None

    @property
    def turns(self) -> list[Turn]:
        return self._turns

    @property
    def ops(self) -> list[Op]:
        return list(self._ops)

    # -- writer lock ------------------------------------------------------------

    def acquire(self, author: Author) -> bool:
        if self._locked_by is not None:
            return False
        self._locked_by = author
        return True

    def release(self) -> None:
        self._locked_by = None

    # -- transactions -----------------------------------------------------------

    def begin(self) -> Turn:
        turn = Turn(id=len(self._turns))
        self._turns.append(turn)
        return turn

    def append(self, turn: Turn, op: Op) -> None:
        op.seq = len(self._ops)
        self._ops.append(op)
        turn.ops.append(op)

    def commit(self, turn: Turn) -> None:
        turn.committed = True

    def restore(self, turns: list[Turn]) -> None:
        """Adopt history recorded before this process started.

        Numbering continues from what was restored, so a turn id stays unique for the life
        of the deck rather than only for the life of the process — a journal with two turn
        3s cannot be replayed in order.
        """
        for turn in turns:
            self._turns.append(turn)
            self._ops.extend(turn.ops)

    def inverses(self, turn: Turn) -> list[Op]:
        """Ops that undo this turn, newest first. Pure — the turn is left intact so a
        committed turn stays redoable."""
        return [op.inverted() for op in reversed(turn.ops)]

    def discard(self, turn: Turn) -> list[Op]:
        """Abandon a failed transaction: return its inverses and erase it from the log."""
        inverses = self.inverses(turn)
        ids = {id(op) for op in turn.ops}
        self._ops = [op for op in self._ops if id(op) not in ids]
        turn.ops.clear()
        if turn in self._turns:
            self._turns.remove(turn)
        return inverses

    # -- queries ----------------------------------------------------------------

    def corrections(self) -> list[tuple[Op, Op]]:
        """(model_op, user_op) pairs where a user overrode the model on the same target.

        This is the observed channel of the preference profile (DESIGN §8.2) — no extra
        instrumentation, just a query over what the log already records.
        """
        pairs: list[tuple[Op, Op]] = []
        last_model: dict[str, Op] = {}
        for op in self._ops:
            if op.author is Author.MODEL:
                last_model[op.target] = op
            elif op.author is Author.USER and op.target in last_model:
                pairs.append((last_model.pop(op.target), op))
        return pairs

    def __len__(self) -> int:
        return len(self._ops)
