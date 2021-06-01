import os
import json
import time
import hashlib
from functools import wraps

CACHE_LOCATION = os.path.expanduser('~/.ghm/cache.json')


def cache(f):
    @wraps(f)
    def wrapper(self, *args, **kwargs):
        cache = self._cache

        parts = [f.__name__] + [str(a) for a in args if a is not None]
        key = hashlib.sha256("_".join(parts).encode()).hexdigest()

        if cache.exists(key):
            return cache.get(key)

        val = f(self, *args, **kwargs)
        cache.save(key, val)
        return val
    return wrapper


def invalidate(f):
    @wraps(f)
    def wrapper(self, *args, **kwargs):
        cache = self._cache
        val = f(self, *args, **kwargs)
        cache.clear()
        return val
    return wrapper


class Cache:
    def __init__(self, location=None):
        self._location = location and location or CACHE_LOCATION
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
                self._data = json.load(open(self._location, 'rt'))
            else:
                self._data = {}
        except FileNotFoundError:
            self._data = {}

    def save(self, key, value):
        self._data[key] = value

    def get(self, key):
        return self._data[key]

    def exists(self, key):
        return key in self._data.keys()

    def invalidate(self, key):
        del self._data[key]
