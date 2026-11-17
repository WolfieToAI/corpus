import random

import pytest

from corpus.core.api_keys import generate_api_key, hash_secret, parse_api_key, verify_secret
from corpus.jobs.queue import compute_backoff


def test_generated_key_round_trips() -> None:
    key = generate_api_key()
    assert key.plaintext.startswith(f"crp_{key.prefix}_")
    parsed = parse_api_key(key.plaintext)
    assert parsed is not None and parsed.prefix == key.prefix
    assert verify_secret(parsed.secret, key.secret_hash)
    assert key.secret_hash == hash_secret(parsed.secret)
    assert parsed.secret not in key.secret_hash  # only the digest is stored


def test_keys_are_unique() -> None:
    keys = {generate_api_key().plaintext for _ in range(200)}
    assert len(keys) == 200


def test_wrong_secret_is_rejected() -> None:
    a, b = generate_api_key(), generate_api_key()
    parsed_b = parse_api_key(b.plaintext)
    assert parsed_b is not None
    assert not verify_secret(parsed_b.secret, a.secret_hash)


@pytest.mark.parametrize(
    "value",
    ["", "crp_short_x", "abc_" + "a" * 12 + "_" + "b" * 43, "crp_" + "a" * 12 + "_" + "b" * 42],
)
def test_malformed_keys_do_not_parse(value: str) -> None:
    assert parse_api_key(value) is None


def test_backoff_grows_exponentially_within_jitter_bounds() -> None:
    rng = random.Random(42)
    for attempt, ceiling in [(1, 5), (2, 10), (3, 20), (4, 40)]:
        delay = compute_backoff(attempt, base=5, cap=1000, rng=rng)
        assert ceiling / 2 <= delay <= ceiling


def test_backoff_is_capped() -> None:
    assert compute_backoff(30, base=5, cap=600, rng=random.Random(0)) <= 600


def test_backoff_rejects_zero_based_attempts() -> None:
    with pytest.raises(ValueError):
        compute_backoff(0, base=1, cap=10)
