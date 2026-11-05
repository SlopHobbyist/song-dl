"""Admin login.

Admins are listed in config.yaml (webserver.admins). Logging in sets a random
session cookie; the Server and All pages and their API are refused without it.
Everything else on the site is public.

Guessing is slowed three ways:
  * every failed attempt takes a second to answer;
  * an IP that fails 5 times in a row is locked out, for longer each time
    (30 s, 1 min, 2 min ... up to an hour), and a locked IP is refused even
    with the right password;
  * across all IPs, 30 failures in 10 minutes pause every login for a while,
    which covers attackers who rotate addresses. Existing sessions are
    unaffected, but it does mean an attacker can briefly stop new logins.
"""

import hashlib
import hmac
import secrets
import time
from collections import deque

COOKIE = 'songdl_session'
REMEMBER_SECONDS = 30 * 86400
SESSION_SECONDS = 12 * 3600     # when "stay signed in" is off: the cookie also ends with the browser
FAIL_DELAY = 1.0
LOCK_AFTER = 5
LOCK_BASE = 30
LOCK_MAX = 3600
GLOBAL_FAILS = 30
GLOBAL_WINDOW = 600
MAX_SESSIONS = 1000


def _digest(text):
    return hashlib.sha256(text.encode('utf-8')).digest()


class Auth:
    def __init__(self, admins):
        self.enabled = bool(admins)
        self._admins = {name: _digest(password) for name, password in admins.items()}
        self._nobody = _digest(secrets.token_urlsafe(16))   # compared against when the username is unknown
        self.sessions = {}    # sha256 of the token -> (username, expiry (monotonic))
        self.fails = {}       # ip -> [consecutive failures, locked until, strikes, last failure]
        self.recent = deque() # times of recent failures from anyone

    # Passwords

    def retry_after(self, ip):
        """Seconds until this IP may try again, or 0."""
        now = time.monotonic()
        while self.recent and now - self.recent[0] > GLOBAL_WINDOW:
            self.recent.popleft()
        wait = 0
        if len(self.recent) >= GLOBAL_FAILS:
            wait = GLOBAL_WINDOW - (now - self.recent[0])
        entry = self.fails.get(ip)
        if entry:
            wait = max(wait, entry[1] - now)
        return max(0, int(wait) + 1) if wait > 0 else 0

    def check(self, ip, username, password):
        """The admin's name if the login is right, else None. Call only when retry_after() is 0."""
        name = next((n for n in self._admins if hmac.compare_digest(n.encode(), username.encode())), None)
        ok = hmac.compare_digest(_digest(password), self._admins.get(name, self._nobody)) and name is not None
        now = time.monotonic()
        if ok:
            self.fails.pop(ip, None)
            return name
        self.recent.append(now)
        entry = self.fails.setdefault(ip, [0, 0, 0, now])
        entry[0] += 1
        entry[3] = now
        if entry[0] >= LOCK_AFTER:
            entry[1] = now + min(LOCK_MAX, LOCK_BASE * 2 ** entry[2])
            entry[0], entry[2] = 0, entry[2] + 1
        return None

    # Sessions

    def create_session(self, username, remember):
        now = time.monotonic()
        for k in [k for k, (_, exp) in self.sessions.items() if exp < now]:
            del self.sessions[k]
        while len(self.sessions) >= MAX_SESSIONS:
            del self.sessions[min(self.sessions, key=lambda k: self.sessions[k][1])]
        token = secrets.token_urlsafe(32)
        seconds = REMEMBER_SECONDS if remember else SESSION_SECONDS
        self.sessions[self._key(token)] = (username, now + seconds)
        return token, (seconds if remember else None)

    def user(self, token):
        """The username a session cookie belongs to, or None."""
        if not self.enabled or not token:
            return None
        key = self._key(token)
        entry = self.sessions.get(key)
        if entry is None:
            return None
        if entry[1] < time.monotonic():
            del self.sessions[key]
            return None
        return entry[0]

    def end_session(self, token):
        if token:
            self.sessions.pop(self._key(token), None)

    @staticmethod
    def _key(token):
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    def prune(self):
        now = time.monotonic()
        for ip in [ip for ip, e in self.fails.items() if e[1] < now and now - e[3] > 86400]:
            del self.fails[ip]
