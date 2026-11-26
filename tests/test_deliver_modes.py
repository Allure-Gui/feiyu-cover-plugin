"""单元测试：覆盖麦弦 v0.2.2 四档发歌模式、整首段数上限护栏、瞬时重试与单曲卡徽章。"""
from __future__ import annotations

import ast
import asyncio
import base64
import contextlib
import hashlib
import html as html_lib
import unittest
from pathlib import Path
from types import SimpleNamespace

PLUGIN = Path(__file__).resolve().parents[1] / "plugin.py"
TREE = ast.parse(PLUGIN.read_text(encoding="utf-8"))
CLASS = next(n for n in TREE.body if isinstance(n, ast.ClassDef) and n.name == "FeiyuCoverPlugin")
METHODS = {n.name: n for n in CLASS.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _load_method(name: str, extra_ns: dict | None = None):
    node = ast.parse(ast.unparse(METHODS[name])).body[0]
    node.decorator_list = []
    mod = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(mod)
    ns = {
        "asyncio": asyncio,
        "base64": base64,
        "contextlib": contextlib,
        "hashlib": hashlib,
        "html_lib": html_lib,
        "Any": object,
    }
    if extra_ns:
        ns.update(extra_ns)
    exec(compile(mod, str(PLUGIN), "exec"), ns)
    return ns[name]


_normalize = _load_method("_normalize_deliver_mode")
_build_card = _load_method("_build_song_card_html")
_send_voice_parts = _load_method("_send_voice_parts")
_deliver = _load_method("_deliver")


class FakeResp:
    def __init__(self, status_code=200, json_data=None, content=b"audio"):
        self.status_code = status_code
        self._json = json_data or {}
        self.content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json


class FakeClient:
    def __init__(self, total_parts: int):
        self.total_parts = total_parts
        self.requested_parts: list[int] = []

    async def get(self, url: str, params=None, timeout=None):
        if url.endswith("/voice-info"):
            return FakeResp(200, {"parts": self.total_parts})
        if "/voice-part/" in url:
            idx = int(url.rsplit("/", 1)[1])
            self.requested_parts.append(idx)
            return FakeResp(200, content=f"part-{idx}".encode())
        return FakeResp(404)


class FakeLogger:
    def warning(self, *a, **kw): pass
    def error(self, *a, **kw): pass
    def info(self, *a, **kw): pass
    def debug(self, *a, **kw): pass


class FakePlugin:
    _normalize_deliver_mode = staticmethod(_normalize)
    _send_voice_parts = _send_voice_parts
    _deliver = _deliver

    def __init__(self, deliver_mode: str, total_parts: int = 5, max_voice_parts: int = 8, fail_once_on: int = -1, file_ok: bool = True):
        self.config = SimpleNamespace(
            cover=SimpleNamespace(
                deliver_mode=deliver_mode,
                max_voice_parts=max_voice_parts,
                voice_part_interval=0.5,
                send_song_card=True,
            )
        )
        self._cli = FakeClient(total_parts=total_parts)
        self.sent_segments: list[dict] = []
        self.card_calls: list[dict] = []
        self.file_calls: int = 0
        self.highlight_calls: int = 0
        self.planner_notes: list[str] = []
        self._fail_once_on = fail_once_on
        self._failed_already = False
        self._file_ok = file_ok

        async def hybrid(segs, stream_id, processed_plain_text=""):
            if self._fail_once_on >= 0 and len(self.sent_segments) == self._fail_once_on and not self._failed_already:
                self._failed_already = True
                return False
            self.sent_segments.extend(segs)
            return True

        async def text(msg, stream_id):
            return True

        self.ctx = SimpleNamespace(
            logger=FakeLogger(),
            send=SimpleNamespace(hybrid=hybrid, text=text),
        )

    def _client(self):
        return self._cli

    def _should_use_qq_master(self, platform: str) -> bool:
        return platform.startswith("qq")

    def _send_ok(self, res) -> bool:
        return bool(res)

    def _spawn(self, coro):
        coro.close()

    async def _pet_notify(self, *a, **kw):
        pass

    async def _append_planner_note(self, stream_id: str, note: str):
        self.planner_notes.append(note)

    async def _resolve_platform(self, stream_id: str, record: dict) -> str:
        return "qq"

    async def _send_song_card(self, meta, title, stream_id, requester, highlight=True, *, deliver_mode="", expected_parts=0):
        self.card_calls.append({"highlight": highlight, "deliver_mode": deliver_mode, "expected_parts": expected_parts})
        return True

    async def _send_highlight(self, job_id, title, stream_id, seconds=58, *, platform=""):
        self.highlight_calls += 1
        return 1

    async def _send_file(self, job_id, title, stream_id, *, platform=""):
        self.file_calls += 1
        return self._file_ok


class DeliverModeTests(unittest.TestCase):
    def test_normalize_all_four_modes(self):
        self.assertEqual(_normalize("副歌高潮直出（58秒·推荐）"), "highlight")
        self.assertEqual(_normalize("highlight"), "highlight")
        self.assertEqual(_normalize("从头分段发送（前2段×60秒）"), "two_parts")
        self.assertEqual(_normalize("two_parts"), "two_parts")
        self.assertEqual(_normalize("整首完整分段发送（每段60秒·连续发完）"), "full_voice")
        self.assertEqual(_normalize("full_voice"), "full_voice")
        self.assertEqual(_normalize("直接发送完整 MP3 音频文件"), "full_file")
        self.assertEqual(_normalize("full_file"), "full_file")

    def test_song_card_badge_labels(self):
        h1 = _build_card(title="雨爱", artist="杨丞琳", pitch=0, requester="老大", deliver_mode="highlight", duration_sec=240)
        h2 = _build_card(title="雨爱", artist="杨丞琳", pitch=0, requester="老大", deliver_mode="two_parts", duration_sec=240)
        h3 = _build_card(title="雨爱", artist="杨丞琳", pitch=0, requester="老大", deliver_mode="full_voice", expected_parts=4, duration_sec=240)
        h4 = _build_card(title="雨爱", artist="杨丞琳", pitch=0, requester="老大", deliver_mode="full_file", duration_sec=240)
        self.assertIn("副歌高潮直出", h1)
        self.assertIn("分段演唱 · 前2段", h2)
        self.assertIn("整首分段 · 共4段", h3)
        self.assertIn("320k 完整音频", h4)

    def test_full_voice_sends_all_parts_with_cap_and_retry(self):
        # 模拟 5 段歌曲、上限 4 段、且第 2 段首次发送抖动后重试成功
        async def run():
            orig_sleep = asyncio.sleep
            async def fast_sleep(_): pass
            asyncio.sleep = fast_sleep
            try:
                p = FakePlugin("整首完整分段发送（每段60秒·连续发完）", total_parts=5, max_voice_parts=4, fail_once_on=1)
                await p._deliver("job1", {"meta": {"title": "雨爱", "duration": 290}}, {"stream_id": "s1", "platform": "qq"})
                self.assertEqual(len(p.sent_segments), 4)
                self.assertEqual(p._cli.requested_parts, [0, 1, 2, 3])
                self.assertEqual(p.card_calls[0]["deliver_mode"], "full_voice")
                self.assertEqual(p.card_calls[0]["expected_parts"], 4)
                self.assertIn("整首分段语音（共 4 段）", p.planner_notes[0])
            finally:
                asyncio.sleep = orig_sleep

        asyncio.run(run())

    def test_full_file_and_telegram_fallback(self):
        async def run():
            orig_sleep = asyncio.sleep
            async def fast_sleep(_): pass
            asyncio.sleep = fast_sleep
            try:
                # QQ 直接发完整 MP3 文件
                p_qq = FakePlugin("直接发送完整 MP3 音频文件", total_parts=4)
                await p_qq._deliver("job2", {"meta": {"title": "大鱼", "duration": 220}}, {"stream_id": "s2", "platform": "qq"})
                self.assertEqual(p_qq.file_calls, 1)
                self.assertEqual(len(p_qq.sent_segments), 0)
                self.assertEqual(p_qq.card_calls[0]["deliver_mode"], "full_file")

                # Telegram 不支持 file 段，自动切为整首分段语音
                p_tg = FakePlugin("直接发送完整 MP3 音频文件", total_parts=3)
                await p_tg._deliver("job3", {"meta": {"title": "大鱼", "duration": 170}}, {"stream_id": "s3", "platform": "telegram"})
                self.assertEqual(p_tg.file_calls, 0)
                self.assertEqual(len(p_tg.sent_segments), 3)
                self.assertEqual(p_tg.card_calls[0]["deliver_mode"], "full_voice")
            finally:
                asyncio.sleep = orig_sleep

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
