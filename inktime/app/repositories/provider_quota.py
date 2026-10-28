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
            "FROM (SELECT tokens,lease_until FROM provider_quota_events WHERE scope=? AND started_at>? "
            "UNION ALL SELECT tokens,lease_until FROM provider_quota_events "
            "WHERE scope=? AND lease_until>? AND started_at<=?)",
            (now, self.scope, now - 60, self.scope, now, now - 60),
        ).fetchone()
        return (
            (not channel.requests_per_minute or row[0] < channel.requests_per_minute)
            and (not channel.tokens_per_minute or row[1] + channel.request_token_reserve <= channel.tokens_per_minute)
            and row[2] < channel.max_concurrency
        )

    def available(self, channel):
        with self.database.session() as connection:
            return self._allowed(connection, channel, time.time())

    def retry_after(self, channel):
        """Return a bounded polling delay without acquiring a quota slot."""
        now = time.time()
        with self.database.session() as connection:
            state = connection.execute(
                "SELECT circuit_until FROM provider_quota_state WHERE scope=?", (self.scope,),
            ).fetchone()
            if state and float(state[0]) > now:
                return max(1.0, float(state[0]) - now)
            events = connection.execute(
                "SELECT started_at,tokens FROM provider_quota_events WHERE scope=? AND started_at>? "
                "ORDER BY started_at", (self.scope, now - 60),
            ).fetchall()
            rpm_full = channel.requests_per_minute and len(events) >= channel.requests_per_minute
            tpm_full = channel.tokens_per_minute and (
                sum(row[1] for row in events) + channel.request_token_reserve >= channel.tokens_per_minute
            )
            if events and (rpm_full or tpm_full):
                return max(1.0, float(events[0][0]) + 60 - now)
        # An in-flight request may finish before its crash-recovery lease.
        return 5.0

    def acquire(self, channel):
        now = time.time()
        with self.database.transaction(operation="provider_quota_acquire") as connection:
            connection.execute(
                "DELETE FROM provider_quota_events WHERE lease_until<=? AND started_at<=?",
                (now, now - 60),
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

    def cancel(self, identifier):
        """Cancel an unsent model reservation without changing circuit state."""
        with self.database.transaction(operation="provider_quota_cancel") as connection:
            connection.execute(
                "DELETE FROM provider_quota_events WHERE id=? AND scope=?", (identifier, self.scope),
            )

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
