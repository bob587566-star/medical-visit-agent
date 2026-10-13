"""Recoverable local accounts. Account roles are not medical credentials."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .storage import now, session_digest


def valid_password_text(value: str) -> str:
    try:
        value.encode('utf-8')
    except UnicodeError:
        raise ValueError('密码文字编码无效。') from None
    return value


class Credentials(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=15, max_length=128)

    _valid_password = field_validator('password')(valid_password_text)

    @field_validator('username')
    @classmethod
    def normalized(cls, value):
        value = value.strip().lower()
        if not re.fullmatch(r'[a-z][a-z0-9_.-]{2,39}', value):
            raise ValueError('请使用匿名账号名，首位字母，3至40位字母、数字或._-')
        return value


class Recovery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    username: str = Field(min_length=3, max_length=40)
    recovery_code: str = Field(min_length=32, max_length=100)
    new_password: str = Field(min_length=15, max_length=128)

    _valid_password = field_validator('new_password')(valid_password_text)

    @field_validator('username')
    @classmethod
    def normalized(cls, value):
        return Credentials.normalized(value)


class DeleteAccount(BaseModel):
    model_config = ConfigDict(extra='forbid')
    password: str = Field(min_length=1, max_length=128)


def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=32768, r=8, p=1, maxmem=80 * 1024 * 1024)
    return 'scrypt-v1:' + salt.hex() + ':' + derived.hex()


def password_matches(password: str, encoded: str) -> bool:
    try:
        scheme, salt, value = encoded.split(':')
        if scheme != 'scrypt-v1' or len(salt) != 32 or len(value) != 128:
            return False
        return hmac.compare_digest(password_hash(password, bytes.fromhex(salt)), encoded)
    except (ValueError, TypeError):
        return False


def recovery_digest(code: str) -> str:
    normalized = code.replace('-', '').strip().lower()
    if not re.fullmatch(r'[0-9a-f]{64}', normalized):
        normalized = 'invalid-recovery-code'
    return hashlib.sha256(normalized.encode('ascii')).hexdigest()


def fresh_recovery() -> str:
    raw = secrets.token_hex(32)
    return '-'.join(raw[index:index + 8] for index in range(0, len(raw), 8))


class Accounts:
    def __init__(self, store):
        self.store = store
        self.hash_slots = asyncio.Semaphore(2)
        self.dummy_hash = password_hash(secrets.token_urlsafe(32))
        with store.lock, store.connect() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS accounts (id TEXT PRIMARY KEY, data_owner TEXT UNIQUE NOT NULL REFERENCES sessions(id), username_hash TEXT UNIQUE NOT NULL, profile TEXT NOT NULL, password_hash TEXT NOT NULL, recovery_digest TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN (\'user\',\'reviewer\',\'admin\')), created_at TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS auth_sessions (id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, owner TEXT NOT NULL REFERENCES sessions(id), csrf TEXT NOT NULL, expires_at TEXT NOT NULL)')
            conn.execute('CREATE TABLE IF NOT EXISTS auth_attempts (id TEXT PRIMARY KEY, window_start TEXT NOT NULL, attempts INTEGER NOT NULL)')

    @staticmethod
    def username_digest(username: str) -> str:
        return hashlib.sha256(username.strip().lower().encode('utf-8')).hexdigest()

    def resolve(self, cookie: str | None):
        if not cookie or len(cookie) > 200:
            return None
        identifier = session_digest(cookie)
        with self.store.connect() as conn:
            row = conn.execute('SELECT owner,csrf FROM auth_sessions WHERE id=? AND expires_at>?', (identifier, now())).fetchone()
        if row:
            csrf = self.store.open_value('auth_csrf', row[0], identifier, row[1])
            return row[0], csrf
        session = self.store.session(cookie)
        if session:
            with self.store.connect() as conn:
                claimed = conn.execute('SELECT 1 FROM accounts WHERE data_owner=?', (session[0],)).fetchone()
            # The original guest cookie cannot keep access after account binding.
            return None if claimed else session
        return None

    def public(self, owner: str):
        with self.store.connect() as conn:
            row = conn.execute('SELECT id,profile,role FROM accounts WHERE data_owner=?', (owner,)).fetchone()
        if not row:
            return None
        profile = self.store.open_value('account_profile', owner, row[0], row[1])
        return {'id': row[0], 'username': profile['username'], 'role': row[2], 'identity_verified': False}

    def throttle(self, remote: str, operation: str):
        # Persistent across API restarts; never store a raw IP or submitted name.
        identifier = hashlib.sha256((operation + ':' + remote).encode()).hexdigest()
        stamp = now()
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec='seconds')
        with self.store.lock, self.store.connect() as conn:
            row = conn.execute('SELECT window_start,attempts FROM auth_attempts WHERE id=?', (identifier,)).fetchone()
            if row and row[0] > cutoff and row[1] >= 20:
                raise HTTPException(429, '尝试过于频繁，请稍后再试。', headers={'Retry-After': '600'})
            start, count = (row[0], row[1] + 1) if row and row[0] > cutoff else (stamp, 1)
            conn.execute('INSERT INTO auth_attempts VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET window_start=excluded.window_start,attempts=excluded.attempts', (identifier, start, count))
            conn.execute('DELETE FROM auth_attempts WHERE window_start<?', (cutoff,))

    def issue(self, conn, account_id: str, owner: str):
        cookie, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        identifier = session_digest(cookie)
        expiry = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(timespec='seconds')
        conn.execute('INSERT INTO auth_sessions VALUES (?,?,?,?,?)', (identifier, account_id, owner, self.store.seal_value('auth_csrf', owner, identifier, csrf), expiry))
        return cookie, csrf

    async def register(self, owner: str, body: Credentials):
        async with self.hash_slots:
            encoded = await asyncio.to_thread(password_hash, body.password)
        identifier, recovery = secrets.token_hex(16), fresh_recovery()
        digest = self.username_digest(body.username)
        with self.store.lock, self.store.connect() as conn:
            if conn.execute('SELECT 1 FROM accounts WHERE username_hash=? OR data_owner=?', (digest, owner)).fetchone():
                raise HTTPException(409, '无法创建这个账号，请使用其他账号名或直接登录。')
            profile = self.store.seal_value('account_profile', owner, identifier, {'username': body.username})
            conn.execute('INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?)', (identifier, owner, digest, profile, encoded, recovery_digest(recovery), 'user', now()))
            cookie, csrf = self.issue(conn, identifier, owner)
        return cookie, csrf, recovery

    async def login(self, body: Credentials):
        with self.store.connect() as conn:
            row = conn.execute('SELECT id,data_owner,password_hash FROM accounts WHERE username_hash=?', (self.username_digest(body.username),)).fetchone()
        async with self.hash_slots:
            matches = await asyncio.to_thread(password_matches, body.password, row[2] if row else self.dummy_hash)
        if not row or not matches:
            raise HTTPException(401, '账号或密码不正确。')
        with self.store.lock, self.store.connect() as conn:
            current = conn.execute('SELECT password_hash FROM accounts WHERE id=?', (row[0],)).fetchone()
            if not current or not hmac.compare_digest(current[0], row[2]):
                raise HTTPException(401, '账号状态已变化，请重新登录。')
            return (*self.issue(conn, row[0], row[1]), row[1])

    async def recover(self, body: Recovery):
        with self.store.connect() as conn:
            row = conn.execute('SELECT id,data_owner,recovery_digest FROM accounts WHERE username_hash=?', (self.username_digest(body.username),)).fetchone()
        supplied = recovery_digest(body.recovery_code)
        if not row or not hmac.compare_digest(supplied, row[2]):
            raise HTTPException(401, '账号或恢复码不正确。')
        async with self.hash_slots:
            encoded = await asyncio.to_thread(password_hash, body.new_password)
        recovery = fresh_recovery()
        with self.store.lock, self.store.connect() as conn:
            # A concurrent use of a one-time recovery code loses this CAS.
            updated = conn.execute('UPDATE accounts SET password_hash=?,recovery_digest=? WHERE id=? AND recovery_digest=?', (encoded, recovery_digest(recovery), row[0], supplied)).rowcount
            if not updated:
                raise HTTPException(401, '恢复码已失效，请使用最新恢复码。')
            conn.execute('DELETE FROM auth_sessions WHERE account_id=?', (row[0],))
            cookie, csrf = self.issue(conn, row[0], row[1])
        return cookie, csrf, row[1], recovery

    def logout(self, cookie):
        with self.store.lock, self.store.connect() as conn:
            conn.execute('DELETE FROM auth_sessions WHERE id=?', (session_digest(cookie or ''),))

    def cleanup(self):
        with self.store.lock, self.store.connect() as conn:
            conn.execute('DELETE FROM auth_sessions WHERE expires_at<?', (now(),))

    async def delete(self, owner: str, password: str):
        with self.store.connect() as conn:
            row = conn.execute('SELECT id,password_hash FROM accounts WHERE data_owner=?', (owner,)).fetchone()
        if not row:
            raise HTTPException(401, '请先登录账号。')
        async with self.hash_slots:
            valid = await asyncio.to_thread(password_matches, password, row[1])
        if not valid:
            raise HTTPException(401, '密码不正确，未删除账号。')
        with self.store.lock, self.store.connect() as conn:
            current = conn.execute('SELECT password_hash FROM accounts WHERE id=?', (row[0],)).fetchone()
            if not current or not hmac.compare_digest(current[0], row[1]):
                raise HTTPException(401, '账号状态已变化，请重新确认密码。')
            conn.execute('DELETE FROM visits WHERE owner=?', (owner,))
            for table in ('bookmarks', 'task_audit', 'task_audit_heads', 'privacy_current', 'privacy_events'):
                conn.execute(f'DELETE FROM {table} WHERE owner=?', (owner,))
            conn.execute('DELETE FROM auth_sessions WHERE account_id=?', (row[0],))
            conn.execute('DELETE FROM accounts WHERE id=?', (row[0],))
            conn.execute('DELETE FROM sessions WHERE id=?', (owner,))


def register_account_routes(app, identity, mutation, cookie_name, secure=False):
    def write_cookie(response: Response, cookie: str):
        response.set_cookie(cookie_name, cookie, max_age=7 * 86400, httponly=True, samesite='strict', secure=secure)

    @app.get('/api/account')
    async def account(session=Depends(identity)):
        return {'account': app.state.accounts.public(session[0])}

    @app.post('/api/account/register', status_code=201)
    async def register(body: Credentials, request: Request, response: Response, owner=Depends(mutation)):
        app.state.accounts.throttle(request.client.host if request.client else 'unknown', 'register')
        cookie, csrf, code = await app.state.accounts.register(owner, body)
        write_cookie(response, cookie)
        return {'account': app.state.accounts.public(owner), 'csrf_token': csrf, 'recovery_code': code}

    @app.post('/api/account/login')
    async def login(body: Credentials, request: Request, response: Response, owner=Depends(mutation)):
        app.state.accounts.throttle(request.client.host if request.client else 'unknown', 'login')
        cookie, csrf, account_owner = await app.state.accounts.login(body)
        write_cookie(response, cookie)
        return {'account': app.state.accounts.public(account_owner), 'csrf_token': csrf}

    @app.post('/api/account/recover')
    async def recover(body: Recovery, request: Request, response: Response, owner=Depends(mutation)):
        app.state.accounts.throttle(request.client.host if request.client else 'unknown', 'recover')
        cookie, csrf, account_owner, code = await app.state.accounts.recover(body)
        write_cookie(response, cookie)
        return {'account': app.state.accounts.public(account_owner), 'csrf_token': csrf, 'recovery_code': code}

    @app.post('/api/account/logout')
    async def logout(request: Request, response: Response, owner=Depends(mutation)):
        app.state.accounts.logout(request.cookies.get(cookie_name))
        response.delete_cookie(cookie_name, httponly=True, samesite='strict', secure=secure)
        return {'account': None}

    @app.delete('/api/account', status_code=204)
    async def delete(body: DeleteAccount, response: Response, owner=Depends(mutation)):
        await app.state.accounts.delete(owner, body.password)
        response.delete_cookie(cookie_name, httponly=True, samesite='strict', secure=secure)
        response.status_code = 204
        return response
