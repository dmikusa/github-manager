import os
import json
import re
import sys
import time
import hashlib
from functools import wraps

CACHE_LOCATION = os.path.expanduser('~/.ghm/cache.json')
CACHE_TTL_DEFAULT = 60


def cache(f, ttl=CACHE_TTL_DEFAULT):
    @wraps(f)
    def wrapper(self, *args, **kwargs):
        if getattr(self, 'skip_cache', False):
            return f(self, *args, **kwargs)

        cache = self._cache

        parts = [str(a) for a in args if a is not None]
        parts += [str(kwargs[k]) for k in sorted(kwargs) if kwargs[k] is not None]
        key = hashlib.sha256("_".join(parts).encode()).hexdigest()

        method = f.__name__
        entry = cache.get(method, key)
        if entry is not None:
            return entry

        val = f(self, *args, **kwargs)
        scope = args[0] if args else None
        cache.save(method, key, val, scope=scope, ttl=ttl)
        return val
    return wrapper


def invalidate(*method_names, use_scope=True):
    def decorator(f):
        @wraps(f)
        def wrapper(self, *args, **kwargs):
            val = f(self, *args, **kwargs)
            cache = self._cache
            scope = args[0] if use_scope and args else None
            for method_name in method_names:
                cache.invalidate_method(method_name, scope=scope)
            return val
        return wrapper
    return decorator


def _is_new_format(data):
    for key in data:
        if isinstance(key, str) and not re.match(r'^[0-9a-f]{64}$', key):
            return True
    return not data


class Cache:
    def __init__(self, location=None):
        self._location = location or CACHE_LOCATION
        self._data = {}

    def clear(self):
        self._data = {}
        self.store()

    def store(self):
        json.dump(self._data, open(self._location, 'wt'))

    def load(self):
        try:
            mtime = os.path.getmtime(self._location)
            now = time.time()
            if (now - mtime) <= 86400:
                data = json.load(open(self._location, 'rt'))
                if _is_new_format(data):
                    self._data = data
                else:
                    print(
                        "Warning: Cache file has incompatible format, discarding",
                        file=sys.stderr,
                    )
                    self._data = {}
            else:
                self._data = {}
        except FileNotFoundError:
            self._data = {}
        except (json.JSONDecodeError, ValueError) as e:
            print(f"Warning: Could not read cache file ({e}), starting fresh",
                  file=sys.stderr)
            self._data = {}

    def save(self, method, key, value, scope=None, ttl=CACHE_TTL_DEFAULT):
        if method not in self._data:
            self._data[method] = {}
        self._data[method][key] = {
            "value": value,
            "scope": scope,
            "timestamp": time.time(),
            "ttl": ttl,
        }

    def get(self, method, key):
        entries = self._data.get(method, {})
        entry = entries.get(key)
        if entry is None:
            return None
        if time.time() - entry["timestamp"] > entry["ttl"]:
            del entries[key]
            return None
        return entry["value"]

    def exists(self, method, key):
        entries = self._data.get(method, {})
        entry = entries.get(key)
        if entry is None:
            return False
        if time.time() - entry["timestamp"] > entry["ttl"]:
            del entries[key]
            return False
        return True

    def invalidate_method(self, method, scope=None):
        entries = self._data.get(method, {})
        if scope is None:
            self._data[method] = {}
        else:
            self._data[method] = {
                k: v for k, v in entries.items() if v.get("scope") != scope
            }
