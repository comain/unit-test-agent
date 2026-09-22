"""SQLite adapter for repository-scoped Java hanging-test observations."""

from datetime import datetime, timedelta, timezone

from uta.tasks.db import TaskDB


class SQLiteHangingTestQuarantine:
    def __init__(self, db_path=None):
        self.db = TaskDB(db_path)

    def active(self, repo_slug: str, ttl_days: int, *, now=None) -> list[str]:
        self.db.init()
        cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=ttl_days)).isoformat()
        with self.db.connect() as conn:
            return [
                row[0]
                for row in conn.execute(
                    """SELECT test_class FROM java_hanging_tests
                       WHERE repo_slug=? AND last_seen_at>?
                       ORDER BY test_class""",
                    (repo_slug, cutoff),
                )
            ]

    def observe(
        self,
        repo_slug: str,
        test_class: str,
        module: str,
        *,
        now=None,
    ) -> None:
        self.db.init()
        stamp = (now or datetime.now(timezone.utc)).isoformat()
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO java_hanging_tests
                   (repo_slug,test_class,module,first_seen_at,last_seen_at,observations)
                   VALUES (?,?,?,?,?,1)
                   ON CONFLICT(repo_slug,test_class) DO UPDATE SET
                   module=excluded.module,last_seen_at=excluded.last_seen_at,
                   observations=java_hanging_tests.observations+1""",
                (repo_slug, test_class, module, stamp, stamp),
            )
