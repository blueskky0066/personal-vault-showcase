"""Account reading settings and per-resource positions."""
from functools import lru_cache
from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
import re
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt

HEADERS = {'Cache-Control': 'private, no-store', 'Vary': 'Cookie'}
SLUG = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}')
MAX_REVISION = 9007199254740991
MAX_PAGES = 3000
RECENT_LIMIT = 500
# Not a page fingerprint. A reset retains only the revision needed to reject stale devices.
DELETED_CONTENT = "-" * 64


def ensure_tables(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS user_reader_settings (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL CHECK(revision > 0),
        mode TEXT NOT NULL CHECK(mode IN ('pager','scroll')),
        fit TEXT NOT NULL CHECK(fit IN ('page','width')),
        theme TEXT NOT NULL CHECK(theme IN ('paper','mist','sage','blue','rose','dark')),
        background TEXT NOT NULL CHECK(background IN ('solid','glow'))
    )''')
    conn.execute('''CREATE TABLE IF NOT EXISTS user_reader_positions (
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL CHECK(revision > 0),
        content TEXT NOT NULL CHECK(length(content)=64),
        page INTEGER NOT NULL CHECK(page > 0 AND page <= total_pages),
        total_pages INTEGER NOT NULL CHECK(total_pages BETWEEN 1 AND 3000),
        offset REAL NOT NULL CHECK(offset BETWEEN 0 AND 1),
        updated_at INTEGER NOT NULL,
        PRIMARY KEY(user_id, item_id)
    )''')
    conn.execute('''CREATE INDEX IF NOT EXISTS user_reader_positions_recent
                    ON user_reader_positions(user_id, updated_at DESC, item_id DESC)''')


class WriteIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    user_id: StrictInt = Field(gt=0, le=MAX_REVISION)
    revision: StrictInt = Field(ge=0, lt=MAX_REVISION)


class SettingsIn(WriteIn):
    mode: Literal['pager', 'scroll']
    fit: Literal['page', 'width']
    theme: Literal['paper', 'mist', 'sage', 'blue', 'rose', 'dark']
    background: Literal['solid', 'glow']


class PositionIn(WriteIn):
    content: str = Field(pattern=r'^[a-f0-9]{64}$', min_length=64, max_length=64)
    page: StrictInt = Field(ge=1, le=MAX_PAGES)
    total_pages: StrictInt = Field(ge=1, le=MAX_PAGES)
    offset: float = Field(strict=True, ge=0, le=1, allow_inf_nan=False)


class ReaderPages(HTMLParser):
    def __init__(self):
        super().__init__()
        self.slug = None
        self.images = []

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == 'body':
            self.slug = fields.get('data-reader-slug')
        if tag == 'img' and 'comic-img' in (fields.get('class') or '').split():
            self.images.append(fields.get('src', ''))


@lru_cache(maxsize=RECENT_LIMIT)
def _read_pages(path, mtime, ctime, inode, size):
    # Stat fields key the cache across regenerated/replaced reader documents.
    parser = ReaderPages()
    with path.open(encoding='utf-8') as stream:
        parser.feed(stream.read(8 * 1024 * 1024 + 1))
    if not 1 <= len(parser.images) <= MAX_PAGES or not all(parser.images):
        raise ValueError('invalid reader document')
    content = sha256('\n'.join(parser.images).encode()).hexdigest()
    return parser.slug, content, len(parser.images)


def get_router(*, get_db, get_current_user, same_origin, reader_root):
    router = APIRouter(prefix='/api/user/reading')

    def document(slug):
        if not SLUG.fullmatch(slug):
            raise HTTPException(404, '阅读资源不存在', headers=HEADERS)
        path = Path(reader_root()) / 'reader' / slug / 'index.html'
        try:
            if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
                raise ValueError('unsupported reader path')
            stat = path.stat()
            if stat.st_size > 8 * 1024 * 1024:
                raise ValueError('oversized reader document')
            owner, content, total = _read_pages(path, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino, stat.st_size)
            if owner != slug:
                raise ValueError('reader owner mismatch')
        except (OSError, ValueError):
            raise HTTPException(404, '阅读资源不存在', headers=HEADERS) from None
        return content, total

    # Both the shelf and the single-book routes accept only a book's own local reader.
    readable = """i.content_kind!='image' AND i.category IN ('漫画','图包')
                  AND rtrim(i.reader_url, '/')='/reader/' || i.slug"""

    def info(conn, slug):
        item = conn.execute(f'SELECT i.id FROM items i WHERE i.slug=? AND {readable}', (slug,)).fetchone()
        if item is None:
            raise HTTPException(404, '阅读资源不存在', headers=HEADERS)
        content, total = document(slug)
        return item['id'], content, total

    def principal(request, conn, expected=None):
        try:
            uid = get_current_user(request, conn=conn)['id']
        except HTTPException as exc:
            exc.headers = {**(exc.headers or {}), **HEADERS}
            raise
        if expected is not None and uid != expected:
            raise HTTPException(409, {'code': 'identity_changed'}, headers=HEADERS)
        return uid

    def settings(conn, uid):
        row = conn.execute('''SELECT revision,mode,fit,theme,background FROM user_reader_settings
                              WHERE user_id=?''', (uid,)).fetchone()
        return dict(row) if row else None

    def position(conn, uid, item_id, content):
        row = conn.execute('''SELECT revision,content,page,total_pages,offset,updated_at
            FROM user_reader_positions WHERE user_id=? AND item_id=?''', (uid, item_id)).fetchone()
        revision = row['revision'] if row else 0
        return revision, dict(row) if row and row['content'] == content else None, bool(row and row['content'] == DELETED_CONTENT)

    @router.get('')
    def list_positions(request: Request):
        with get_db() as conn:
            conn.execute('BEGIN')
            uid = principal(request, conn)
            fields = 'i.slug, p.item_id, p.revision, p.page, p.total_pages, p.updated_at, p.content'
            joined = 'FROM user_reader_positions p JOIN items i ON p.item_id=i.id'
            rows = conn.execute(f'''SELECT * FROM (
                SELECT {fields} {joined} WHERE p.user_id=? AND p.content!=? AND {readable}
                ORDER BY p.updated_at DESC, p.item_id DESC LIMIT ?)
                UNION ALL SELECT {fields} {joined} WHERE p.user_id=? AND p.content=?
                ORDER BY updated_at DESC, item_id DESC''',
                (uid, DELETED_CONTENT, RECENT_LIMIT, uid, DELETED_CONTENT)).fetchall()
        records, invalidated, deleted = [], [], []
        for row in rows:
            if row['content'] == DELETED_CONTENT:
                deleted.append({'slug': row['slug'], 'revision': row['revision']})
                continue
            try:
                content, _ = document(row['slug'])
            except HTTPException as exc:
                if exc.status_code == 404:
                    continue
                raise
            if content == row['content']:
                records.append({k: row[k] for k in ('slug', 'page', 'total_pages', 'updated_at', 'content', 'revision')})
            else:
                invalidated.append({k: row[k] for k in ('slug', 'content', 'updated_at')})
        return JSONResponse({'user_id': uid, 'records': records, 'invalidated': invalidated, 'deleted': deleted}, headers=HEADERS)

    @router.get('/{slug}')
    def read_book(slug: str, request: Request):
        with get_db() as conn:
            conn.execute('BEGIN')
            uid = principal(request, conn)
            item_id, content, total = info(conn, slug)
            revision, progress, deleted = position(conn, uid, item_id, content)
            result = {'user_id': uid, 'content': content, 'total_pages': total,
                      'revision': revision, 'progress': progress, 'deleted': deleted, 'settings': settings(conn, uid)}
        return JSONResponse(result, headers=HEADERS)

    @router.delete('/{slug}')
    def delete_position(slug: str, data: WriteIn, request: Request):
        same_origin(request)
        with get_db(immediate=True) as conn:
            uid = principal(request, conn, data.user_id)
            if not SLUG.fullmatch(slug):
                raise HTTPException(404, '阅读资源不存在', headers=HEADERS)
            item = conn.execute(f'''SELECT i.id, ({readable}) AS readable, p.content, p.revision
                FROM items i LEFT JOIN user_reader_positions p ON p.item_id=i.id AND p.user_id=?
                WHERE i.slug=?''', (uid, slug)).fetchone()
            revision = (item['revision'] or 0) if item else 0
            if item and item['content'] == DELETED_CONTENT:
                pass  # A retry after a lost acknowledgement is already complete.
            elif data.revision != revision:
                raise HTTPException(409, '阅读进度已更新，请刷新后重试', headers=HEADERS)
            elif item and (item['readable'] or revision):
                revision += 1
                conn.execute('''INSERT INTO user_reader_positions
                    (user_id,item_id,revision,content,page,total_pages,offset,updated_at) VALUES(?,?,?,?,1,1,0,0)
                    ON CONFLICT(user_id,item_id) DO UPDATE SET revision=excluded.revision,content=excluded.content,
                    page=1,total_pages=1,offset=0,updated_at=0''', (uid, item['id'], revision, DELETED_CONTENT))
        return JSONResponse({'user_id': uid, 'slug': slug, 'revision': revision, 'deleted': True}, headers=HEADERS)

    @router.put('/settings')
    def write_settings(data: SettingsIn, request: Request):
        same_origin(request)
        with get_db(immediate=True) as conn:
            uid = principal(request, conn, data.user_id)
            current = settings(conn, uid)
            if data.revision != (current['revision'] if current else 0):
                return JSONResponse({'user_id': uid, 'settings': current}, status_code=409, headers=HEADERS)
            conn.execute('''INSERT INTO user_reader_settings(user_id,revision,mode,fit,theme,background)
                VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET revision=excluded.revision,
                mode=excluded.mode,fit=excluded.fit,theme=excluded.theme,background=excluded.background''',
                (uid, data.revision + 1, data.mode, data.fit, data.theme, data.background))
            result = settings(conn, uid)
        return JSONResponse({'user_id': uid, 'settings': result}, headers=HEADERS)

    @router.put('/{slug}')
    def write_position(slug: str, data: PositionIn, request: Request):
        same_origin(request)
        with get_db(immediate=True) as conn:
            uid = principal(request, conn, data.user_id)
            item_id, content, total = info(conn, slug)
            if data.content != content:
                raise HTTPException(409, {'code': 'reader_changed'}, headers=HEADERS)
            if data.total_pages != total or data.page > total:
                raise HTTPException(422, '阅读位置超出范围', headers=HEADERS)
            revision, previous, deleted = position(conn, uid, item_id, content)
            if data.revision != revision:
                return JSONResponse({'user_id': uid, 'revision': revision, 'progress': previous, 'deleted': deleted},
                                    status_code=409, headers=HEADERS)
            stamp = max(time.time_ns() // 1000000, (previous['updated_at'] + 1) if previous else 0)
            conn.execute('''INSERT INTO user_reader_positions(user_id,item_id,revision,content,page,total_pages,offset,updated_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,item_id) DO UPDATE SET revision=excluded.revision,
                content=excluded.content,page=excluded.page,total_pages=excluded.total_pages,
                offset=excluded.offset,updated_at=excluded.updated_at''',
                (uid, item_id, revision + 1, content, data.page, total, data.offset, stamp))
            revision, result, _ = position(conn, uid, item_id, content)
        return JSONResponse({'user_id': uid, 'revision': revision, 'progress': result}, headers=HEADERS)

    return router
