"""Bigram Jaccard text similarity — shared by golden_samples.py (few-shot
example retrieval) and recurrence.py (duplicate-issue detection).

Pulled out of golden_samples.py verbatim; that module keeps private aliases
(`_bigrams`/`_jaccard_similarity`) pointing here so its behavior and the one
existing caller (agent_orchestrator.py) are unchanged byte-for-byte.
"""
from __future__ import annotations

import re


def bigrams(text: str) -> set:
    """Generate bigram tokens from text. Chinese chars individually, English by word."""
    tokens = []
    # Split into Chinese chars and English words
    for part in re.findall(r'[一-鿿]|[a-zA-Z0-9]+', text.lower()):
        if len(part) == 1 and '一' <= part <= '鿿':
            tokens.append(part)
        else:
            tokens.append(part)

    # Generate bigrams
    result = set()
    for i in range(len(tokens)):
        result.add(tokens[i])
        if i + 1 < len(tokens):
            result.add(tokens[i] + tokens[i + 1])
    return result


def jaccard_similarity(text_a: str, text_b: str) -> float:
    """Compute Jaccard similarity between two texts using bigram tokens."""
    a = bigrams(text_a)
    b = bigrams(text_b)
    if not a or not b:
        return 0.0
    intersection = a & b
    union = a | b
    return len(intersection) / len(union) if union else 0.0
