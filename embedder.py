"""Pluggable embedder with two backends, chosen by the $CC_MEM_EMBEDDER env var:

* "local"   — a real sentence-transformers model (default: all-MiniLM-L6-v2,
              384-dim) for true SEMANTIC recall (synonyms, paraphrase, meaning).
              Runs offline after a one-time model download. Requires the venv.
* "lexical" — a dependency-free, deterministic hashing embedder (token hashing +
              char trigrams). Stable, zero installs, but only matches shared
              words/substrings. The safe fallback if the model can't load.

`graph_memory` only ever calls `embed(text)`, so nothing else changes between
backends. IMPORTANT: a single DB must use ONE backend — vectors of different
dimensions/spaces can't be compared. Pick before the first insert.

You can also install any function yourself:
    from embedder import set_embedder
    set_embedder(my_fn)          # my_fn(text: str) -> list[float], fixed dim
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sys

# Dimension of the active embedder. Updated when the local model loads.
EMBED_DIM = 256

# Default local model. Override with $CC_MEM_MODEL.
_LOCAL_MODEL_NAME = os.environ.get("CC_MEM_MODEL", "all-MiniLM-L6-v2")

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _hash_to_index(token: str, salt: str) -> tuple[int, float]:
    """Map a token to a (dimension, sign) pair via a stable hash.

    The sign turns this into a signed random-projection style sketch so that
    unrelated tokens tend to cancel rather than always add — which keeps the
    cosine geometry meaningful instead of every vector pointing the same way.
    """
    h = hashlib.blake2b(f"{salt}:{token}".encode("utf-8"), digest_size=8).digest()
    n = int.from_bytes(h, "big")
    idx = n % EMBED_DIM
    sign = 1.0 if (n >> 16) & 1 else -1.0
    return idx, sign


def _trigrams(token: str) -> list[str]:
    """Character trigrams of a padded token, for fuzzy partial-word overlap.

    'docker' and 'dockerfile' share trigrams, so they land near each other
    even though they are not the same token."""
    if len(token) < 3:
        return [token]
    padded = f"#{token}#"
    return [padded[i : i + 3] for i in range(len(padded) - 2)]


def hash_embed(text: str) -> list[float]:
    """Deterministic lexical embedding. Same text -> same vector, always."""
    vec = [0.0] * EMBED_DIM
    tokens = _TOKEN_RE.findall((text or "").lower())
    if not tokens:
        return vec

    for tok in tokens:
        # Whole-token signal (weighted higher than fuzzy trigrams).
        idx, sign = _hash_to_index(tok, "tok")
        vec[idx] += sign * 2.0
        # Fuzzy trigram signal.
        for tri in _trigrams(tok):
            i, s = _hash_to_index(tri, "tri")
            vec[i] += s * 1.0

    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return vec
    return [v / norm for v in vec]


# ── Local semantic backend (sentence-transformers), loaded lazily ────────────

_model = None


def _load_local_model():
    global _model, EMBED_DIM
    if _model is not None:
        return _model
    from sentence_transformers import SentenceTransformer  # heavy import, lazy
    _model = SentenceTransformer(_LOCAL_MODEL_NAME)
    _dim = getattr(_model, "get_embedding_dimension", None) \
        or _model.get_sentence_embedding_dimension
    EMBED_DIM = _dim()
    print(f"[cc-mem] loaded local embedder {_LOCAL_MODEL_NAME} (dim={EMBED_DIM})",
          file=sys.stderr, flush=True)
    return _model


def local_embed(text: str) -> list[float]:
    model = _load_local_model()
    vec = model.encode(text or "", normalize_embeddings=True)
    return vec.tolist()


# ── Backend selection ─────────────────────────────────────────────────────────

def _lazy_local(text: str) -> list[float]:
    """First-call shim for the 'local' backend: load the model on the first
    embed (not at import — so the server binds instantly), then swap _embed_fn
    to the direct function. Falls back to lexical if the model can't load, so
    the server never crashes."""
    global _embed_fn
    try:
        _load_local_model()
        _embed_fn = local_embed
    except Exception as exc:  # missing package / download failure
        print(f"[cc-mem] local embedder unavailable ({exc}); "
              f"falling back to lexical", file=sys.stderr, flush=True)
        _embed_fn = hash_embed
    return _embed_fn(text)


def _select_default():
    """Pick the backend from $CC_MEM_EMBEDDER. 'local' is loaded lazily on first
    use; anything else uses the dependency-free lexical embedder."""
    choice = os.environ.get("CC_MEM_EMBEDDER", "lexical").strip().lower()
    return _lazy_local if choice == "local" else hash_embed


# The active embedder. Swap with set_embedder().
_embed_fn = _select_default()


def set_embedder(fn) -> None:
    """Install a different embedding function. Must return a fixed-length
    list[float]; if its dimension differs from EMBED_DIM, update EMBED_DIM too
    BEFORE creating/opening a store (vectors of mixed dims cannot be compared)."""
    global _embed_fn
    _embed_fn = fn


def embed(text: str) -> list[float]:
    return _embed_fn(text)


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Both vectors are expected L2-normalized, so this is
    just a dot product, but we stay safe if one isn't."""
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    denom = math.sqrt(na) * math.sqrt(nb)
    return dot / denom if denom else 0.0
