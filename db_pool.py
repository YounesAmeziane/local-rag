# db_pool.py
# Bounded, thread-safe pyodbc connection pool. pyodbc has no pooling of its own;
# opening a fresh connection per call is real overhead (network handshake, auth)
# and becomes a real bottleneck once concurrent users can trigger overlapping SQL
# calls (multi-user server, not the single-user laptop this was built on).

import contextlib
import queue
import threading

import pyodbc

import config

_idle: "queue.Queue[pyodbc.Connection]" = queue.Queue()
_sem = threading.Semaphore(config.DB_POOL_SIZE)


def _new_connection(conn_str: str) -> pyodbc.Connection:
    conn = pyodbc.connect(conn_str, timeout=30)
    conn.timeout = 30
    return conn


def _is_alive(conn: pyodbc.Connection) -> bool:
    try:
        conn.cursor().execute("SELECT 1")
        return True
    except Exception:
        return False


@contextlib.contextmanager
def connection(conn_str_factory):
    """Checkout a pooled connection for one `with` block.

    Bounded to config.DB_POOL_SIZE concurrent connections (callers beyond that
    block until one frees up, rather than opening unbounded extra connections).
    Validates on checkout with a cheap SELECT 1 and drops+recreates a dead
    connection instead of handing a broken one to the caller. `conn_str_factory`
    is only invoked when a fresh connection is actually needed, so a config
    change (e.g. DB_READONLY_USER set mid-run) takes effect on the next new
    connection rather than being baked in early.
    """
    _sem.acquire()
    conn = None
    try:
        while conn is None:
            try:
                conn = _idle.get_nowait()
            except queue.Empty:
                conn = _new_connection(conn_str_factory())
                break
            if not _is_alive(conn):
                try:
                    conn.close()
                except Exception:
                    pass
                conn = None

        try:
            yield conn
        except Exception:
            # Don't return a connection that errored mid-use to the pool.
            try:
                conn.close()
            except Exception:
                pass
            conn = None
            raise
    finally:
        if conn is not None:
            _idle.put(conn)
        _sem.release()
