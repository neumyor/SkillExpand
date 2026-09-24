"""Deterministic lightweight embedding dependency checks."""

import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Tuple

from skillexpand.runtime import embedders as E

FAILURES: List[str] = []

def check(name: str, fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(f'{name}: {type(exc).__name__}: {exc}')
        print(f'  FAIL  {name}\n        {type(exc).__name__}: {exc}')
    else:
        print(f'  ok    {name}')


def cos(a: List[float], b: List[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return num / (na * nb) if na and nb else 0.0


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_similarity_ordering_is_fixed() -> None:
    """Regression test for the bug that motivated the stopword design.

    An unrelated pair used to outscore the related one because cross-word
    character n-grams encoded "put a ... in the ...".
    """
    emb = E.LexicalEmbedder()
    a = emb.embed_query('put a hot mug in the countertop')
    similar = emb.embed_query('heat some mug and put it in coffeemachine')
    unrelated = emb.embed_query('cool a lettuce and place it on the countertop')
    assert cos(a, similar) > cos(a, unrelated), (
        'related pair must outscore unrelated pair\n'
        f'  related   {cos(a, similar):.4f}\n'
        f'  unrelated {cos(a, unrelated):.4f}')


def test_vectors_are_normalised() -> None:
    emb = E.LexicalEmbedder()
    for text in ['', 'mug', 'heat the mug then put it in the fridge', '12345']:
        v = emb.embed_query(text)
        assert len(v) == emb.dim, (len(v), emb.dim)
        norm = sum(x * x for x in v) ** 0.5
        assert abs(norm - 1.0) < 1e-9 or norm == 0.0, (text, norm)


def test_empty_text_is_zero_vector_not_nan() -> None:
    """A blank query must not produce NaN and poison every comparison."""
    emb = E.LexicalEmbedder()
    v = emb.embed_query('   ')
    assert all(x == 0.0 for x in v), v[:5]


def test_task_type_verbs_are_not_stopwords() -> None:
    """heat/cool/clean/look are the discriminative signal for ALFWorld."""
    for verb in ['heat', 'cool', 'clean', 'examine', 'two', 'look']:
        assert verb not in E._STOPWORDS, (
            f'{verb!r} was dropped as a stopword; it separates skill families')


def test_determinism_within_process() -> None:
    emb = E.LexicalEmbedder()
    a = emb.embed_query('heat some mug and put it in coffeemachine')
    b = emb.embed_query('heat some mug and put it in coffeemachine')
    assert a == b


def test_determinism_across_processes() -> None:
    """Feature hashing must not depend on PYTHONHASHSEED.

    Python salts str.__hash__ per process.  Using the built-in hash() to bucket
    features would silently change every embedding between runs, so the same
    experiment would make different retrieval decisions on a re-run.
    """
    code = (
        'from skillexpand.runtime.embedders import LexicalEmbedder;'
        'v=LexicalEmbedder().embed_query("heat some mug");'
        'print(sum(v), v[:4])'
    )
    outputs = []
    for seed in ('0', '1', '12345'):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        res = subprocess.run([sys.executable, '-c', code], capture_output=True,
                             text=True, env=env, cwd=os.getcwd())
        assert res.returncode == 0, res.stderr[-500:]
        outputs.append(res.stdout.strip())
    assert len(set(outputs)) == 1, (
        'embedding changed with PYTHONHASHSEED; the bucketing hash is not stable\n'
        + '\n'.join(outputs))


def test_factory_shares_one_instance() -> None:
    """A fresh executor per task must not rebuild the backend each time."""
    factory = E.make_embedder_factory(E.BACKEND_LEXICAL)
    assert factory(model_name='all-mpnet-base-v2') is factory(model_name='all-mpnet-base-v2')


def test_factory_rejects_weight_downloading_backends() -> None:
    from types import SimpleNamespace

    for backend in ('huggingface', 'openai', 'gpt4all', 'llama'):
        cfg = SimpleNamespace(
            agent=SimpleNamespace(retrieval_kwargs={'embedder_type': backend}))
        try:
            E.build_from_config(cfg)
        except ValueError as exc:
            assert 'weights' in str(exc), str(exc)
        else:
            raise AssertionError(f'{backend} should have been rejected')


def test_build_from_config_requires_base_url_for_remote() -> None:
    """Selecting the remote backend without an endpoint must fail loudly."""
    from types import SimpleNamespace

    saved = os.environ.pop('EXPE_EMBED_BASE_URL', None)
    try:
        cfg = SimpleNamespace(
            agent=SimpleNamespace(retrieval_kwargs={'embedder_type': 'remote'}))
        try:
            E.build_from_config(cfg)
        except RuntimeError as exc:
            assert 'EXPE_EMBED_BASE_URL' in str(exc), str(exc)
        else:
            raise AssertionError('remote backend without a base URL should fail')
    finally:
        if saved is not None:
            os.environ['EXPE_EMBED_BASE_URL'] = saved


def test_remote_kwargs_do_not_collide_on_model_name() -> None:
    """A model_override must not be forwarded alongside an explicit model_name."""
    factory = E.make_embedder_factory(
        E.BACKEND_REMOTE,
        remote_kwargs={'base_url': 'http://127.0.0.1:1/v1', 'model_override': 'override-name'})
    try:
        emb = factory(model_name='from-config')
    except TypeError as exc:
        raise AssertionError(f'model_name collided in kwargs: {exc}') from exc
    assert emb.model_name == 'override-name', emb.model_name


TESTS = [
    test_similarity_ordering_is_fixed,
    test_vectors_are_normalised,
    test_empty_text_is_zero_vector_not_nan,
    test_task_type_verbs_are_not_stopwords,
    test_determinism_within_process,
    test_determinism_across_processes,
    test_factory_shares_one_instance,
    test_factory_rejects_weight_downloading_backends,
    test_build_from_config_requires_base_url_for_remote,
    test_remote_kwargs_do_not_collide_on_model_name,
]


def main() -> int:
    print(f'skillexpand.runtime.embedders tests (python {sys.version.split()[0]})')
    for fn in TESTS:
        check(fn.__name__, fn)
    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILED of {len(TESTS)}')
        for f in FAILURES:
            print(f'  - {f}')
        return 1
    print(f'all {len(TESTS)} passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
