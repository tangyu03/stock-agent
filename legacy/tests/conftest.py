"""Global test fixtures for isolation."""
import pytest


@pytest.fixture(autouse=True)
def clean_institutional_cache():
    """Clear institutional scorer global cache before and after each test."""
    try:
        import src.analyzers.institutional_scorer as inst
        inst._institutional_session_cache.clear()
    except Exception:
        pass
    yield
    try:
        import src.analyzers.institutional_scorer as inst
        inst._institutional_session_cache.clear()
    except Exception:
        pass
