"""Standalone stdlib regressions for the upload bridge included in this plugin release."""
import ast
import asyncio
import base64
import json
import re
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]


def load_bridge():
    tree = ast.parse((ROOT / 'plugin.py').read_text(encoding='utf-8-sig'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'FeiyuCoverPlugin')
    methods = {'jingge_source_upload', 'jingge_preview', '_drop_source_upload', '_expire_source_later', '_forward_source'}
    cls.body = [n for n in cls.body if getattr(n, 'name', '') in methods]
    cls.bases.clear()
    for fn in cls.body:
        fn.decorator_list.clear()
    tree.body = [cls]
    scope = dict(globals())
    exec(compile(tree, str(ROOT / 'plugin.py'), 'exec'), scope)
    plugin = scope[cls.name]()
    plugin._source_uploads = {}
    return plugin


class SourceUploadBridge(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = load_bridge()
        self.rpc = self.plugin.jingge_source_upload

    def tearDown(self):
        for token in list(self.plugin._source_uploads):
            self.plugin._drop_source_upload(token)

    def test_webui_contract(self):
        page = json.loads((ROOT / 'webui.json').read_text(encoding='utf-8-sig'))['pages'][0]
        action = page['actions']['sourceUpload']
        self.assertEqual(action['api'], 'jingge_source_upload')
        fields = action['parameters']
        self.assertEqual(set(fields), {'op', 'token', 'name', 'size', 'index', 'data'})
        self.assertEqual(set(fields['op']['choices']), {'start', 'chunk', 'finish', 'cancel', 'status'})
        self.assertEqual(fields['size']['maximum'], 32 * 1024 * 1024)
        self.assertEqual(fields['data']['max_length'], 32768)

    async def test_chunk_wire_body_and_pending_status(self):
        gate = asyncio.Event()
        raw = bytes(range(256)) * 961
        upload_id = 'a' * 32

        async def post(path, *, content, headers, timeout, follow_redirects):
            self.assertEqual(path, '/sources/uploads')
            self.assertFalse(follow_redirects)
            self.assertEqual(headers['X-Audio-Extension'], 'wav')
            self.assertEqual(headers['Content-Length'], str(len(raw)))
            self.assertEqual(headers['Content-Type'], 'application/octet-stream')
            self.assertEqual(b''.join([block async for block in content]), raw)
            await gate.wait()
            return SimpleNamespace(is_success=True, json=lambda: {'upload_id': upload_id})

        client = SimpleNamespace(post=AsyncMock(side_effect=post))
        self.plugin._client = lambda: client
        for length in [240 * 1024, 24 * 1024]:
            with self.subTest(length=length):
                gate.clear()
                started = await self.rpc('start', name='original.WAV', size=len(raw))
                self.assertEqual(started['chunk_size'], 24576)
                token = started['token']
                for i, pos in enumerate(range(0, len(raw), length)):
                    data = base64.b64encode(raw[pos:pos + length]).decode()
                    self.assertTrue((await self.rpc('chunk', token, index=i, data=data))['ok'])
                self.assertEqual((await self.rpc('finish', token))['status'], 'uploading')
                self.assertEqual((await self.rpc('finish', token))['status'], 'uploading')
                gate.set()
                await self.plugin._source_uploads[token]['task']
                self.assertEqual((await self.rpc('status', token))['upload_id'], upload_id)
                self.assertFalse(self.plugin._source_uploads[token]['chunks'])
        self.assertEqual(client.post.await_count, 2)

    async def test_fail_cancel_and_expire(self):
        self.assertFalse((await self.rpc('start', name='big.wav', size=33554433))['ok'])
        self.assertFalse((await self.rpc('start', name='bad.exe', size=1))['ok'])
        for op, args in [('finish', {}), ('chunk', {'data': '!!!!', 'index': 0}), ('chunk', {'data': 'YQ==', 'index': 1}), ('cancel', {})]:
            token = (await self.rpc('start', name='one.aac', size=1))['token']
            self.assertEqual((await self.rpc(op, token, **args))['ok'], op == 'cancel')
            self.assertFalse((await self.rpc('status', token))['ok'])
        token = (await self.rpc('start', name='one.aac', size=1))['token']
        await self.rpc('chunk', token, data='YQ==', index=0)
        self.plugin._client = lambda: SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(is_success=False, status_code=422)))
        self.assertFalse((await self.rpc('finish', token))['ok'])
        self.assertFalse(self.plugin._source_uploads[token]['chunks'])
        loop = asyncio.get_running_loop()
        with patch.object(loop, 'call_later', lambda delay, callback, *args: loop.call_soon(callback, *args)):
            token = (await self.rpc('start', name='old.wav', size=1))['token']
            await asyncio.sleep(0)
        self.assertFalse((await self.rpc('status', token))['ok'])

    async def test_uploaded_preview_discards_stale_search(self):
        plugin = self.plugin
        plugin._parse_payload = json.loads
        plugin._health_ok = AsyncMock(return_value=True)
        plugin._load_voice = dict
        plugin._save_voice = lambda voice, persist: voice
        plugin._voice_body = dict
        post = AsyncMock(return_value=SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'id': 'job'}))
        plugin._client = lambda: SimpleNamespace(post=post)
        upload_id = 'f' * 32
        result = await plugin.jingge_preview(json.dumps({'upload_id': upload_id, 'song': 'old', 'bvid': 'BV', 'selected_track': {'title': 'old'}, 'source_hint': 'qq'}))
        self.assertEqual(result['job_id'], 'job')
        self.assertEqual(post.await_args.kwargs['json'], {'upload_id': upload_id, 'voice': {}, 'requester': 'playground', 'preview_seconds': 0})


if __name__ == '__main__':
    unittest.main()
