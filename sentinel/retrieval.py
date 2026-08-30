"""Tiny dependency-free TF-IDF retriever used for retrieval-grounding checks.

Not meant to be state of the art — it exists so the Performance lane can verify
factual claims against source documents without pulling in a vector DB.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from functools import lru_cache

from .settings import KNOWLEDGE_DIR

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "are",
    "be", "as", "at", "by", "with", "that", "this", "it", "from", "your", "you",
    "may", "can", "will", "not", "no", "any", "per", "within", "up", "if", "we",
}


def tokenize(text: str) -> list[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP and len(w) > 1]


@dataclass
class Chunk:
    doc: str
    idx: int
    text: str
    heading: str


class Retriever:
    def __init__(self, name: str, chunks: list[Chunk]):
        self.name = name
        self.chunks = chunks
        self._tf: list[dict[str, float]] = []
        df: dict[str, int] = {}
        for ch in chunks:
            toks = tokenize(ch.text)
            counts: dict[str, float] = {}
            for t in toks:
                counts[t] = counts.get(t, 0.0) + 1.0
            self._tf.append(counts)
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        n = max(1, len(chunks))
        self._idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
        self._vecs = [self._vectorize_counts(c) for c in self._tf]

    def _vectorize_counts(self, counts: dict[str, float]) -> dict[str, float]:
        vec = {t: (1.0 + math.log(c)) * self._idf.get(t, 0.0) for t, c in counts.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def _vectorize(self, text: str) -> dict[str, float]:
        counts: dict[str, float] = {}
        for t in tokenize(text):
            counts[t] = counts.get(t, 0.0) + 1.0
        return self._vectorize_counts(counts)

    @staticmethod
    def _cos(a: dict[str, float], b: dict[str, float]) -> float:
        if len(a) > len(b):
            a, b = b, a
        return sum(v * b.get(t, 0.0) for t, v in a.items())

    def search(self, query: str, k: int = 4) -> list[tuple[Chunk, float]]:
        q = self._vectorize(query)
        scored = [(self.chunks[i], self._cos(q, self._vecs[i])) for i in range(len(self.chunks))]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    def max_similarity(self, text: str, context_chunks: list[Chunk]) -> float:
        v = self._vectorize(text)
        best = 0.0
        for ch in context_chunks:
            cv = self._vectorize(ch.text)
            best = max(best, self._cos(v, cv))
        return best


def _load_chunks(doc_name: str) -> list[Chunk]:
    path = KNOWLEDGE_DIR / f"{doc_name}.md"
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    chunks: list[Chunk] = []
    heading = doc_name
    buf: list[str] = []
    idx = 0

    def flush() -> None:
        nonlocal idx, buf
        text = " ".join(x.strip() for x in buf if x.strip())
        if text:
            chunks.append(Chunk(doc=doc_name, idx=idx, text=text, heading=heading))
            idx += 1
        buf = []

    for line in raw.splitlines():
        if line.startswith("#"):
            flush()
            heading = line.lstrip("#").strip()
        elif not line.strip():
            flush()
        else:
            buf.append(line)
    flush()
    return chunks


@lru_cache(maxsize=32)
def get_retriever(doc_name: str) -> Retriever:
    return Retriever(doc_name, _load_chunks(doc_name))
