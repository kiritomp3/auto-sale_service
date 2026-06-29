from __future__ import annotations

from datetime import datetime, timedelta, timezone

import redis

from app.models import AvitoAccount, AvitoQueuedListing, AvitoSession


class RedisAvitoRepository:
    def __init__(self, redis_client: redis.Redis, key_prefix: str = "avito"):
        self._redis = redis_client
        self._key_prefix = key_prefix.rstrip(":")

    def _account_index_key(self) -> str:
        return f"{self._key_prefix}:accounts"

    def _account_key(self, account_id: str) -> str:
        return f"{self._key_prefix}:account:{account_id}"

    def _session_key(self, token: str) -> str:
        return f"{self._key_prefix}:session:{token}"

    def _listing_key(self, account_id: str, listing_id: str) -> str:
        return f"{self._key_prefix}:listing:{account_id}:{listing_id}"

    def _queue_key(self, account_id: str) -> str:
        return f"{self._key_prefix}:queue:{account_id}"

    @staticmethod
    def _decode(raw: bytes | str) -> str:
        return raw.decode("utf-8") if isinstance(raw, bytes) else raw

    def save_account(self, account: AvitoAccount) -> None:
        self._redis.set(self._account_key(account.account_id), account.model_dump_json())
        self._redis.sadd(self._account_index_key(), account.account_id)

    def get_account(self, account_id: str) -> AvitoAccount | None:
        raw = self._redis.get(self._account_key(account_id))
        if raw is None:
            return None
        return AvitoAccount.model_validate_json(self._decode(raw))

    def list_account_ids(self) -> list[str]:
        raw_ids = self._redis.smembers(self._account_index_key())
        return sorted(self._decode(raw_id) for raw_id in raw_ids)

    def save_session(self, session: AvitoSession, ttl_seconds: int) -> None:
        self._redis.set(self._session_key(session.token), session.model_dump_json(), ex=ttl_seconds)

    def get_valid_session(self, token: str) -> AvitoSession | None:
        raw = self._redis.get(self._session_key(token))
        if raw is None:
            return None
        session = AvitoSession.model_validate_json(self._decode(raw))
        if session.expires_at <= datetime.now(timezone.utc):
            self.delete_session(token)
            return None
        return session

    def delete_session(self, token: str) -> None:
        self._redis.delete(self._session_key(token))

    @staticmethod
    def build_expiry(ttl_seconds: int) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    def append_listing(self, listing: AvitoQueuedListing) -> None:
        self.save_listing(listing)
        self._redis.rpush(self._queue_key(listing.account_id), listing.listing_id)

    def save_listing(self, listing: AvitoQueuedListing) -> None:
        self._redis.set(
            self._listing_key(listing.account_id, listing.listing_id),
            listing.model_dump_json(),
        )

    def get_listing(self, account_id: str, listing_id: str) -> AvitoQueuedListing | None:
        raw = self._redis.get(self._listing_key(account_id, listing_id))
        if raw is None:
            return None
        return AvitoQueuedListing.model_validate_json(self._decode(raw))

    def list_listing_ids(self, account_id: str) -> list[str]:
        raw_ids = self._redis.lrange(self._queue_key(account_id), 0, -1)
        return [self._decode(raw_id) for raw_id in raw_ids]

    def list_listings(self, account_id: str) -> list[AvitoQueuedListing]:
        listings: list[AvitoQueuedListing] = []
        for listing_id in self.list_listing_ids(account_id):
            listing = self.get_listing(account_id, listing_id)
            if listing is not None:
                listings.append(listing)
        return listings

    def replace_listing_order(self, account_id: str, listing_ids: list[str]) -> None:
        pipe = self._redis.pipeline()
        pipe.delete(self._queue_key(account_id))
        for listing_id in listing_ids:
            pipe.rpush(self._queue_key(account_id), listing_id)
        pipe.execute()
