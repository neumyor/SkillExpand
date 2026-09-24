"""Lightweight embedding dependency for executor construction; routing uses descriptions."""

import hashlib
import math
import re
from typing import Any, Dict, List, Optional, Sequence

#: Tokenisation used by the lexical embedder.  ALFWorld text is lowercase words,
#: digits ("mug 1") and punctuation, so a simple alphanumeric scan suffices.
_WORD_RE = re.compile(r'[a-z0-9]+')

#: Dropped before featurising.  These carry the sentence scaffolding shared by
#: every ALFWorld instruction ("put a ... in the ..."), which made unrelated task
#: families look similar -- see LexicalEmbedder.
#:
#: ONLY function words belong here.  The task-type verbs (heat / cool / clean /
#: examine / two / look) are the single most discriminative feature between skill
#: families and must never be dropped, however generic they look.
_STOPWORDS = frozenset("""
a an the this that these those it its they them their
in on at of to from into onto by for with and or but
is are was were be been being am
you your yours i me my we our he she his her
some any all both each few more most other such no nor not only own same so than too very
there here where when why how what which who whom
then now also just still yet even ever never
""".split())

BACKEND_LEXICAL = 'lexical'
BACKEND_REMOTE = 'remote'


def _stable_bucket(feature: str, dim: int) -> int:
    """Map a feature string to a bucket using a process-stable hash."""
    digest = hashlib.blake2b(feature.encode('utf-8'), digest_size=8).digest()
    return int.from_bytes(digest, 'big') % dim


class LexicalEmbedder:
    """Deterministic bag-of-features embedder.

    Features are content-word unigrams plus *within-token* character n-grams,
    hashed into a fixed dimensionality and L2-normalised.

    Two design choices are the result of a measured failure.  A first version
    used cross-word character n-grams over the whole string, and it ranked an
    unrelated pair above a related one:

        sim("put a hot mug in the countertop", "heat some mug and put it in coffeemachine") = 0.36
        sim("put a hot mug in the countertop", "cool a lettuce and place it on the countertop") = 0.46

    because generic phrasing ("put a ... in the ...", "a", "the") dominated the
    feature vector.  Hence:

    *   **Stopwords are dropped and n-grams never span word boundaries**, so
        ``heat``/``heating``/``heated`` still match while sentence scaffolding
        does not contribute.
    *   **Pure-digit tokens are dropped**: in ALFWorld they are instance
        identifiers (``mug 1``, ``cabinet 4``), not task semantics.

    Not subclassing langchain's ``Embeddings`` on purpose: that base class is a
    pydantic model and subclassing it drags in validation behaviour we do not
    want.  LangChain only ever duck-types ``embed_documents`` / ``embed_query``.
    """

    def __init__(self, dim: int = 2048, char_ngram_min: int = 3,
                 char_ngram_max: int = 5, word_weight: float = 1.0,
                 char_weight: float = 0.25, drop_digits: bool = True,
                 **_ignored: Any) -> None:
        if dim <= 0:
            raise ValueError('dim must be positive')
        self.dim = dim
        self.char_ngram_min = char_ngram_min
        self.char_ngram_max = char_ngram_max
        self.word_weight = word_weight
        self.char_weight = char_weight
        self.drop_digits = drop_digits

    @property
    def name(self) -> str:
        return (f'lexical-dim{self.dim}-char{self.char_ngram_min}{self.char_ngram_max}')

    def _content_tokens(self, text: str) -> List[str]:
        toks = _WORD_RE.findall(text.lower())
        out = []
        for t in toks:
            if t in _STOPWORDS:
                continue
            if self.drop_digits and t.isdigit():
                continue
            out.append(t)
        return out

    def _features(self, text: str) -> Dict[str, float]:
        feats: Dict[str, float] = {}
        for tok in self._content_tokens(text):
            key = f'w:{tok}'
            feats[key] = feats.get(key, 0.0) + self.word_weight

        # Character n-grams strictly inside a token, with markers so that short
        # words still yield a full set of grams.
        for tok in self._content_tokens(text):
            padded = '^' + tok + '$'
            for n in range(self.char_ngram_min, self.char_ngram_max + 1):
                for i in range(len(padded) - n + 1):
                    key = f'c{n}:{padded[i:i + n]}'
                    feats[key] = feats.get(key, 0.0) + self.char_weight
        return feats

    def _embed_one(self, text: str) -> List[float]:
        vec = [0.0] * self.dim
        for feature, weight in self._features(text).items():
            vec[_stable_bucket(feature, self.dim)] += weight
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0.0:
            vec = [v / norm for v in vec]
        return vec

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        return [self._embed_one(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._embed_one(text)

    def __repr__(self) -> str:
        return f'<LexicalEmbedder {self.name}>'


class RemoteOpenAIEmbedder:

    def __init__(self, model_name: str, base_url: str, api_key: str = 'unused',
                 batch_size: int = 64, **_ignored: Any) -> None:
        if not base_url:
            raise ValueError('RemoteOpenAIEmbedder requires a base_url')
        import openai  # imported lazily so the lexical path needs no client

        self._openai = openai
        self.model_name = model_name
        self.base_url = base_url
        self.api_key = api_key
        self.batch_size = batch_size
        self.dim: Optional[int] = None

    @property
    def name(self) -> str:
        return f'remote:{self.model_name}'

    def _embed_batch(self, batch: Sequence[str]) -> List[List[float]]:
        resp = self._openai.Embedding.create(
            model=self.model_name,
            input=list(batch),
            api_base=self.base_url,
            api_key=self.api_key,
        )
        # The API does not guarantee ordering; sort by the returned index.
        data = sorted(resp['data'], key=lambda d: d['index'])
        vectors = [list(map(float, d['embedding'])) for d in data]
        if vectors:
            self.dim = len(vectors[0])
        return vectors

    def embed_documents(self, texts: Sequence[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(self._embed_batch(texts[i:i + self.batch_size]))
        return out

    def embed_query(self, text: str) -> List[float]:
        return self._embed_batch([text])[0]

    def __repr__(self) -> str:
        return f'<RemoteOpenAIEmbedder {self.name} @ {self.base_url}>'


def make_embedder_factory(backend: str = BACKEND_LEXICAL,
                          remote_kwargs: Optional[Dict[str, Any]] = None,
                          **lexical_kwargs: Any):
    """Return a callable compatible with ``ExpelAgent``'s ``embedder`` argument.

    ``ExpelAgent.__init__`` calls ``embedder(model_name=embedder_path)`` and
    ``train.py`` passes ``EMBEDDERS(...)`` -- a class.  We pass a closure that
    ignores the model name and returns one cached instance, so a fresh agent per
    task never re-instantiates the backend.
    """
    cache: Dict[str, Any] = {}
    remote_kwargs = dict(remote_kwargs or {})
    # Pop the override before forwarding: passing an explicit model_name plus a
    # model_name inside kwargs would raise TypeError on the remote backend.
    model_override = remote_kwargs.pop('model_override', None)

    def factory(model_name: str = None, **call_kwargs: Any):
        key = model_override or model_name or '<default>'
        if key not in cache:
            if backend == BACKEND_LEXICAL:
                cache[key] = LexicalEmbedder(**lexical_kwargs)
            elif backend == BACKEND_REMOTE:
                cache[key] = RemoteOpenAIEmbedder(model_name=key, **remote_kwargs)
            else:
                raise ValueError(
                    f'unknown embedding backend {backend!r}; '
                    f'expected one of {BACKEND_LEXICAL!r}, {BACKEND_REMOTE!r}')
        return cache[key]

    return factory


def build_from_config(cfg: Any) -> Any:
    """Factory chosen by ``cfg.agent.retrieval_kwargs.embedder_type``.

    Recognised values: ``lexical`` (default) or ``remote``.  The upstream values
    (``huggingface`` / ``openai`` / ``gpt4all`` / ``llama``) are rejected loudly
    rather than silently downloading weights, because this experiment runs
    against a remote server and must not pull checkpoints onto the local host.
    """
    import os

    rk = cfg.agent.retrieval_kwargs
    backend = str(rk['embedder_type']) if 'embedder_type' in rk else BACKEND_LEXICAL

    if backend in ('huggingface', 'openai', 'gpt4all', 'llama'):
        raise ValueError(
            f"embedder_type={backend!r} loads weights from a model hub. This "
            'experiment runs entirely against a remote server, so set '
            "skillexpand.runtime.agent.retrieval_kwargs.embedder_type='lexical' (default) or "
            "'remote' with EXPE_EMBED_BASE_URL pointing at a real service.")

    if backend == BACKEND_REMOTE:
        base = os.environ.get('EXPE_EMBED_BASE_URL')
        if not base:
            raise RuntimeError(
                "embedder_type='remote' requires EXPE_EMBED_BASE_URL to be set")
        return make_embedder_factory(BACKEND_REMOTE, remote_kwargs={
            'base_url': base,
            'api_key': os.environ.get('EXPE_EMBED_API_KEY', 'unused'),
            'model_override': os.environ.get('EXPE_EMBED_MODEL'),
        })

    return make_embedder_factory(BACKEND_LEXICAL)
