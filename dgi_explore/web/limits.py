"""Who counts as one visitor, and the limits kept in memory.

An IPv4 address is one visitor. An IPv6 user usually has a whole block of
2^64 addresses (a /64) and can use a new one for every request, so an IPv6
address counts by its /64 (security review 20261120_ask-security-review.org,
finding 2). The daily question limit (store.client_hash) and the limits here
use the same rule.

Nothing here is written anywhere: the counters live in this process's memory
only, keyed by a hash, and are forgotten within minutes.
"""

import collections
import hashlib
import ipaddress
import threading
import time

ASK_PER_MINUTE = 6       # questions from one visitor in any 60 seconds
EVENTS_PER_VISITOR = 4   # "live steps" connections one visitor may hold open
EVENTS_TOTAL = 400       # and the whole site

_lock = threading.Lock()
_asked = collections.defaultdict(collections.deque)    # visitor -> times of recent questions
_open = collections.Counter()                           # visitor -> open events connections


def network(address):
    """The address as a visitor: IPv4 as it is, IPv6 as its /64."""
    try:
        ip = ipaddress.ip_address((address or "").strip())
    except ValueError:
        return address or "unknown"
    if ip.version == 6 and ip.ipv4_mapped:
        return str(ip.ipv4_mapped)
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def visitor(address):
    """A short key for the in-memory counters; the address itself isn't kept."""
    return hashlib.sha256(network(address).encode()).hexdigest()[:16]


def may_ask(key, now=None):
    """True, and counted, if this visitor has asked fewer than ASK_PER_MINUTE in the last minute."""
    now = time.monotonic() if now is None else now
    with _lock:
        q = _asked[key]
        while q and q[0] <= now - 60:
            q.popleft()
        if len(q) >= ASK_PER_MINUTE:
            return False
        q.append(now)
        if len(_asked) > 10000:                  # forget visitors with nothing recent
            for k in [k for k, v in _asked.items() if not v or v[-1] <= now - 60]:
                del _asked[k]
        return True


def open_events(key):
    """Take a "live steps" connection for this visitor; False if they hold too many."""
    with _lock:
        if _open[key] >= EVENTS_PER_VISITOR or sum(_open.values()) >= EVENTS_TOTAL:
            return False
        _open[key] += 1
        return True


def close_events(key):
    with _lock:
        _open[key] -= 1
        if _open[key] <= 0:
            del _open[key]
