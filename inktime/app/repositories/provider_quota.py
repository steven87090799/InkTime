"""Account-scoped quota and circuit state shared by worker processes."""
import time
from uuid import uuid4


class ProviderQuotaRepository:
    def __init__(self, database, scope):
        self.database = database
        self.scope = scope

    def _allowed(self, connection, channel, now):
        state = connection.execute(
            "SELECT circuit_until FROM provider_quota_state WHERE scope=?", (self.scope,),
        ).fetchone()
        if state and float(state[0]) > now:
            return False
        row = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(tokens),0),COALESCE(SUM(lease_until>?),0) "
            "FROM provider_quota_events WHERE scope=? AND (started_at>? OR lease_until>?)",
            (now, self.scope, now - 60, now),
        ).fetchone()
        return (
            (not channel.requests_per_minute or row[0] < channel.requests_per_minute)
            and (not channel.tokens_per_minute or row[1] + channel.request_token_reserve <= channel.tokens_per_minute)
            and row[2] < channel.max_concurrency
        )

    def available(self, channel):
        with self.database.session() as connection:
            return self._allowed(connection, channel, time.time())

    def acquire(self, channel):
        now = time.time()
        with self.database.transaction(operation="provider_quota_acquire") as connection:
            connection.execute(
                "DELETE FROM provider_quota_events WHERE scope=? AND started_at<=? AND lease_until<=?",
                (self.scope, now - 60, now),
            )
            if not self._allowed(connection, channel, now):
                return None
            identifier = str(uuid4())
            connection.execute(
                "INSERT INTO provider_quota_events(id,scope,started_at,tokens,lease_until) VALUES (?,?,?,?,?)",
                (identifier, self.scope, now, channel.request_token_reserve,
                 now + max(1, float(getattr(channel.provider, 'timeout', 120))) + 60),
            )
            return identifier

    def release(self, identifier, channel, *, usage, error, failure_threshold):
        now = time.time()
        with self.database.transaction(operation="provider_quota_release") as connection:
            tokens = usage.input_tokens + usage.output_tokens if usage and usage.tokens_reported else channel.request_token_reserve
            connection.execute(
                "UPDATE provider_quota_events SET lease_until=0,started_at=?,tokens=? WHERE id=? AND scope=?",
                (now, tokens, identifier, self.scope),
            )
            connection.execute(
                "INSERT OR IGNORE INTO provider_quota_state(scope,failures,circuit_until) VALUES (?,0,0)",
                (self.scope,),
            )
            if error is None:
                connection.execute("UPDATE provider_quota_state SET failures=0 WHERE scope=?", (self.scope,))
            else:
                connection.execute("UPDATE provider_quota_state SET failures=failures+1 WHERE scope=?", (self.scope,))
                failures = connection.execute("SELECT failures FROM provider_quota_state WHERE scope=?", (self.scope,)).fetchone()[0]
                retry_after = float(getattr(error, "retry_after", None) or 0)
                if failures >= failure_threshold or retry_after:
                    connection.execute(
                        "UPDATE provider_quota_state SET circuit_until=MAX(circuit_until,?) WHERE scope=?",
                        (now + max(retry_after, channel.cooldown_seconds), self.scope),
                    )
