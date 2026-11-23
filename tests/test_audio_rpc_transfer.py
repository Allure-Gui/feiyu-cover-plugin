import ast
import asyncio
import base64
import time
import unittest
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugin.py"
TREE = ast.parse(PLUGIN.read_text(encoding="utf-8"))
CLASS = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "FeiyuCoverPlugin")
METHODS = {n.name: n for n in CLASS.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

def helper(name):
    node = METHODS[name]
    node.decorator_list = []
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    ns = {"asyncio": asyncio, "base64": base64, "time": time}
    exec(compile(module, str(PLUGIN), "exec"), ns)
    return ns[name]

_spool_impl = helper("_spool_audio")
async def spool(client, url, timeout):
    return await _spool_impl(client, url, timeout)
class Response:
    def __init__(self, data=b"", status=200, headers=None, fail=False):
        self.data, self.status_code = data, status
        self.headers, self.fail, self.closed = headers or {}, fail, False
    async def __aenter__(self): return self
    async def __aexit__(self, *args): self.closed = True
    async def aiter_raw(self, size):
        for i in range(0, len(self.data), size): yield self.data[i:i + size]
        if self.fail: raise OSError("stream broke")
class Client:
    def __init__(self, response): self.response, self.kwargs = response, None
    def stream(self, *args, **kwargs): self.kwargs = kwargs; return self.response

class TransferTests(unittest.TestCase):
    def run_async(self, coro): return asyncio.run(coro)
    def test_old_backend_download_and_chunk_roundtrip(self):
        payload = bytes(range(251)) * 2200
        response = Response(payload, headers={"content-length": str(len(payload))})
        client = Client(response)
        f, size, mime = self.run_async(spool(client, "/audio", 3))
        try:
            self.assertEqual((size, mime), (len(payload), "audio/mpeg"))
            self.assertEqual(client.kwargs["headers"]["Accept-Encoding"], "identity")
            offset = 240 * 1024
            f.seek(offset); self.assertEqual(f.read(240 * 1024), payload[offset:offset + 240 * 1024])
        finally: f.close()
        self.assertTrue(response.closed)
    def test_http_error_and_oversize_content_length(self):
        with self.assertRaisesRegex(ValueError, "HTTP 404"):
            self.run_async(spool(Client(Response(status=404)), "/audio", 3))
        with self.assertRaisesRegex(ValueError, "超过限制"):
            self.run_async(spool(Client(Response(headers={"content-length": str(64 * 1024 * 1024 + 1)})), "/audio", 3))
    def test_unknown_length_overflow_and_stream_error(self):
        class Tracking:
            def __init__(self, response): self.response = response
            def stream(self, *a, **kw): return self.response
        response = Response(b"x" * (64 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "超过限制"):
            self.run_async(spool(Tracking(response), "/audio", 3))
        broken = Response(b"partial", fail=True)
        with self.assertRaisesRegex(OSError, "stream broke"):
            self.run_async(spool(Tracking(broken), "/audio", 3))
        self.assertTrue(response.closed); self.assertTrue(broken.closed)
    def test_rpc_cache_reuses_same_url_and_rejects_over_100_chunks(self):
        class Plugin:
            _audio_rpc_chunk = helper("_audio_rpc_chunk")
            _spool_audio = staticmethod(spool)
            def __init__(self, client):
                self._audio_lock = asyncio.Lock(); self._audio_cache = {}; self._audio_fill = None
                self._unloading = False; self._client_value = client
            def _client(self): return self._client_value
        class CountClient(Client):
            calls = 0
            def stream(self, *args, **kwargs): self.calls += 1; return super().stream(*args, **kwargs)
        payload = b"a" * (240 * 1024 + 9)
        client = CountClient(Response(payload, headers={"content-length": str(len(payload))}))
        plugin = Plugin(client)
        async def fetch_both():
            return (await plugin._audio_rpc_chunk("/same", 0, 3), await plugin._audio_rpc_chunk("/same", 1, 3))
        first, second = self.run_async(fetch_both())
        self.assertEqual(client.calls, 1)
        self.assertEqual((base64.b64decode(first[0]), base64.b64decode(second[0])), (payload[:240 * 1024], payload[240 * 1024:]))
        self.assertEqual(len(plugin._audio_cache), 1)
        key = (client, "/same")
        stale = plugin._audio_cache.pop(key)
        plugin._audio_cache[key] = (stale[0], stale[1], stale[2], time.monotonic() - 301)
        client.response = Response(b"new", headers={"content-length": "3"})
        self.run_async(plugin._audio_rpc_chunk("/other", 0, 3))
        self.assertTrue(stale[0].closed)
        big_client = CountClient(Response(b"a" * (240 * 1024 * 100 + 1)))
        with self.assertRaisesRegex(ValueError, "100 片"):
            self.run_async(Plugin(big_client)._audio_rpc_chunk("/big", 0, 3))

if __name__ == "__main__": unittest.main()
