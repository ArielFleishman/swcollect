"""A requests.Session stand-in that sends SwisClient traffic to a Flask test client (no sockets)."""
from urllib.parse import urlsplit


class _Resp:
    def __init__(self, r):
        self.status_code = r.status_code
        self.content = r.data
        self.text = r.data.decode("utf-8", "replace")
        self._r = r

    def json(self):
        return self._r.get_json(force=True)


class FlaskSession:
    def __init__(self, app):
        self.http = app.test_client()
        self.headers = {}
        self.auth = None
        self.verify = True

    def post(self, url, json=None, timeout=None):
        return _Resp(self.http.post(urlsplit(url).path, json=json, auth=self.auth))

    def get(self, url, timeout=None):
        u = urlsplit(url)
        return _Resp(self.http.get(u.path, auth=self.auth))
