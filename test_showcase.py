"""Exercise the original router with a temporary library and synthetic identities."""
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from reader_sync import _read_pages, ensure_tables, get_router


class ReadingShowcase(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.sql = []
        with self.db() as conn:
            conn.executescript("""
                CREATE TABLE users(id INTEGER PRIMARY KEY);
                INSERT INTO users VALUES(1), (2);
                CREATE TABLE items(id INTEGER PRIMARY KEY, slug TEXT UNIQUE,
                    category TEXT, content_kind TEXT, reader_url TEXT);
            """)
            ensure_tables(conn)
        app = FastAPI()
        # Both identities exist only inside TestClient; no network server is started.
        router = get_router(get_db=self.db, reader_root=lambda: self.root,
                            get_current_user=lambda request, conn: {'id': self.account},
                            same_origin=lambda request: True)
        app.include_router(router)
        self.account = 1
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    @contextmanager
    def db(self, immediate=False):
        conn = sqlite3.connect(self.root / 'demo.sqlite')
        conn.row_factory = sqlite3.Row
        conn.set_trace_callback(self.sql.append)
        try:
            with conn:
                if immediate:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
        finally:
            conn.close()

    def seed(self, count):
        with self.db() as conn:
            for index in range(count):
                slug = f'book-{index}'
                path = self.root / 'reader' / slug / 'index.html'
                path.parent.mkdir(parents=True)
                path.write_text(f'<body data-reader-slug="{slug}"><img class="comic-img" src="page.svg"></body>')
                item = conn.execute('INSERT INTO items(slug,category,content_kind,reader_url) VALUES(?,?,?,?)',
                                    (slug, '漫画', 'book', f'/reader/{slug}')).lastrowid
                fingerprint = sha256(b'page.svg').hexdigest()
                conn.execute('INSERT INTO user_reader_positions VALUES(?,?,?,?,?,?,?,?)',
                             (1, item, 1, fingerprint, 1, 1, 0.25, index))

    def test_shelf_query_count_and_cache(self):
        self.seed(140)
        self.sql.clear()
        endpoint = '/api/user/reading'
        rows = self.client.get(endpoint).json()['records']
        self.assertEqual([r['slug'] for r in rows], [f'book-{n}' for n in reversed(range(140))])
        self.assertEqual(sum('JOIN items ' in query for query in self.sql), 1)
        misses = _read_pages.cache_info().misses
        self.assertEqual(self.client.get(endpoint).json()['records'], rows)
        self.assertEqual(_read_pages.cache_info().misses, misses)

    def test_account_isolation_and_stale_write(self):
        self.seed(1)
        endpoint = '/api/user/reading/book-0'
        book = self.client.get(endpoint).json()
        write = dict(user_id=1, revision=0, content=book['content'], page=1, total_pages=1, offset=0.75)
        self.assertEqual(self.client.put(endpoint, json=write).status_code, 409)
        self.assertEqual(self.client.get(endpoint).json()['progress']['offset'], 0.25)
        write['revision'] = book['revision']
        self.assertEqual(self.client.put(endpoint, json=write).status_code, 200)
        self.account = 2
        self.assertIsNone(self.client.get(endpoint).json()['progress'])


if __name__ == '__main__':
    unittest.main()
