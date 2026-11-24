"""Serialize this node's short write transactions, without serializing readers."""
import sqlite3
import threading
from collections import defaultdict

_writers = defaultdict(threading.RLock)
_registry_lock = threading.Lock()


def busy(error):
    return isinstance(error, sqlite3.OperationalError) and (
        getattr(error, "sqlite_errorcode", 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
        or str(error) in {"database is locked", "database table is locked"}
    )


class WriteConnection(sqlite3.Connection):
    def bind(self, path):
        with _registry_lock:
            self._writer = _writers[str(path)]
        self._claimed = False
        return self

    def _claim(self):
        if not self._claimed:
            self._writer.acquire()
            self._claimed = True

    def _release(self):
        if self._claimed:
            self._claimed = False
            self._writer.release()

    def execute(self, sql, parameters=()):
        verb = sql.lstrip().split(None, 1)[0].upper()
        if verb in {"BEGIN", "INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER"}:
            self._claim()
        try:
            return super().execute(sql, parameters)
        except BaseException:
            if not self.in_transaction:
                self._release()
            raise

    def executemany(self, sql, parameters):
        self._claim()
        try:
            return super().executemany(sql, parameters)
        except BaseException:
            if not self.in_transaction:
                self._release()
            raise

    def executescript(self, sql):
        self._claim()
        try:
            return super().executescript(sql)
        finally:
            if not self.in_transaction:
                self._release()

    def commit(self):
        try:
            return super().commit()
        finally:
            if not self.in_transaction:
                self._release()

    def rollback(self):
        try:
            return super().rollback()
        finally:
            self._release()

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self._release()

    def close(self):
        try:
            return super().close()
        finally:
            self._release()
