from collections.abc import MutableMapping
from contextlib import ExitStack, nullcontext
from hashlib import sha256
import json
from uuid import uuid4


class BatchResultStore(MutableMapping):
    """Persist raw receipts and materialize at most one parsed result at a time."""

    def __init__(self, database, batch_id):
        self.database = database
        self.batch_id = batch_id
        self.run_id = str(uuid4())
        self._connection = None
        self._resources: ExitStack | None = None

    def __enter__(self):
        with ExitStack() as resources:
            owner = self.database.try_acquire_operation_lock(
                "batch-ingest-" + sha256(self.batch_id.encode()).hexdigest()[:3]
            )
            if owner is None:
                raise ValueError("Batch 匯入仍由其他執行者持有，請稍後再試")
            resources.callback(owner.close)
            connection = resources.enter_context(self.database.session())
            connection.execute("DELETE FROM batch_result_staging WHERE batch_id=?", (self.batch_id,))
            self._connection = connection
            self._resources = resources.pop_all()
        return self

    def __exit__(self, *_exc):
        self.close()

    def _session(self):
        return nullcontext(self._connection) if self._connection is not None else self.database.session()

    def save_raw(self, raw_line: bytes, *, source_file: str = "", line_number: int = 0):
        digest = sha256(raw_line).hexdigest()
        with self._session() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO batch_result_raw(batch_id,line_sha256,raw_line) VALUES (?,?,?)",
                (self.batch_id, digest, raw_line),
            )
            occurrence = connection.execute(
                "INSERT OR IGNORE INTO batch_result_occurrences(batch_id,source_file,line_number,line_sha256) VALUES (?,?,?,?)",
                (self.batch_id, source_file, line_number, digest),
            )
            if not occurrence.rowcount:
                previous = connection.execute(
                    "SELECT line_sha256 FROM batch_result_occurrences WHERE batch_id=? AND source_file=? AND line_number=?",
                    (self.batch_id, source_file, line_number),
                ).fetchone()
                if previous[0] != digest:
                    raise ValueError("Batch 結果檔與先前保存的行內容不一致")

    def mark_file_complete(self, source_file: str, line_count: int) -> None:
        with self._session() as connection:
            connection.execute(
                "INSERT INTO batch_result_files(batch_id,source_file,line_count) VALUES (?,?,?) "
                "ON CONFLICT(batch_id,source_file) DO UPDATE SET line_count=excluded.line_count",
                (self.batch_id, source_file, line_count),
            )

    def has_complete_file(self, source_file: str, *, legacy_checkpoint: bool) -> bool:
        with self._session() as connection:
            proof = connection.execute(
                "SELECT line_count FROM batch_result_files WHERE batch_id=? AND source_file=?",
                (self.batch_id, source_file),
            ).fetchone()
            if proof is None and not legacy_checkpoint:
                return False
            count, first, last = connection.execute(
                """SELECT COUNT(*),MIN(o.line_number),MAX(o.line_number)
                   FROM batch_result_occurrences o
                   JOIN batch_result_raw r ON r.batch_id=o.batch_id AND r.line_sha256=o.line_sha256
                   WHERE o.batch_id=? AND o.source_file=?""",
                (self.batch_id, source_file),
            ).fetchone()
        if proof is not None and int(proof[0]) != count:
            return False
        if count == 0:
            return proof is not None
        return first == 1 and last == count

    def raw_lines(self, source_file: str):
        after = 0
        while True:
            with self._session() as connection:
                rows = connection.execute(
                    """SELECT o.line_number,r.raw_line FROM batch_result_occurrences o
                       JOIN batch_result_raw r ON r.batch_id=o.batch_id AND r.line_sha256=o.line_sha256
                       WHERE o.batch_id=? AND o.source_file=? AND o.line_number>?
                       ORDER BY o.line_number LIMIT 8""",
                    (self.batch_id, source_file, after),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                yield int(row[0]), row[1]
            after = int(rows[-1][0])

    def __getitem__(self, key):
        with self._session() as connection:
            row = connection.execute(
                "SELECT payload_json FROM batch_result_staging WHERE batch_id=? AND run_id=? AND custom_id=?",
                (self.batch_id, self.run_id, key),
            ).fetchone()
        if row is None:
            raise KeyError(key)
        return tuple(json.loads(row[0]))

    def __contains__(self, key):
        with self._session() as connection:
            return connection.execute(
                "SELECT 1 FROM batch_result_staging WHERE batch_id=? AND run_id=? AND custom_id=?",
                (self.batch_id, self.run_id, key),
            ).fetchone() is not None

    def __setitem__(self, key, value):
        with self._session() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO batch_result_staging(batch_id,run_id,custom_id,payload_json) VALUES (?,?,?,?)",
                (self.batch_id, self.run_id, key, json.dumps(value, ensure_ascii=False)),
            )

    def __delitem__(self, key):
        with self._session() as connection:
            connection.execute(
                "DELETE FROM batch_result_staging WHERE batch_id=? AND run_id=? AND custom_id=?",
                (self.batch_id, self.run_id, key),
            )

    def __iter__(self):
        after = ""
        while True:
            with self._session() as connection:
                rows = connection.execute(
                    "SELECT custom_id FROM batch_result_staging WHERE batch_id=? AND run_id=? "
                    "AND custom_id>? ORDER BY custom_id LIMIT 200",
                    (self.batch_id, self.run_id, after),
                ).fetchall()
            if not rows:
                return
            for row in rows:
                yield str(row[0])
            after = str(rows[-1][0])

    def __len__(self):
        with self._session() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM batch_result_staging WHERE batch_id=? AND run_id=?",
                (self.batch_id, self.run_id),
            ).fetchone()[0])

    def close(self):
        try:
            with self._session() as connection:
                connection.execute("DELETE FROM batch_result_staging WHERE batch_id=? AND run_id=?",
                                   (self.batch_id, self.run_id))
        finally:
            resources, self._resources = self._resources, None
            self._connection = None
            if resources is not None:
                resources.close()
