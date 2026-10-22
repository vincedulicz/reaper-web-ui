from .base import DawBackend


def create_backend(name: str) -> DawBackend:
    """Factory: backends are imported lazily so 'mock' works without python-reapy installed."""
    if name == "mock":
        from .mock_backend import MockBackend

        return MockBackend()
    if name == "reapy":
        from .reapy_backend import ReapyBackend

        return ReapyBackend()
    raise ValueError(f"Unknown backend: {name!r}")
