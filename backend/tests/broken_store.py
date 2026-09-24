"""A store whose every call fails, to test each feature's failure policy."""

from app.cache.store import Counter, StoreUnavailableError


class BrokenStore:
    async def get(self, key: str) -> bytes | None:
        raise StoreUnavailableError("ConnectionError")

    async def set(
        self, key: str, value: bytes, *, ttl_seconds: float, only_if_absent: bool = False
    ) -> bool:
        raise StoreUnavailableError("ConnectionError")

    async def get_and_delete(self, key: str) -> bytes | None:
        raise StoreUnavailableError("ConnectionError")

    async def delete(self, key: str) -> None:
        raise StoreUnavailableError("ConnectionError")

    async def increment(self, key: str, *, ttl_seconds: float) -> Counter:
        raise StoreUnavailableError("ConnectionError")

    async def ping(self) -> bool:
        return False

    async def close(self) -> None:
        return None
