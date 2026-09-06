"""Embed snippet text into vectors via a pluggable provider.

snippets.content strings -> embed() -> float32 vectors -> to_blob() bytes
for the snippets.embedding column. triage.py calls embed() when a snippet
is confirmed; search.py calls from_blob() to unpack stored vectors back
into numpy arrays for cosine similarity.

In: list of content strings (already includes the context prefix, since
    that's what gets embedded — decision 18).
Out: {"ok": True, "vectors": [[float]...], "model": str, "backend": str}
     or {"ok": False, "error": str, "backend": str}. Never raises. A
     failed embed is not fatal to triage: the caller stores the snippet
     with embedding=NULL and backfills later — FTS5 search is unaffected
     either way (decision 15's "gap" reasoning applies to embed failures
     too, not just discarded chunks).
State: one HTTPS POST per call (stdlib urllib, no new HTTP dependency),
       no local caching.
"""

import json
import os
import time
import urllib.error
import urllib.request

import numpy as np

NAME = "knowledge_embedder"
DESCRIPTION = "Embed text into vectors via Voyage AI (or a swappable local model)."
INPUT_SCHEMA = {
    "texts": {"type": "array", "required": True},
    "model": {"type": "string", "required": False},
    "input_type": {"type": "string", "required": False},
}

# voyage-3.x lost its free-tier allocation under Voyage's Sept-2026 pricing —
# only the voyage-4 series (plus voyage-context-*/code-3) still carries the
# 200M free tokens PLAN.md decision 5 relies on. voyage-4's 32,000-token
# context also dwarfs MAX_CHARS (~2,000 tokens) — over_cap chunks are a
# non-issue for this provider; the cap exists for the EmbeddingGemma local
# swap target (decision 6), whose 2,048-token window is the real constraint.
DEFAULT_MODEL = "voyage-4"
API_URL = "https://api.voyageai.com/v1/embeddings"
MAX_BATCH = 1000  # Voyage's own per-call limit on len(texts)
_KNOWN_BACKENDS = ("voyage", "local", "off")
_RETRY_STATUSES = {429, 500, 502, 503}


def to_blob(vector) -> bytes:
    """Pack a vector into raw float32 bytes for the embedding BLOB column.

    In: a list/array of numbers.
    Out: bytes — exactly 4 * len(vector) bytes. This is the one place the
         byte layout is defined; from_blob() below is its exact inverse.
    """
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    """Unpack an embedding BLOB back into a float32 vector.

    In: bytes previously produced by to_blob().
    Out: 1-D float32 numpy array, ready for a cosine-similarity matmul.
    """
    return np.frombuffer(blob, dtype=np.float32)


def _post(body: bytes, api_key: str) -> dict:
    """One HTTPS POST to Voyage, with one bounded retry on a transient status.

    In: JSON-encoded request body, API key.
    Out: decoded JSON payload. Raises urllib.error.HTTPError /
         URLError / json.JSONDecodeError on final failure — caught by the
         one caller, _embed_voyage, which turns them into the dict contract.
    """
    req = urllib.request.Request(
        API_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code not in _RETRY_STATUSES:
            raise
        time.sleep(2)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())


def _embed_voyage(texts: list, model: str, input_type: str) -> dict:
    """Call the Voyage REST API for one batch.

    In: texts (<= MAX_BATCH, caller's responsibility), model, input_type.
    Out: see embed()'s docstring. truncation is always left at Voyage's
         own default (True) — an over-length chunk gets truncated by
         Voyage, never rejected; stored `content` is untouched either way.
    """
    api_key = os.environ.get("VOYAGE_API_KEY")
    if not api_key:
        return {"ok": False, "error": "VOYAGE_API_KEY not set", "backend": "voyage"}

    body = json.dumps({"input": texts, "model": model, "input_type": input_type}).encode()
    try:
        payload = _post(body, api_key)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        return {"ok": False, "error": f"voyage API error {e.code}: {detail}", "backend": "voyage"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"voyage request failed: {e.reason}", "backend": "voyage"}
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"voyage returned invalid JSON: {e}", "backend": "voyage"}

    vectors = [item["embedding"] for item in payload.get("data", [])]
    if len(vectors) != len(texts):
        return {
            "ok": False,
            "error": f"voyage returned {len(vectors)} vectors for {len(texts)} inputs",
            "backend": "voyage",
        }
    return {"ok": True, "vectors": vectors, "model": model, "backend": "voyage"}


def embed(texts: list, model: str = DEFAULT_MODEL, input_type: str = "document") -> dict:
    """Embed a batch of texts.

    In: content strings — caller batches per document (a few dozen chunks),
        well under Voyage's MAX_BATCH of 1,000. input_type is "document"
        for stored snippets (the default) or "query" for a search string —
        Voyage prepends a different retrieval-tuning prompt to each, and
        the two are compatible in the same vector space (not a separate
        embedding_model value).
    Out: see module docstring. Backend from KNOWLEDGE_EMBED env var,
         default "voyage". "off" fails without a call (tests, dev without
         burning quota). "local" is decision 6's swap target — the
         interface exists so triage.py never changes when it lands, but
         no implementation exists yet.
    """
    if not texts:
        return {"ok": False, "error": "no texts to embed"}
    if len(texts) > MAX_BATCH:
        return {"ok": False, "error": f"{len(texts)} texts exceeds Voyage's {MAX_BATCH}-per-call limit"}

    backend = os.environ.get("KNOWLEDGE_EMBED", "voyage")
    if backend == "off":
        return {"ok": False, "error": "KNOWLEDGE_EMBED=off", "backend": "off"}
    if backend == "local":
        return {"ok": False, "error": "local backend not yet implemented", "backend": "local"}
    if backend not in _KNOWN_BACKENDS:
        return {"ok": False, "error": f"unknown KNOWLEDGE_EMBED backend: {backend}", "backend": backend}
    return _embed_voyage(texts, model, input_type)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument("text", nargs="+", help="Text(s) to embed")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--input-type", default="document", choices=("document", "query"))
    args = parser.parse_args()

    result = embed(args.text, model=args.model, input_type=args.input_type)
    if result.get("ok"):
        result = {**result, "vectors": [f"<{len(v)} floats>" for v in result["vectors"]]}
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
