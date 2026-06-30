"""Repetition detector — flags when a candidate asks the same question 2+ times.

Uses lightweight Jaccard similarity over normalized tokens. Good enough for
call-script intent matching without an embedding call per turn.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
_STOPWORDS = {
    "і", "та", "а", "але", "що", "як", "це", "у", "в", "на", "до", "по",
    "и", "а", "но", "что", "как", "это", "в", "на", "к", "по",
    "the", "a", "an", "is", "are", "do", "does",
}


def _tokens(text: str) -> set[str]:
    toks = [t.lower() for t in _TOKEN_RE.findall(text)]
    return {t for t in toks if t not in _STOPWORDS and len(t) > 2}


def _similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class RepetitionTracker:
    history: list[str] = field(default_factory=list)
    threshold: float = 0.55
    counts: Counter[int] = field(default_factory=Counter)

    def observe(self, utterance: str) -> int:
        """Add an utterance; return count of similar previous utterances."""
        match_idx = -1
        best_sim = 0.0
        for i, prev in enumerate(self.history):
            sim = _similarity(utterance, prev)
            if sim >= self.threshold and sim > best_sim:
                best_sim = sim
                match_idx = i
        self.history.append(utterance)
        if match_idx >= 0:
            self.counts[match_idx] += 1
            return self.counts[match_idx] + 1
        return 1
