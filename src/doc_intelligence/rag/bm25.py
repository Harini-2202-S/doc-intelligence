"""BM25 keyword ranking and Reciprocal Rank Fusion.

BM25 complements dense retrieval on exact terms such as names, codes and numbers.
"""

import math
import re
from collections import Counter

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by for from has have in is it its of on or shall that the this to was "
    "were will with any all such not no may".split()
)


def tokenize(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


class BM25:
    def __init__(self, docs: list[tuple[str, str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.ids = [doc_id for doc_id, _ in docs]
        self.tfs = [Counter(tokenize(text)) for _, text in docs]
        self.lengths = [sum(tf.values()) for tf in self.tfs]
        self.avg_len = (sum(self.lengths) / len(self.lengths)) if self.lengths else 0.0
        df: Counter[str] = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        n = len(docs)
        self.idf = {term: math.log(1 + (n - freq + 0.5) / (freq + 0.5)) for term, freq in df.items()}
        self.k1, self.b = k1, b

    def search(self, query: str, limit: int = 20) -> list[tuple[str, float]]:
        terms = set(tokenize(query))
        scored = []
        for i, tf in enumerate(self.tfs):
            score = 0.0
            for term in terms:
                freq = tf.get(term)
                if not freq:
                    continue
                norm = self.k1 * (1 - self.b + self.b * self.lengths[i] / (self.avg_len or 1))
                score += self.idf[term] * freq * (self.k1 + 1) / (freq + norm)
            if score > 0:
                scored.append((self.ids[i], score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:limit]


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Merges ranked id lists using ranks only, so scores from different retrievers need not be comparable."""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda item: item[1], reverse=True)
