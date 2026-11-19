"""鲸歌 · 麦麦插件

⚠️ 前置依赖：本插件不含翻唱引擎，必须另配一个「翻唱 API」服务
（本地或远程皆可，见 README）。装了插件 ≠ 能直接翻唱。

设计要点（都是踩过的坑）：
1. Tool 必须「立即返回」。翻唱要 3~5 分钟，宿主工具调用等不了，超时会失败。
2. 结果由后台任务轮询 API，完成后主动推送到原群。
3. 后台任务存活于插件进程内，热重载会丢，所以任务表持久化到 data_dir，on_load 时恢复。
4. 成品以语音条发送（ctx.send.hybrid 的 voice 段）；语音失败时降级为文本提示。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import contextlib
import json
import time
from pathlib import Path
from typing import Any, ClassVar, Iterable

from maibot_sdk import API, Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

try:  # httpx 由 manifest 声明，宿主会自动安装
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]


class PluginSection(PluginConfigBase):
    """插件总开关与配置版本。"""

    __ui_label__ = "插件"
    __ui_icon__ = "music"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用翻唱功能",
        json_schema_extra={"label": "启用插件"},
    )
    config_version: str = Field(default="0.1.0", description="配置版本")


class CoverSection(PluginConfigBase):
    """翻唱服务的地址、队列与发送策略。"""

    __ui_label__ = "翻唱服务"
    __ui_icon__ = "server"
    __ui_order__ = 1

    api_base: str = Field(
        default="http://127.0.0.1:9881",
        description="翻唱 API 地址：本地填 127.0.0.1，远程填服务器地址",
        json_schema_extra={"label": "服务地址"},
    )
    api_key: str = Field(
        default="",
        description="API 的 Bearer Key，服务端没设就留空",
        json_schema_extra={"label": "访问密钥"},
    )
    auto_start_api: bool = Field(
        default=False,
        description="服务连不上时自动执行启动命令拉起 API（需先填启动命令）",
        json_schema_extra={"label": "自动拉起服务"},
    )
    api_start_cmd: str = Field(
        default="",
        description="启动 API 的命令，例如 WSL 用户填：wsl.exe -d Ubuntu-24.04 -- bash /mnt/g/.../start.sh；留空则不自动拉起",
        json_schema_extra={"label": "启动命令"},
    )
    auto_stop_api: bool = Field(
        default=False,
        description="插件卸载时关闭 API（仅关闭此前由本插件自动拉起的服务，不影响手动启动的）",
        json_schema_extra={"label": "卸载时关闭服务"},
    )
    api_stop_cmd: str = Field(
        default="",
        description="关闭 API 的命令；留空则自动从启动命令推导，或用内置的 WSL uvicorn 停止方式",
        json_schema_extra={"label": "停止命令"},
    )
    cooldown_seconds: int = Field(
        default=600, ge=0, le=86400,
        description="同一个人两次点歌的最短间隔",
        json_schema_extra={"label": "冷却时间（秒）"},
    )
    poll_interval: int = Field(
        default=10, ge=3, le=120,
        description="多久查一次进度，太快没必要",
        json_schema_extra={"label": "轮询间隔（秒）"},
    )
    max_wait_seconds: int = Field(
        default=1800, ge=60, le=7200,
        description="单首歌最长等多久，超时放弃",
        json_schema_extra={"label": "最长等待（秒）"},
    )
    notify_progress: bool = Field(
        default=True,
        description="接单时是否在群里回一句「我去唱」",
        json_schema_extra={"label": "接单播报"},
    )
    bot_name: str = Field(
        default="",
        description="回复里用的自称。留空则自动取机器人昵称，取不到就用「麦麦」",
        json_schema_extra={"label": "机器人自称"},
    )


# 音色参数不再走 config.toml：由侧边栏页面「鲸歌」直接编辑插件数据目录下的 voice.json，
# 好处是改完即时生效，不用碰宿主托管的配置文件。
VOICE_DEFAULTS: dict[str, Any] = {
    "pitch": 0,
    "index_rate": 0.5,
    "protect": 0.25,
    "volume_envelope": 0.7,
    "f0_method": "rmvpe",
    "formant_shifting": True,
    "formant_qfrency": 1.15,
}

VOICE_LIMITS: dict[str, tuple[float, float]] = {
    "pitch": (-12.0, 12.0),
    "index_rate": (0.0, 1.0),
    "protect": (0.0, 0.5),
    "volume_envelope": (0.0, 1.0),
    "formant_qfrency": (0.5, 1.5),
}

VOICE_F0_METHODS = ("rmvpe", "fcpe", "crepe", "harvest", "pm")


class FeiyuCoverConfig(PluginConfigBase):
    """配置根模型：必须是嵌套节，宿主从中提取 plugin.config_version。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    cover: CoverSection = Field(default_factory=CoverSection)


class FeiyuCoverPlugin(MaiBotPlugin):
    config_model = FeiyuCoverConfig
    config_reload_subscriptions: ClassVar[Iterable[str]] = ("bot",)

    def __init__(self) -> None:
        # 必须调用基类构造：self.config 依赖它初始化的 _plugin_config_data / _plugin_config_instance
        super().__init__()
        self._tasks: set[asyncio.Task] = set()
        self._pending: dict[str, dict[str, Any]] = {}
        self._cooldown: dict[str, float] = {}
        self._state_file: Path | None = None
        self._http: httpx.AsyncClient | None = None
        self._started_flag: Path | None = None  # 标记「服务由本插件拉起」的文件

    def _flag_path(self) -> Path | None:
        if self._started_flag is None:
            return None
        return self._started_flag

    # ------------------------------------------------------------ HTTP 客户端
    def _client(self) -> "httpx.AsyncClient":
        """复用的 AsyncClient（带 api_base 与可选 Bearer Key）。"""
        if httpx is None:
            raise RuntimeError("缺少 httpx 依赖，检查 manifest 的 dependencies")
        if self._http is None:
            headers: dict[str, str] = {}
            key = (self.config.cover.api_key or "").strip()
            if key:
                headers["Authorization"] = f"Bearer {key}"
            self._http = httpx.AsyncClient(
                base_url=(self.config.cover.api_base or "").rstrip("/"),
                headers=headers,
                timeout=30.0,
            )
        return self._http

    async def _health_ok(self) -> bool:
        """探测 API 是否在线（快速，2 秒超时）。"""
        try:
            resp = await self._client().get("/health", timeout=2.0)
            return resp.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def _start_api(self) -> tuple[bool, str]:
        """按配置执行启动命令拉起 API，并等待其就绪。

        返回 (是否成功, 说明)。启动命令由用户自己填写，等价于用户手动执行该命令。
        """
        cmd = (self.config.cover.api_start_cmd or "").strip()
        if not cmd:
            return False, "未配置启动命令"
        try:
            # 用 shell 执行整条命令（用户填的是完整命令行），不等待其结束
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            # 启动命令通常很快返回（如 wsl 后台起服务），给它一点时间
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=10.0)
        except Exception as exc:  # noqa: BLE001
            return False, f"启动命令执行失败：{exc}"

        # 等 API 就绪，最多 30 秒，每 2 秒探一次
        for _ in range(15):
            if await self._health_ok():
                # 标记「服务由本插件拉起」，卸载时据此决定是否关闭
                flag = self._flag_path()
                if flag:
                    with contextlib.suppress(OSError):
                        flag.write_text(str(time.time()), encoding="utf-8")
                return True, "服务已自动启动"
            await asyncio.sleep(2.0)
        return False, "启动命令已执行，但服务在 30 秒内未就绪"

    async def _stop_api(self) -> str:
        """关闭 API（仅当标记表明服务是本插件拉起的）。

        返回说明文字。未开启自动关闭 / 无标记 / 未配停止命令时都不动作。
        """
        if not self.config.cover.auto_stop_api:
            return "未开启自动关闭"
        flag = self._flag_path()
        if not flag or not flag.exists():
            return "服务非本插件拉起，跳过关闭"
        cmd = (self.config.cover.api_stop_cmd or "").strip()
        if not cmd:
            return "未配置停止命令，跳过关闭"
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=15.0)
        except Exception as exc:  # noqa: BLE001
            return f"停止命令执行失败：{exc}"
        finally:
            # 无论成功与否都清标记，避免下次误判
            with contextlib.suppress(OSError):
                flag.unlink(missing_ok=True)
        return "已执行停止命令"

    async def _ensure_service(self) -> tuple[bool, str]:
        """确保 API 可用：在线直接返回；否则按配置尝试自动拉起。

        返回 (可用, 提示)。提示在服务不可用时给出可操作建议。
        """
        if await self._health_ok():
            return True, ""
        if not self.config.cover.auto_start_api:
            return False, (
                f"翻唱服务连接不可用（{self.config.cover.api_base}）。"
                "请先部署并启动 feiyu-cover-api 服务；如需自动拉起，"
                "请在插件配置中开启「自动拉起服务」并填写「启动命令」。"
            )
        ok, msg = await self._start_api()
        if ok:
            return True, ""
        return False, (
            f"翻唱服务连接不可用，自动拉起也失败：{msg}。"
            "请检查「启动命令」配置，或手动启动 feiyu-cover-api 服务。"
        )

    async def _post(self, path: str, payload: dict[str, Any],
                    timeout: float | None = None) -> dict[str, Any]:
        resp = await self._client().post(path, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    async def _get_job(self, job_id: str) -> dict[str, Any]:
        resp = await self._client().get(f"/jobs/{job_id}")
        resp.raise_for_status()
        return resp.json()

    # -------------------------------------------------- 音色参数（侧边栏页面）
    def _voice_path(self) -> Path:
        return Path(self.ctx.paths.data_dir) / "voice.json"

    def _bot_name(self) -> str:
        """回复用的自称：配置 > 宿主昵称 > 麦麦。"""
        configured = (self.config.cover.bot_name or "").strip()
        if configured:
            return configured
        for attr in ("bot_name", "bot_nickname", "nickname", "self_name"):
            value = getattr(self.ctx, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "麦麦"

    def _load_voice(self) -> dict[str, Any]:
        data = dict(VOICE_DEFAULTS)
        try:
            path = self._voice_path()
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data.update({k: v for k, v in raw.items()
                                 if k in VOICE_DEFAULTS or k in ("model", "index")})
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("读取 voice.json 失败，先用默认值：%s", exc)
        return data

    def _save_voice(self, incoming: dict[str, Any], persist: bool = True) -> dict[str, Any]:
        clean: dict[str, Any] = {}
        for key, default in VOICE_DEFAULTS.items():
            value = incoming.get(key, default)
            if key in VOICE_LIMITS:
                lo, hi = VOICE_LIMITS[key]
                try:
                    num = float(value)
                except (TypeError, ValueError):
                    num = float(default)
                num = min(max(num, lo), hi)
                value = int(round(num)) if isinstance(default, int) else round(num, 3)
            elif key == "f0_method":
                value = value if value in VOICE_F0_METHODS else default
            elif key == "formant_shifting":
                value = bool(value)
            clean[key] = value

        for key in ("model", "index"):
            value = incoming.get(key)
            if isinstance(value, str) and value.strip():
                clean[key] = value.strip()

        if not persist:
            return clean          # 试听用：只校验范围，不写入，别影响群里点歌的音色

        path = self._voice_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return clean

    @API("jingge_state", description="读取音色参数、服务地址与健康状态")
    async def jingge_state(self) -> dict[str, Any]:
        ok = await self._health_ok()
        if ok:
            health = {"ok": True, "message": "服务在线"}
        else:
            # 页面查询是只读的，不触发自动拉起；给出可操作提示
            if self.config.cover.auto_start_api and (self.config.cover.api_start_cmd or "").strip():
                tip = "服务离线；点歌时将按配置尝试自动拉起"
            elif not (self.config.cover.api_start_cmd or "").strip():
                tip = "服务离线；请部署 API，或在配置里填写启动命令并开启自动拉起"
            else:
                tip = "服务离线；请在配置里开启「自动拉起服务」"
            health = {"ok": False, "message": tip}
        return {
            "voice": self._load_voice(),
            "defaults": dict(VOICE_DEFAULTS),
            "limits": {k: list(v) for k, v in VOICE_LIMITS.items()},
            "f0_methods": list(VOICE_F0_METHODS),
            "api_base": self.config.cover.api_base,
            "health": health,
        }

    @API("jingge_save", description="保存音色参数，下一次点歌立即生效")
    async def jingge_save(self, payload: str) -> dict[str, Any]:
        try:
            data = json.loads(payload)
        except (ValueError, RecursionError):
            raise ValueError("参数不是合法 JSON") from None
        if not isinstance(data, dict):
            raise ValueError("参数必须是对象")
        saved = self._save_voice(data)
        self.ctx.logger.info("音色参数已更新：%s", json.dumps(saved, ensure_ascii=False))
        return {"ok": True, "voice": saved}

    @API("jingge_models", description="列出可切换的音色权重")
    async def jingge_models(self) -> dict[str, Any]:
        try:
            resp = await self._client().get("/models")
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return {"models": [], "error": f"{type(exc).__name__}: {exc}"}
        current = self._load_voice()
        return {
            "models": data.get("models", []),
            "selected": {"model": current.get("model", ""), "index": current.get("index", "")},
        }

    @API("jingge_sessions", description="列出可以发送成品的群聊与会话")
    async def jingge_sessions(self, platform: str = "qq") -> dict[str, Any]:
        try:
            raw = await self.ctx.chat.get_all_streams(platform=platform or "qq")
        except Exception as exc:  # noqa: BLE001
            return {"sessions": [], "error": f"{type(exc).__name__}: {exc}"}

        data: Any = raw
        for _ in range(4):
            if not isinstance(data, dict):
                break
            for key in ("streams", "result", "data", "items"):
                if key in data:
                    data = data[key]
                    break
            else:
                break
        if not isinstance(data, list):
            return {"sessions": [], "error": "会话列表格式异常"}

        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in data:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("session_id") or item.get("stream_id") or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            # 私聊流没有 group_name，只有 user_* 字段，拼成「xx的私聊」
            name = (item.get("group_name") or item.get("session_name")
                    or item.get("name") or item.get("nickname"))
            if not name:
                who = (item.get("user_cardname") or item.get("user_nickname")
                       or item.get("user_id") or "用户")
                name = f"{who}的私聊"
            out.append({
                "stream_id": sid,
                "name": str(name),
                "group_id": str(item.get("group_id") or ""),
                "user_id": str(item.get("user_id") or ""),
                "platform": str(item.get("platform") or ""),
                "is_group": bool(item.get("group_id")),
            })
        return {"sessions": out}

    @API("jingge_render", description="按当前音色参数生成一首翻唱")
    async def jingge_render(self, payload: str) -> dict[str, Any]:
        try:
            data = json.loads(payload)
        except (ValueError, RecursionError):
            raise ValueError("参数不是合法 JSON") from None
        if not isinstance(data, dict):
            raise ValueError("参数必须是对象")
        song = str(data.get("song") or "").strip()
        if not song:
            raise ValueError("请先填写歌名")

        voice = self._save_voice(data.get("voice") if isinstance(data.get("voice"), dict) else {})
        body = {
            "song": song,
            "voice": {
                "pitch": int(voice["pitch"]),
                "index_rate": float(voice["index_rate"]),
                "protect": float(voice["protect"]),
                "volume_envelope": float(voice["volume_envelope"]),
                "f0_method": voice["f0_method"],
                "formant_shifting": bool(voice["formant_shifting"]),
                "formant_qfrency": float(voice["formant_qfrency"]),
                **({"model": voice["model"]} if voice.get("model") else {}),
                **({"index": voice["index"]} if voice.get("index") else {}),
            },
            "requester": "studio",
        }
        resp = await self._client().post("/cover", json=body)
        resp.raise_for_status()
        job = resp.json()
        return {
            "job_id": str(job.get("id") or ""),
            "queue_position": job.get("queue_position", 1),
            "eta_seconds": job.get("eta_seconds", 240),
        }

    @API("jingge_preview", description="试听工作台：用临时参数生成，不影响已保存的语音路由")
    async def jingge_preview(self, payload: str) -> dict[str, Any]:
        try:
            data = json.loads(payload)
        except (ValueError, RecursionError):
            raise ValueError("参数不是合法 JSON") from None
        if not isinstance(data, dict):
            raise ValueError("参数必须是对象")
        song = str(data.get("song") or "").strip()
        if not song:
            raise ValueError("请先填写歌名")

        draft = data.get("voice") if isinstance(data.get("voice"), dict) else {}
        # 以当前保存的配置为底，叠上试听参数，但绝不落盘
        merged = {**self._load_voice(), **draft}
        voice = self._save_voice(merged, persist=False)

        body = {
            "song": song,
            "voice": {
                "pitch": int(voice["pitch"]),
                "index_rate": float(voice["index_rate"]),
                "protect": float(voice["protect"]),
                "volume_envelope": float(voice["volume_envelope"]),
                "f0_method": voice["f0_method"],
                "formant_shifting": bool(voice["formant_shifting"]),
                "formant_qfrency": float(voice["formant_qfrency"]),
                **({"model": voice["model"]} if voice.get("model") else {}),
                **({"index": voice["index"]} if voice.get("index") else {}),
            },
            "requester": "playground",
        }
        resp = await self._client().post("/cover", json=body)
        resp.raise_for_status()
        job = resp.json()
        return {
            "job_id": str(job.get("id") or ""),
            "queue_position": job.get("queue_position", 1),
            "eta_seconds": job.get("eta_seconds", 240),
            "voice": voice,
        }

    @API("jingge_audio", description="分片取 60 秒试听音频（宿主单次返回限 512KB，所以切片传）")
    async def jingge_audio(self, job_id: str, part: int = 0, chunk: int = 0) -> dict[str, Any]:
        if not job_id.strip():
            raise ValueError("缺少任务号")
        resp = await self._client().get(
            f"/jobs/{job_id}/voice-part/{int(part)}", timeout=120.0,
        )
        if resp.status_code != 200:
            raise ValueError(f"第 {int(part) + 1} 段还没准备好（HTTP {resp.status_code}）")

        data = resp.content
        size = 240 * 1024                      # base64 后约 320KB，留足余量
        total = max(1, (len(data) + size - 1) // size)
        idx = min(max(int(chunk), 0), total - 1)
        piece = data[idx * size:(idx + 1) * size]

        return {
            "audio": base64.b64encode(piece).decode("ascii"),
            "chunk": idx,
            "chunks": total,
            "content_type": resp.headers.get("content-type") or "audio/mpeg",
            "size_kb": round(len(data) / 1024, 1),
            "part": int(part),
        }

    @API("jingge_file", description="分片取完整成品音频，供试听工作台下载")
    async def jingge_file(self, job_id: str, chunk: int = 0) -> dict[str, Any]:
        if not job_id.strip():
            raise ValueError("缺少任务号")
        resp = await self._client().get(f"/jobs/{job_id}/audio", timeout=180.0)
        if resp.status_code != 200:
            raise ValueError(f"成品还没准备好（HTTP {resp.status_code}）")

        data = resp.content
        size = 240 * 1024
        total = max(1, (len(data) + size - 1) // size)
        idx = min(max(int(chunk), 0), total - 1)
        piece = data[idx * size:(idx + 1) * size]

        return {
            "audio": base64.b64encode(piece).decode("ascii"),
            "chunk": idx,
            "chunks": total,
            "total_kb": round(len(data) / 1024, 1),
            "content_type": resp.headers.get("content-type") or "audio/mpeg",
        }

    @API("jingge_save_to", description="把翻唱成品直接保存到指定的本地路径（绕开内嵌浏览器不能弹另存为的限制）")
    async def jingge_save_to(self, job_id: str, path: str) -> dict[str, Any]:
        """把完整成品写到插件宿主（Windows）上的指定路径。

        path 可以是目录（自动用歌名命名）或以 .mp3 结尾的完整文件路径。
        """
        if not job_id.strip():
            raise ValueError("缺少任务号")
        raw = (path or "").strip().strip('"')
        if not raw:
            raise ValueError("请填写保存路径")

        target = Path(raw).expanduser()
        # 拒绝路径穿越（..），避免写到预期之外的地方
        if ".." in target.parts:
            raise ValueError("路径不能包含 ..")
        # 目录 or 以 .mp3 结尾当作文件名
        if target.suffix.lower() != ".mp3":
            target = target / "鲸歌翻唱.mp3"

        # 基本防护：禁止写入系统关键目录（该接口只绑 WebUI，仅宿主本人可调用，
        # 保留自选路径是刚需，这里只拦最危险的位置）
        resolved = str(target.resolve()).lower().replace("/", "\\")
        blocked = (
            "c:\\windows\\", "c:\\program files\\", "c:\\program files (x86)\\",
            "c:\\programdata\\", "\\system32\\",
        )
        if any(b in resolved for b in blocked):
            raise ValueError("出于安全考虑，不能保存到系统目录，请换个文件夹")

        resp = await self._client().get(f"/jobs/{job_id}/audio", timeout=180.0)
        if resp.status_code != 200:
            raise ValueError(f"成品还没准备好（HTTP {resp.status_code}）")

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(resp.content)
        except OSError as exc:
            raise ValueError(f"写入失败：{exc}") from None

        return {
            "path": str(target),
            "size_kb": round(len(resp.content) / 1024, 1),
        }

    @API("jingge_job", description="查询翻唱任务状态")
    async def jingge_job(self, job_id: str) -> dict[str, Any]:
        resp = await self._client().get(f"/jobs/{job_id}")
        resp.raise_for_status()
        return resp.json()

    @API("jingge_send", description="把成品发送到指定群聊")
    async def jingge_send(self, job_id: str, stream_id: str,
                          group_id: str = "", user_id: str = "") -> dict[str, Any]:
        resp = await self._client().get(f"/jobs/{job_id}")
        resp.raise_for_status()
        data = resp.json()
        await self._deliver(job_id, data, {
            "stream_id": stream_id,
            "group_id": group_id,
            "user_id": user_id,
            "song": data.get("song", ""),
        })
        return {"ok": True, "message": "已发送"}

    # ------------------------------------------------------------ 生命周期
    async def on_load(self) -> None:
        self._state_file = Path(self.ctx.paths.data_dir) / "pending_jobs.json"
        self._started_flag = Path(self.ctx.paths.data_dir) / "api_started_by_plugin.flag"
        self._load_state()
        self.ctx.logger.info("鲸歌插件已加载，API=%s，待恢复任务 %d 个",
                             self.config.cover.api_base, len(self._pending))
        for job_id, record in list(self._pending.items()):
            self._spawn(self._watch(job_id, record))
        # 重载/加载时也按需拉起服务（后台执行，避免阻塞加载）
        if self.config.cover.auto_start_api and (self.config.cover.api_start_cmd or "").strip():
            self._spawn(self._auto_start_on_load())

    async def _auto_start_on_load(self) -> None:
        """加载时若服务离线则尝试拉起，结果只记日志，不阻塞 on_load。"""
        try:
            # 重载时旧实例的停止命令可能仍在执行（走 WSL 约 1-2 秒），
            # 稍等再探测，避免「检测时还在线→随后被关」导致最终离线
            await asyncio.sleep(3.0)
            ok, msg = await self._ensure_service()
            if ok:
                self.ctx.logger.info("鲸歌：翻唱服务已就绪")
            else:
                self.ctx.logger.warning("鲸歌：加载时服务不可用：%s", msg)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("鲸歌：加载时自动拉起异常：%s", exc)

    async def on_unload(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            with contextlib.suppress(Exception):
                await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.aclose()
            self._http = None
        self._save_state()
        # 仅关闭由本插件拉起的服务（有标记），避免误杀用户手动启动的 API
        stop_msg = await self._stop_api()
        self.ctx.logger.info("鲸歌插件已卸载，任务表已保存；服务关闭：%s", stop_msg)

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == "bot":
            self.ctx.logger.info("配置已更新 (scope=%s, version=%s)", scope, version)

    # ------------------------------------------------------------ 工具：提交
    @Tool(
        "feiyu_cover",
        description=(
            "用小鲸的音色翻唱一首歌，3-5 分钟后成品自动发到群里。\n"
            "\n"
            "【该调用】群友说「翻唱 XX」「点歌翻唱 XX」「唱一首 XX」「用小鲸/你的声音唱 XX」"
            "「让小鲸唱 XX」「你会唱 XX 吗」，或者回复某首歌说「来一首」。\n"
            "\n"
            "【不该调用】只是聊到某首歌、问你听没听过；已经有任务在跑时不要重复提交；"
            "对方只是想听原唱。\n"
            "\n"
            "【参数】song 填歌名，可带歌手名帮助匹配，例如「晴天 周杰伦」；"
            "pitch 是变调半音，不确定就留空（默认 0），男声原曲改成女声填 12。\n"
            "\n"
            "调用后立即返回接单结果，不要等待生成完成，也不要反复调用同一个请求。\n"
            "重要：不要擅自改写用户点播的歌曲信息。用户说了版本（如「司凤版」「女声版」「Live」）"
            "就按原样传，不要替换成原版，也不要自己加歌手名覆盖用户给的信息。"
        ),
        parameters=[
            ToolParameterInfo(
                name="song",
                param_type=ToolParamType.STRING,
                description=(
                    "歌曲名，可带歌手名帮助匹配，例如「晴天 周杰伦」。"
                    "**用户指定了版本、翻唱者或编曲时，必须原样保留，不得删改或替换成原版**，"
                    "例如「雨爱 司凤版」「起风了 女声版」「晴天 Live」都要照抄。"
                    "只有用户没提版本时，才用「歌名 歌手」的形式。"
                ),
                required=True,
            ),
            ToolParameterInfo(
                name="pitch",
                param_type=ToolParamType.INTEGER,
                description="变调半音数。默认 0；男声原曲改成女声需要 12，不确定就不要填",
                required=False,
            ),
        ],
    )
    async def handle_cover(self, song: str = "", **kwargs: Any) -> dict[str, Any]:
        return await self._submit(song, kwargs, from_tool=True)

    @Tool(
        "feiyu_cover_status",
        description=(
            "查询翻唱任务做到哪一步了。群友问「唱好了没」「还要多久」「进度怎么样」时调用。\n"
            "不传 job_id 会返回最近一个任务的状态，通常够用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="job_id",
                param_type=ToolParamType.STRING,
                description="任务编号，可留空",
                required=False,
            ),
        ],
    )
    async def handle_status(self, job_id: str = "", **kwargs: Any) -> dict[str, Any]:
        if not self._pending:
            return {"success": True, "message": "当前没有正在处理的翻唱任务"}
        if not job_id:
            job_id = list(self._pending.keys())[-1]
        try:
            data = await self._get_job(job_id)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "message": f"查询失败: {exc}"}
        return {"success": True, "message": self._describe(data)}

    # ------------------------------------------------------------ 命令：兜底
    @Command(
        "feiyu_cover_cmd",
        description="翻唱一首歌，例如：/翻唱 晴天",
        pattern=r"^(?:/翻唱|翻唱)\s*(?P<song>.+)$",
    )
    async def handle_cover_cmd(self, stream_id: str = "", matched_groups: dict | None = None,
                               **kwargs: Any) -> tuple[bool, str, int]:
        song = ((matched_groups or {}).get("song") or "").strip()
        if not song:
            await self.ctx.send.text("用法：/翻唱 歌名", stream_id)
            return True, "缺少歌名", 2
        result = await self._submit(song, {**kwargs, "stream_id": stream_id}, from_tool=False)
        if not result.get("success"):
            await self.ctx.send.text(result.get("message", "提交失败"), stream_id)
        return True, result.get("message", ""), 2

    # ------------------------------------------------------------ 提交逻辑
    async def _submit(self, song: str, kwargs: dict[str, Any], *, from_tool: bool) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return {"success": False, "message": "翻唱功能已关闭"}

        song = (song or "").strip()
        if not song:
            return {"success": False, "message": "没有收到歌名"}

        # 服务可用性检查（必要时按配置自动拉起），失败直接给用户明确提示
        ok, hint = await self._ensure_service()
        if not ok:
            return {"success": False, "message": hint}

        stream_id = str(kwargs.get("stream_id") or "")
        user_id = self._extract(kwargs, ("user_id", "sender_id", "qq", "operator_id"))
        group_id = self._group_id(kwargs)
        nickname = self._extract(kwargs, ("nickname", "sender_name", "user_name")) or user_id

        # 冷却：同一用户短时间内只接一单
        cooldown = int(self.config.cover.cooldown_seconds)
        if user_id and cooldown > 0:
            last = self._cooldown.get(user_id, 0)
            remain = int(cooldown - (time.time() - last))
            if remain > 0:
                return {"success": False,
                        "message": f"先歇会儿，{remain // 60 + 1} 分钟后才能再点（每人每 {cooldown // 60} 分钟一首）"}

        # 同一会话已有排队任务时不再叠加
        for existing in self._pending.values():
            if stream_id and existing.get("stream_id") == stream_id:
                return {"success": False,
                        "message": f"《{existing.get('song')}》还在做，等这首出来再点吧"}

        # 音色参数：每次点歌现读 voice.json，页面改完保存后下一次就生效
        voice_cfg = self._load_voice()
        pitch_value = kwargs.get("pitch")
        if pitch_value is None or pitch_value == "":
            pitch_value = voice_cfg["pitch"]
        payload: dict[str, Any] = {
            "song": song,
            "voice": {
                "pitch": int(pitch_value),
                "index_rate": float(voice_cfg["index_rate"]),
                "protect": float(voice_cfg["protect"]),
                "volume_envelope": float(voice_cfg["volume_envelope"]),
                "f0_method": voice_cfg["f0_method"],
                "formant_shifting": bool(voice_cfg["formant_shifting"]),
                "formant_qfrency": float(voice_cfg["formant_qfrency"]),
                **({"model": voice_cfg["model"]} if voice_cfg.get("model") else {}),
                **({"index": voice_cfg["index"]} if voice_cfg.get("index") else {}),
            },
            "requester": nickname,
            "stream_id": stream_id,
        }

        try:
            # 提交只需拿任务号，不必等 30 秒；超时快点失败好给用户反馈
            data = await self._post("/cover", payload, timeout=15.0)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("提交翻唱失败: %s", exc)
            return {"success": False,
                    "message": f"翻唱服务没响应（{exc}）。请确认 API 服务在运行、地址配置正确。"}

        job_id = data.get("id", "")
        position = int(data.get("queue_position", 1))
        eta = int(data.get("eta_seconds", 240))

        record = {
            "job_id": job_id,
            "song": song,
            "stream_id": stream_id,
            "group_id": group_id,
            "user_id": user_id,
            "nickname": nickname,
            "created": time.time(),
            "announced": False,
        }
        self._pending[job_id] = record
        self._save_state()
        if user_id:
            self._cooldown[user_id] = time.time()

        # 接单必须吭一声：工具调用只把结果回给模型，群里是不出声的，
        # 不说一句用户会以为没反应。
        if stream_id and self.config.cover.notify_progress:
            wait = "马上" if position <= 1 else f"前面还有 {position - 1} 首"
            with contextlib.suppress(Exception):
                await self.ctx.send.text(
                    f"好的，{self._bot_name()}在唱了～《{song}》{wait}就好",
                    stream_id,
                )

        # 接单播报已在上面统一发过，这里不再重复（否则群里出现两条）
        self._spawn(self._watch(job_id, record))

        if from_tool:
            # 只回一句，避免模型自己加戏（stop_after_execution 让本轮到此为止）
            return {
                "success": True,
                "message": "好，我这就去唱~",
                "job_id": job_id,
                "stop_after_execution": True,
            }
        return {"success": True, "message": "好，我这就去唱~", "job_id": job_id,
                "stop_after_execution": True}

    # ------------------------------------------------------------ 后台轮询
    async def _watch(self, job_id: str, record: dict[str, Any]) -> None:
        deadline = time.time() + int(self.config.cover.max_wait_seconds)
        interval = max(3, int(self.config.cover.poll_interval))
        last_step = ""

        while time.time() < deadline:
            await asyncio.sleep(interval)
            try:
                data = await self._get_job(job_id)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("轮询失败 %s: %s", job_id, exc)
                continue

            status = data.get("status", "")
            step = data.get("message", "")
            if status == "done":
                await self._deliver(job_id, data, record)
                self._pending.pop(job_id, None)
                self._save_state()
                return
            if status in ("failed", "interrupted"):
                reason = data.get("error") or data.get("message") or "未知原因"
                # 分离后发现没主唱（伴奏版）→ 自动换下一个搜索结果重试
                if "没有可用人声" in str(reason):
                    retried = int(record.get("candidate_index") or 0) + 1
                    if retried <= 2:
                        self.ctx.logger.info("第 %d 版是伴奏，换下一个候选", retried)
                        self._pending.pop(job_id, None)
                        self._save_state()
                        await self._retry_candidate(record, retried)
                        return
                if record.get("stream_id"):
                    with contextlib.suppress(Exception):
                        await self.ctx.send.text(
                            f"《{record.get('song')}》没唱成：{str(reason)[:120]}",
                            record["stream_id"],
                        )
                self._pending.pop(job_id, None)
                self._save_state()
                return

            if step and step != last_step:
                last_step = step
                self.ctx.logger.info("[%s] %s", job_id, step)

        self.ctx.logger.warning("任务 %s 等待超时", job_id)
        self._pending.pop(job_id, None)
        self._save_state()

    # ------------------------------------------------------------ 发送成品
    async def _retry_candidate(self, record: dict[str, Any], index: int) -> None:
        """换搜索结果里的下一个版本重试（当前版本分离出来没有主唱）。"""
        try:
            payload = {
                "song": record.get("song"),
                "candidate_index": index,
                "requester": record.get("requester", ""),
                "stream_id": record.get("stream_id", ""),
            }
            if record.get("voice"):
                payload["voice"] = record["voice"]
            resp = await self._client().post("/cover", json=payload, timeout=60.0)
            resp.raise_for_status()
            data = resp.json()
            new_id = data.get("id") or data.get("job_id")
            if not new_id:
                raise RuntimeError("接口没返回任务号")
            record = dict(record, candidate_index=index)
            self._pending[new_id] = record
            self._save_state()
            self._spawn(self._watch(new_id, record))
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("换候选重试失败：%s", exc)
            if record.get("stream_id"):
                with contextlib.suppress(Exception):
                    await self.ctx.send.text(
                        f"《{record.get('song')}》几个版本都没有主唱，换首别的吧",
                        record["stream_id"],
                    )

    async def _deliver(self, job_id: str, data: dict, record: dict[str, Any]) -> None:
        stream_id = record.get("stream_id") or ""
        title = (data.get("meta") or {}).get("title") or record.get("song") or "翻唱"

        # 用的是翻唱音频时先说明一句，别让人以为那就是原曲
        meta = data.get("meta") or {}
        if meta.get("suspect_cover") and stream_id:
            with contextlib.suppress(Exception):
                await self.ctx.send.text(
                    f"没找到《{title}》的原唱版本，用的是翻唱音频哦", stream_id,
                )

        # 兜底链：语音条 → 完整文件 → 文本提示
        sent = await self._send_voice_parts(job_id, title, stream_id)
        if sent > 0:
            return
        self.ctx.logger.warning("语音条发送失败，尝试降级为完整文件：%s", job_id)
        if await self._send_file(job_id, title, stream_id):
            return
        if stream_id:
            with contextlib.suppress(Exception):
                await self.ctx.send.text(
                    f"《{title}》唱好了，但发送失败，请去服务日志查看或到 WebUI 手动下载", stream_id,
                )

    async def _send_voice_parts(self, job_id: str, title: str, stream_id: str) -> int:
        """把整首歌按 60 秒切成多条语音条发送。

        QQ 单条语音有 60 秒上限，所以分段发；语音条点开就听，不用下载。
        返回成功发送的段数。
        """
        if not stream_id:
            return 0
        seconds = 60
        max_parts = 2          # 只发前 2 段（2 分钟），够听出效果又不刷屏
        try:
            info = await self._client().get(
                f"/jobs/{job_id}/voice-info", params={"seconds": seconds}, timeout=30.0,
            )
            info.raise_for_status()
            parts = min(int(info.json().get("parts", 1)), max_parts)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("获取语音分段信息失败：%s", exc)
            return 0

        ok_count = 0
        for i in range(parts):
            try:
                resp = await self._client().get(
                    f"/jobs/{job_id}/voice-part/{i}",
                    params={"seconds": seconds}, timeout=120.0,
                )
                if resp.status_code != 200:
                    self.ctx.logger.warning("第 %d 段不可用：HTTP %s", i + 1, resp.status_code)
                    break
                audio = resp.content
                segment = {
                    "type": "voice",
                    "data": f"{title} {i+1}/{parts}",
                    "hash": hashlib.sha256(audio).hexdigest(),
                    "binary_data_base64": base64.b64encode(audio).decode("ascii"),
                }
                sent = await self.ctx.send.hybrid(
                    [segment], stream_id, processed_plain_text=title,
                )
                ok = sent.get("sent") is True if isinstance(sent, dict) else bool(sent)
                if not ok:
                    self.ctx.logger.warning("第 %d 段发送未成功：%s", i + 1, sent)
                    break
                ok_count += 1
                if i < parts - 1:
                    await asyncio.sleep(1.0)   # 稍作间隔，别触发风控
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.error("第 %d 段失败：%s", i + 1, exc)
                break

        if ok_count:
            self.ctx.logger.info("语音条发送完成：%d/%d 段", ok_count, parts)
        return ok_count

    async def _send_file(self, job_id: str, title: str, stream_id: str) -> bool:
        """再补一份完整文件（URL 段，适配器自己下载）。"""
        if not stream_id:
            return False
        base = (self.config.cover.api_base or "").rstrip("/")
        if not base:
            return False
        safe_title = "".join(ch for ch in title if ch not in '\\/:*?"<>|')[:32] or "cover"
        segment = {
            "type": "file",
            "data": {
                "name": f"{safe_title}.mp3",
                "url": f"{base}/jobs/{job_id}/audio",
                "mime_type": "audio/mpeg",
            },
        }
        try:
            sent = await self.ctx.send.hybrid([segment], stream_id, processed_plain_text=safe_title)
            ok = sent.get("sent") is True if isinstance(sent, dict) else bool(sent)
            if ok:
                self.ctx.logger.info("完整文件已发送")
            return ok
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error("完整文件发送失败：%s", exc)
            return False

    async def _download_audio(self, job_id: str) -> bytes:
        resp = await self._client().get(f"/jobs/{job_id}/audio", timeout=120.0)
        resp.raise_for_status()
        return resp.content

    # ------------------------------------------------------------ 杂项
    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    @staticmethod
    def _group_id(kwargs: dict[str, Any]) -> str:
        """从 message 结构里取群号；私聊返回空串。"""
        message = kwargs.get("message")
        if isinstance(message, dict):
            info = message.get("message_info")
            if isinstance(info, dict):
                group = info.get("group_info")
                if isinstance(group, dict) and group.get("group_id"):
                    return str(group["group_id"])
        for key in ("group_id", "chat_id"):
            value = kwargs.get(key)
            if value not in (None, ""):
                return str(value)
        return ""

    @staticmethod
    def _extract(kwargs: dict[str, Any], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = kwargs.get(key)
            if value not in (None, ""):
                return str(value)
        message = kwargs.get("message")
        if isinstance(message, dict):
            for key in keys:
                value = message.get(key)
                if value not in (None, ""):
                    return str(value)
        return ""

    @staticmethod
    def _describe(data: dict) -> str:
        status = data.get("status")
        if status == "done":
            return "已经唱好了"
        if status in ("failed", "interrupted"):
            return f"失败了：{data.get('error') or data.get('message')}"
        message = data.get("message") or "处理中"
        progress = int(float(data.get("progress", 0)) * 100)
        return f"《{data.get('song')}》{message}（{progress}%）"

    def _state_path(self) -> Path | None:
        """惰性解析状态文件路径，避免依赖 on_load 的调用顺序。"""
        if self._state_file is None:
            try:
                self._state_file = Path(self.ctx.paths.data_dir) / "pending_jobs.json"
            except Exception:  # noqa: BLE001
                return None
        return self._state_file

    def _load_state(self) -> None:
        state_file = self._state_path()
        if state_file is None or not state_file.is_file():
            return
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            cooldown = data.pop("__cooldown__", {})
            self._pending = {k: v for k, v in data.items() if isinstance(v, dict)}
            if isinstance(cooldown, dict):
                self._cooldown = {str(k): float(v) for k, v in cooldown.items()}
        except (OSError, ValueError) as exc:
            self.ctx.logger.warning("读取任务表失败: %s", exc)

    def _save_state(self) -> None:
        state_file = self._state_path()
        if state_file is None:
            return
        try:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = dict(self._pending)
            payload["__cooldown__"] = self._cooldown
            # 原子写：先写临时文件再替换，避免崩溃时留下半个 json
            tmp = state_file.with_suffix(state_file.suffix + ".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(state_file)
        except OSError as exc:
            self.ctx.logger.warning("保存任务表失败: %s", exc)


def create_plugin() -> FeiyuCoverPlugin:
    return FeiyuCoverPlugin()
