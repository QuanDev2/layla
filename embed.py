"""Embed snippet text into vectors via a local EmbeddingGemma model.

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
State: HTTP POST to the local ollama daemon (stdlib urllib, no new HTTP
       dependency), one request per MAX_BATCH slice, no local caching.
"""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np

NAME = "knowledge_embedder"
DESCRIPTION = "Embed text into vectors via a local EmbeddingGemma model served by ollama."
INPUT_SCHEMA = {
    "texts": {"type": "array", "required": True},
    "model": {"type": "string", "required": False},
    "input_type": {"type": "string", "required": False},
}


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from the repo-root .env into os.environ.

    In: none — reads {repo root}/.env if present.
    Out: none. A real exported environment variable always wins — this
         only fills in a name that isn't already set. Silent no-op if the
         file is missing; a malformed line is skipped, never raised.
         Generic, not provider-specific — this module needs no key at all,
         but any KEY=VALUE line is loaded, so the same file covers
         ANTHROPIC_API_KEY for heading_agent.py's optional backend if this
         module has already been imported.
    State: mutates os.environ.
    """
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

# DEFAULT_MODEL is the string stored in snippets.embedding_model, and
# search.py compares it row-by-row to keep vector spaces from mixing —
# so it names the quantization, not just the model. OLLAMA_MODEL is the
# tag the daemon serves: always explicit, never bare "embeddinggemma",
# which resolves to the BF16 build and a different vector space.
DEFAULT_MODEL = "embeddinggemma-q8"
OLLAMA_MODEL = "embeddinggemma:300m-qat-q8_0"
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434") + "/api/embed"
MAX_BATCH = 64  # texts per HTTP request; longer lists are sliced, not rejected
EMBED_DIMS = 768
_KNOWN_BACKENDS = ("local", "off")

# Longest content this model can embed without silent truncation. Derived,
# not guessed: the window is 2,048 tokens, and the worst measured ratio on
# this corpus is 2.45 chars/token — timestamped transcript text tokenizes
# far denser than prose. 4,800 chars is ~1,960 tokens at that ratio, inside
# the window with the task prefix counted. chunker.py flags a chunk past
# this as over_cap; nothing rejects it, since FTS5 still indexes all of it.
MAX_INPUT_CHARS = 4800

# EmbeddingGemma is trained with task prefixes and measurably degrades
# without them. "title: none" is deliberate: chunk content already opens
# with its own context prefix (decision 18), so the real title would
# duplicate it.
_PREFIXES = {
    "document": "title: none | text: ",
    "query": "task: search result | query: ",
}


def to_blob(vector) -> bytes:
    """Pack a vector into raw float32 bytes for the embedding BLOB column.

    In: sequence of floats.
    Out: float32 little-endian bytes, width-agnostic.
    """
    return np.asarray(vector, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    """Unpack an embedding BLOB back into a float32 vector.

    In: bytes from snippets.embedding.
    Out: 1-D float32 numpy array.
    """
    return np.frombuffer(blob, dtype=np.float32)


def _post(texts: list) -> list:
    """One HTTP POST to the ollama daemon for one batch.

    In: prefixed texts, at most MAX_BATCH.
    Out: list of raw float vectors. Raises urllib.error.HTTPError /
         URLError / json.JSONDecodeError / KeyError on failure — caught by
         the one caller, _embed_local, which turns them into the contract.
    """
    body = json.dumps({"model": OLLAMA_MODEL, "input": texts}).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read())["embeddings"]


def _embed_local(texts: list, model: str, input_type: str) -> dict:
    """Embed one list of texts through the local ollama daemon.

    In: texts, model (the stored embedding_model string), input_type
        keying _PREFIXES. Slices internally at MAX_BATCH.
    Out: see embed()'s docstring. Over-length text is truncated by the
         model at its 2,048-token window, never rejected; stored `content`
         is untouched either way.
    """
    prefix = _PREFIXES.get(input_type)
    if prefix is None:
        return {
            "ok": False,
            "error": f"unknown input_type: {input_type}",
            "backend": "local",
        }

    vectors = []
    for start in range(0, len(texts), MAX_BATCH):
        batch = [prefix + t for t in texts[start:start + MAX_BATCH]]
        try:
            vectors.extend(_post(batch))
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            return {"ok": False, "error": f"ollama error {e.code}: {detail}", "backend": "local"}
        except urllib.error.URLError as e:
            return {
                "ok": False,
                "error": f"ollama daemon unreachable at {OLLAMA_URL} ({e.reason}) — "
                         f"start it with `ollama serve`",
                "backend": "local",
            }
        except (json.JSONDecodeError, KeyError) as e:
            return {"ok": False, "error": f"ollama returned an unexpected payload: {e}",
                    "backend": "local"}

    if len(vectors) != len(texts):
        return {
            "ok": False,
            "error": f"ollama returned {len(vectors)} vectors for {len(texts)} inputs",
            "backend": "local",
        }
    bad = next((len(v) for v in vectors if len(v) != EMBED_DIMS), None)
    if bad is not None:
        return {
            "ok": False,
            "error": f"ollama returned {bad}-dim vectors, expected {EMBED_DIMS}",
            "backend": "local",
        }
    return {"ok": True, "vectors": vectors, "model": model, "backend": "local"}


def embed(texts: list, model: str = DEFAULT_MODEL, input_type: str = "document") -> dict:
    """Embed a batch of texts.

    In: content strings — caller batches per document (a few dozen
        chunks); lists longer than MAX_BATCH are sliced internally.
        input_type is "document" for stored snippets (the default) or
        "query" for a search string. Each gets a different training
        prefix, and the two share one vector space (not a separate
        embedding_model value).
    Out: see module docstring. Backend from KNOWLEDGE_EMBED env var,
         default "local". "off" fails without a call (tests, dev with no
         daemon running).
    """
    if not texts:
        return {"ok": False, "error": "no texts to embed"}

    backend = os.environ.get("KNOWLEDGE_EMBED", "local")
    if backend == "off":
        return {"ok": False, "error": "KNOWLEDGE_EMBED=off", "backend": "off"}
    if backend not in _KNOWN_BACKENDS:
        return {"ok": False, "error": f"unknown KNOWLEDGE_EMBED backend: {backend}", "backend": backend}
    return _embed_local(texts, model, input_type)


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
