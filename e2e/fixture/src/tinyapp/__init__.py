"""Intentionally small product for autonomous E2E delivery tests."""


def add(left: int, right: int) -> int:
    return left + right


def health() -> tuple[int, dict[str, str]]:
    """Known-imperfect flow: the seeded Issue asks the agent to implement this."""
    return 501, {"status": "not-implemented"}
