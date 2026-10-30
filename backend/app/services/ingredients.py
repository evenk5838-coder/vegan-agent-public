"""Shared ingredient assessment and 90-day history construction.

Callers own the transaction and response/card shape. This service performs no
provider requests, image cleanup, task transitions or implicit commit.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from typing import Literal

from .. import rules
from ..core import Record, now


def evaluate_and_record(db, owner: str, text: str, *, complete: bool,
                        label_warnings: list[str], avoid_coriander: bool = False,
                        source: Literal['manual', 'photo', 'agent'],
                        photo: dict | None = None) -> tuple[dict, Record]:
    """Evaluate the exact evidence and add a history record to caller's unit of work.

    Photo metadata links the retained asset without copying file paths or image
    contents into history. Text and completeness are never inferred here.
    """
    result = rules.evaluate(text, complete, label_warnings, avoid_coriander)
    payload = {'text': text, 'result': result, 'source': source}
    if photo is not None:
        payload['photo'] = deepcopy(photo)
    history = Record(owner=owner, kind='ingredient', payload=payload,
                     expires=now() + timedelta(days=90))
    db.add(history)
    db.flush()
    return result, history
