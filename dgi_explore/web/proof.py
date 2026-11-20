"""A small puzzle each browser solves before a question is sent.

Every page with a question form carries a puzzle: a hash of a random secret
number. static/proof.js finds the number by trying them in turn (about a
second on a laptop, a few on an old phone), while the person is reading or
typing, and sends it with the question. One person never notices; a program
sending thousands of questions has to do that work thousands of times, and
most bots don't run the page's JavaScript at all. The same idea as ALTCHA,
written here so the site loads nothing from anywhere else and sets no cookie.

A puzzle is signed with the site's secret (so it can't be made up), lasts six
hours, and is accepted once. The record of used puzzles is kept in memory:
the site runs as one process (runner.py), and a restart only forgets puzzles
that would have expired within six hours anyway.
"""

import hashlib
import hmac
import os
import secrets
import threading
import time

from . import store

MAX_NUMBER = int(os.environ.get("DGI_WEB_PROOF_MAX", "80000"))   # under 1 s on a laptop, a few on an old phone
LIFETIME = 6 * 3600

_used = {}                  # salt -> when it expires
_lock = threading.Lock()


def _key():
    return hashlib.sha256(("proof|" + store._secret()).encode()).digest()


def _sign(salt, digest):
    return hmac.new(_key(), f"{salt}|{digest}".encode(), "sha256").hexdigest()


def challenge():
    """A new puzzle, for one page."""
    salt = f"{int(time.time()) + LIFETIME}.{secrets.token_hex(8)}"
    number = secrets.randbelow(MAX_NUMBER + 1)
    digest = hashlib.sha256(f"{salt}{number}".encode()).hexdigest()
    return {"salt": salt, "challenge": digest, "signature": _sign(salt, digest), "max": MAX_NUMBER}


def check(salt, digest, signature, number):
    """True if this is a puzzle the site made, not expired, solved, and not used before."""
    try:
        expires, n = int(salt.split(".", 1)[0]), int(number)
    except (ValueError, AttributeError):
        return False
    now = time.time()
    if expires < now or not 0 <= n <= MAX_NUMBER:
        return False
    if not hmac.compare_digest(_sign(salt, digest), signature or ""):
        return False
    if hashlib.sha256(f"{salt}{n}".encode()).hexdigest() != digest:
        return False
    with _lock:
        for s in [s for s, e in _used.items() if e < now]:
            del _used[s]
        if salt in _used:
            return False
        _used[salt] = expires
    return True
