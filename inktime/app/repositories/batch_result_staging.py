from collections.abc import MutableMapping
from hashlib import sha256
import json
from uuid import uuid4


class BatchResultStore(MutableMapping):
    """Persist raw receipts and materialize at most one parsed result at a time."""

    def __init__(self, database, batch_id):
        self.database = database
        self.batch_id = batch_id
        self.run_id = str(uuid4())

    def save_raw(self, raw_line: bytes):
        with self.database.session() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO batch_result_raw(batch_id,line_sha256,raw_line) VALUES (?,?,?)",
                (self.batch_id, sha256(raw_line).hexdigest(), raw_line),
            )

    def __getitem__(self, key):
        with self.database.session() as connection:
            row = connection.execute(
                "SELECT payload_json FROM batch_result_staging WHERE batch_id=? AND run_id=? AND custom_id=?",
                (self.batch_id, self.run_id, key),
            ).fetchone()
        if row is None:
            raise KeyError(key)
        return tuple(json.loads(row[0]))

    def __contains__(self, key):
        with self.database.session() as connection:
            return connection.execute(
                "SELECT 1 FROM batch_result_staging WHERE batch_id=? AND run_id=? AND custom_id=?",
                (self.batch_id, self.run_id, key),
            ).fetchone() is not None

    def __setitem__(self, key, value):
        with self.database.session() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO batch_result_staging(batch_id,run_id,custom_id,payload_json) VALUES (?,?,?,?)",
                (self.batch_id, self.run_id, key, json.dumps(value, ensure_ascii=False)),
            )

    def __delitem__(self, key):
        with self.database.session() as connection:
            connection.execute(
                "DELETE FROM batch_result_staging WHERE batch_id=? AND run_id=? AND custom_id=?",
                (self.batch_id, self.run_id, key),
            )

    def __iter__(self):
        with self.database.session() as connection:
            cursor = connection.execute(
                "SELECT custom_id FROM batch_result_staging WHERE batch_id=? AND run_id=? ORDER BY custom_id",
                (self.batch_id, self.run_id),
            )
            for row in cursor:
                yield str(row[0])

    def __len__(self):
        with self.database.session() as connection:
            return int(connection.execute(
                "SELECT COUNT(*) FROM batch_result_staging WHERE batch_id=? AND run_id=?",
                (self.batch_id, self.run_id),
            ).fetchone()[0])

    def close(self):
        with self.database.session() as connection:
            connection.execute("DELETE FROM batch_result_staging WHERE batch_id=? AND run_id=?",
                               (self.batch_id, self.run_id))
