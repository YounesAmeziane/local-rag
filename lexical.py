# lexical.py
# Zero-dependency Okapi BM25 lexical index for hybrid retrieval (audit #6).
#
# Dense (cosine) search blurs exact technical tokens — AES_KEY_BASE64, StateID,
# error codes, dotted API names, config keys — which are precisely what a governance
# corpus is queried by. BM25 scores exact-term overlap, so fusing it with the dense
# ranker (see retriever._rrf_fuse) recovers those literal matches.
#
# The corpus is tiny (hundreds of points), so a pure-Python in-memory index is
# instant and needs no extra dependency (rank_bm25/torch are absent by design). If
# the corpus grows to thousands, move to Qdrant native sparse vectors + re-ingest.

import math
import re
from collections import Counter

# Keep [a-z0-9_] runs as single tokens so underscored compounds survive whole
# (AES_KEY_BASE64 -> one term). Other punctuation (including '.') splits, but it
# splits the query identically, so "api.Foo" and "StateID = 4" still match on their
# parts. Lowercasing is applied on both sides, so it never breaks exact matching.
_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def tokenize(text: str) -> list[str]:
    if not text:
        return []
    return _TOKEN_RE.findall(text.lower())


class BM25Index:
    """Okapi BM25 over an immutable set of documents.

    Each document is (id, text, payload). `id` and `payload` are opaque — they are
    carried through so callers can map hits back to Qdrant points. `search` returns
    the top-k as (id, score, payload), highest score first, positives only.
    """

    def __init__(self, docs: list[tuple], k1: float = 1.5, b: float = 0.75):
        self.ids = [d[0] for d in docs]
        self.payloads = [d[2] for d in docs]
        self._tokens = [tokenize(d[1]) for d in docs]
        self.k1 = k1
        self.b = b

        self.N = len(docs)
        self.doc_len = [len(t) for t in self._tokens]
        self.avgdl = (sum(self.doc_len) / self.N) if self.N else 0.0
        self.tf = [Counter(t) for t in self._tokens]

        df = Counter()
        for toks in self._tokens:
            df.update(set(toks))
        # Standard BM25 idf with +1 inside the log to keep it non-negative even for
        # terms present in more than half the (tiny) corpus.
        self.idf = {
            term: math.log(1 + (self.N - n + 0.5) / (n + 0.5))
            for term, n in df.items()
        }

    def search(self, query: str, top_k: int) -> list[tuple]:
        if self.N == 0:
            return []
        q_terms = tokenize(query)
        if not q_terms:
            return []

        scored: list[tuple] = []
        for i in range(self.N):
            tf_i = self.tf[i]
            if not tf_i:
                continue
            dl = self.doc_len[i]
            denom_norm = self.k1 * (1 - self.b + self.b * dl / self.avgdl) if self.avgdl else self.k1
            s = 0.0
            for term in q_terms:
                freq = tf_i.get(term)
                if not freq:
                    continue
                idf = self.idf.get(term, 0.0)
                s += idf * (freq * (self.k1 + 1)) / (freq + denom_norm)
            if s > 0:
                scored.append((self.ids[i], s, self.payloads[i]))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]
