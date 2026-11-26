"""麦弦（MaiChord）· 麦麦声学与翻唱插件

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
import contextlib
import hashlib
import html as html_lib
import json
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
import threading
import uuid
from pathlib import Path
from typing import Any, ClassVar, Iterable, Literal
from urllib.parse import urlparse

from maibot_sdk import API, Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType
from pydantic import field_validator

try:  # MaiBot SDK >= 2.23 支持配置页运行时状态卡片与快捷按钮
    from maibot_sdk.config_widgets import ConfigWidget, ConfigWidgetState
except ImportError:  # pragma: no cover
    ConfigWidget = None  # type: ignore[assignment,misc]
    ConfigWidgetState = None  # type: ignore[assignment,misc]

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
    """翻唱服务的连接与点歌策略。★音色模型、试听与干声训练请直接点左侧边栏「麦弦」。"""

    __ui_label__ = "翻唱服务"
    __ui_icon__ = "server"
    __ui_order__ = 1

    api_base: str = Field(
        default="http://127.0.0.1:19655",
        description="本机部署保持默认即可；远程显卡服务器填 http://服务器IP:19655",
        json_schema_extra={
            "label": "服务地址",
            "placeholder": "http://127.0.0.1:19655",
        },
    )
    api_key: str = Field(
        default="",
        description="API 的 Bearer 访问密钥；服务端未设置就留空",
        json_schema_extra={
            "label": "访问密钥",
            "placeholder": "未设密钥可留空",
            "input_type": "password",
        },
    )
    auto_start_api: bool = Field(
        default=True,
        description="服务未启动时自动在后台拉起，无需手写启动命令",
        json_schema_extra={"label": "自动拉起本地服务"},
    )
    api_dir: str = Field(
        default="",
        description="粘贴 feiyu-cover-api 文件夹路径即可（如 G:\\feiyu-cover 或 ~/feiyu-cover-api）；留空会自动探测常见路径",
        json_schema_extra={
            "label": "API 文件夹路径",
            "placeholder": "留空自动探测，或填 G:\\feiyu-cover",
        },
    )
    api_runtime: Literal["自动识别（推荐）", "Windows WSL2", "本机直接运行"] = Field(
        default="自动识别（推荐）",
        description="Windows 优先检测一键包本机环境，否则自动通过 WSL2 启动；Linux 直接本机运行",
        json_schema_extra={"label": "本地运行环境"},
    )
    auto_stop_api: bool = Field(
        default=True,
        description="卸载时关闭绑定项目和端口的本地 API，包括升级前启动的实例；共享服务请关闭此项",
        json_schema_extra={"label": "卸载时自动关闭"},
    )
    # 兼容旧版与进阶用户：在 config.toml 中手写自定义命令时优先生效，配置页默认隐藏不打扰新手
    api_start_cmd: str = Field(
        default="",
        description="（进阶覆盖）自定义启动命令，留空则根据「API 文件夹路径」自动拉起",
        json_schema_extra={"label": "自定义启动命令", "hidden": True},
    )
    api_stop_cmd: str = Field(
        default="",
        description="（进阶覆盖）自定义停止命令，留空则自动按拉起方式安全关闭",
        json_schema_extra={"label": "自定义停止命令", "hidden": True},
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
    cache_deliver_delay: int = Field(
        default=3, ge=0, le=60,
        description="命中缓存的成品额外延迟几秒发，让开唱到出成品的时序自然",
        json_schema_extra={"label": "缓存成品延迟（秒）"},
    )
    deliver_mode: Literal[
        "副歌高潮直出（58秒·推荐）",
        "从头分段发送（前2段×60秒）",
        "整首完整分段发送（每段60秒·连续发完）",
        "直接发送完整 MP3 音频文件",
    ] = Field(
        default="副歌高潮直出（58秒·推荐）",
        description=(
            "副歌高潮直出：张嘴即高潮且不刷屏；从头分段发送：连续发前 2 段语音条；"
            "整首完整分段发送：按 60 秒连续发完整首歌曲；完整 MP3 文件：单条音频文件直发（不支持文件的平台自动回退整首语音）"
        ),
        json_schema_extra={"label": "群聊发歌模式"},
    )
    max_voice_parts: int = Field(
        default=8, ge=2, le=15,
        description="仅「整首完整分段发送」生效：单首歌最多连续发送的 60 秒语音段数上限（默认 8 段 ≈ 8 分钟，防超长串烧刷屏）",
        json_schema_extra={"label": "整首语音最大段数"},
    )
    voice_part_interval: float = Field(
        default=1.8, ge=0.5, le=10.0,
        description="多段语音条连续发送时的段间停顿秒数（默认 1.8 秒，降低顺序错乱与平台限频风险）",
        json_schema_extra={"label": "分段发送间隔（秒）"},
    )
    send_song_card: bool = Field(
        default=True,
        description="唱完附带一张精致黑胶单曲卡（需宿主渲染能力，不可用时自动跳过）",
        json_schema_extra={"label": "发送黑胶单曲卡"},
    )

    @field_validator("api_runtime", mode="before")
    @classmethod
    def _compat_api_runtime(cls, v: Any) -> Any:
        mapping = {
            "auto": "自动识别（推荐）",
            "wsl": "Windows WSL2",
            "local": "本机直接运行",
        }
        return mapping.get(str(v or "").strip().lower(), v)

    @field_validator("deliver_mode", mode="before")
    @classmethod
    def _compat_deliver_mode(cls, v: Any) -> Any:
        mapping = {
            "highlight": "副歌高潮直出（58秒·推荐）",
            "two_parts": "从头分段发送（前2段×60秒）",
            "full_voice": "整首完整分段发送（每段60秒·连续发完）",
            "full": "整首完整分段发送（每段60秒·连续发完）",
            "all_parts": "整首完整分段发送（每段60秒·连续发完）",
            "full_file": "直接发送完整 MP3 音频文件",
            "file": "直接发送完整 MP3 音频文件",
        }
        return mapping.get(str(v or "").strip().lower(), v)


class FeaturesSection(PluginConfigBase):
    """控制猜歌小游戏与桌宠联动等扩展玩法的开关。"""

    __ui_label__ = "玩法开关"
    __ui_icon__ = "sparkles"
    __ui_order__ = 2

    enable_quiz: bool = Field(
        default=True,
        description="开启后群友可发「/猜歌」或自然语言玩 15 秒清唱猜歌小游戏",
        json_schema_extra={"label": "清唱猜歌小游戏"},
    )
    enable_desktop_pet: bool = Field(
        default=True,
        description="开启麦麦桌宠时，点歌接单、唱完与猜歌会触发桌宠气泡和动作（未开桌宠自动跳过）",
        json_schema_extra={"label": "桌宠小歌姬联动"},
    )


class ScopeSection(PluginConfigBase):
    """按群聊 / 私聊黑白名单控制麦弦翻唱与猜歌的生效范围。"""

    __ui_label__ = "生效范围（黑白名单）"
    __ui_icon__ = "shield"
    __ui_order__ = 3

    allow_group: bool = Field(
        default=True,
        description="是否允许在群聊中点歌翻唱与玩清唱猜歌",
        json_schema_extra={"label": "允许群聊使用"},
    )
    allow_private: bool = Field(
        default=True,
        description="是否允许在私聊中点歌翻唱与玩清唱猜歌",
        json_schema_extra={"label": "允许私聊使用"},
    )
    group_list_mode: Literal["不限制（全部群聊）", "仅白名单群聊生效", "排除黑名单群聊"] = Field(
        default="不限制（全部群聊）",
        description="配合下方「群聊名单」生效：白名单仅放行列表内群聊，黑名单屏蔽列表内群聊",
        json_schema_extra={"label": "群聊名单模式"},
    )
    group_list: list[str] = Field(
        default_factory=list,
        description="填写群号 / 群 ID 或群名（兼容 QQ、QQ 官方、Telegram 群/话题 ID 等）",
        json_schema_extra={"label": "群聊黑白名单", "placeholder": "输入群号 / 群 ID 或群名"},
    )
    private_list_mode: Literal["不限制（全部私聊）", "仅白名单私聊生效", "排除黑名单私聊"] = Field(
        default="不限制（全部私聊）",
        description="配合下方「私聊名单」生效：白名单仅放行列表内用户，黑名单屏蔽列表内用户",
        json_schema_extra={"label": "私聊名单模式"},
    )
    private_list: list[str] = Field(
        default_factory=list,
        description="填写私聊用户 ID 或昵称（兼容 QQ 号/OpenID、Telegram ID、微信 ID 等）",
        json_schema_extra={"label": "私聊黑白名单", "placeholder": "输入用户 ID 或昵称"},
    )

    @field_validator("group_list_mode", mode="before")
    @classmethod
    def _compat_group_mode(cls, v: Any) -> Any:
        mapping = {
            "all": "不限制（全部群聊）",
            "none": "不限制（全部群聊）",
            "whitelist": "仅白名单群聊生效",
            "blacklist": "排除黑名单群聊",
        }
        return mapping.get(str(v or "").strip().lower(), v)

    @field_validator("private_list_mode", mode="before")
    @classmethod
    def _compat_private_mode(cls, v: Any) -> Any:
        mapping = {
            "all": "不限制（全部私聊）",
            "none": "不限制（全部私聊）",
            "whitelist": "仅白名单私聊生效",
            "blacklist": "排除黑名单私聊",
        }
        return mapping.get(str(v or "").strip().lower(), v)


# 音色参数不再走 config.toml：由侧边栏页面「鲸歌」直接编辑插件数据目录下的 voice.json，
# 好处是改完即时生效，不用碰宿主托管的配置文件。
VOICE_DEFAULTS: dict[str, Any] = {
    "pitch": 0,
    "auto_pitch": True,
    "index_rate": 0.28,
    "protect": 0.45,
    "volume_envelope": 0.25,
    "f0_method": "rmvpe",
    "f0_autotune": False,
    "f0_autotune_strength": 0.5,
    "formant_shifting": False,
    "formant_qfrency": 1.0,
    "vocal_reverb": True,
}

VOICE_LIMITS: dict[str, tuple[float, float]] = {
    "pitch": (-12.0, 12.0),
    "index_rate": (0.0, 1.0),
    "protect": (0.0, 0.5),
    "volume_envelope": (0.0, 1.0),
    "formant_qfrency": (0.5, 1.5),
    "f0_autotune_strength": (0.0, 1.0),
}

VOICE_F0_METHODS = ("rmvpe", "crepe")

# 60 秒热歌现学极速猜歌内置曲池（本地无缓存或点「/猜歌 新歌」时随机抽取现学）
QUIZ_HOT_SONGS: tuple[str, ...] = (
    "大鱼 周深", "起风了 买辣椒也用券", "雨爱 杨丞琳", "月牙湾 飞儿乐团",
    "稻香 周杰伦", "小幸运 田馥甄", "孤勇者 陈奕迅", "暖暖 梁静茹",
    "勾指起誓 洛天依", "海底 一支榴莲", "漠河舞厅 柳爽", "光年之外 邓紫棋",
    "泡沫 邓紫棋", "年轮 张碧晨", "凉凉 张碧晨", "赤伶 HITA",
    "红昭愿 音阙诗听", "芒种 音阙诗听", "探窗", "偏爱 张芸京",
    "水星记 郭顶", "理想三旬 陈鸿宇", "夜空中最亮的星 逃跑计划", "后来 刘若英",
    "体面 于文文", "说散就散 袁娅维", "追光者 岑宁儿", "绿色 陈雪凝",
    "你的答案 阿冗", "白月光与朱砂痣 大籽", "星辰大海 黄霄雲",
    "Lemon 米津玄师", "打上花火", "千本樱", "青鸟 生物股长",
)


class FeiyuCoverConfig(PluginConfigBase):
    """配置根模型：必须是嵌套节，宿主从中提取 plugin.config_version。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    cover: CoverSection = Field(default_factory=CoverSection)
    features: FeaturesSection = Field(default_factory=FeaturesSection)
    scope: ScopeSection = Field(default_factory=ScopeSection)


class FeiyuCoverPlugin(MaiBotPlugin):
    config_model = FeiyuCoverConfig
    config_reload_subscriptions: ClassVar[Iterable[str]] = ("bot",)
    config_widgets: ClassVar[list[Any]] = (
        [
            ConfigWidget(id="api_status", kind="status", label="翻唱 API 连接状态"),
            ConfigWidget(id="quick_start", kind="action", label="一键检测 / 拉起服务"),
        ]
        if ConfigWidget is not None
        else []
    )

    def __init__(self) -> None:
        # 必须调用基类构造：self.config 依赖它初始化的 _plugin_config_data / _plugin_config_instance
        super().__init__()
        self._tasks: set[asyncio.Task] = set()
        self._audio_lock = asyncio.Lock()
        self._audio_cache = {}
        self._source_uploads: dict[str, dict[str, Any]] = {}
        self._audio_fill = None
        self._native_save_lock = threading.Lock()
        self._native_save_active: dict[str, Any] | None = None
        self._native_save_states: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._cooldown: dict[str, float] = {}
        self._state_file: Path | None = None
        self._http: httpx.AsyncClient | None = None
        self._http_sig: tuple[str, str] = ("", "")
        self._started_flag: Path | None = None  # 标记「服务由本插件拉起」的文件
        self._starting: bool = False  # 是否正在自动拉起服务（供 WebUI 显示状态）
        self._last_start_error: str = ""
        self._unloading = False
        self._start_lock: asyncio.Lock | None = None
        self._bg_proc: subprocess.Popen[bytes] | None = None
        self._wsl_distro_cache: str | None = None
        self._quizzes: dict[str, dict[str, Any]] = {}  # stream_id -> 正在进行的清唱猜歌局
        self._quiz_preparing: set[str] = set()        # 正在后台现学 60s 热歌的 stream_id
        self._bot_nickname: str = ""
        self._stream_cache: dict[str, dict[str, Any]] = {}
        self._stream_cache_ts: float = 0.0

    def _get_start_lock(self) -> asyncio.Lock:
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        return self._start_lock

    # -------------------------------------------------------- 配置页运行时控件
    async def get_config_widget_state(self) -> dict[str, Any]:
        if ConfigWidgetState is None:
            return {}
        health = await self._health_info()
        base = (self.config.cover.api_base or "http://127.0.0.1:19655").strip()
        if health is not None:
            self._last_start_error = ""
            ver = health.get("version") or ""
            ver_txt = f" v{ver}" if ver else ""
            return {
                "api_status": ConfigWidgetState(
                    text=f"服务在线{ver_txt}（{base}），可随时点歌或打开侧边栏「麦弦」",
                    tone="success",
                ),
                "quick_start": ConfigWidgetState(text="重新检测状态", tone="neutral"),
            }
        if self._starting:
            return {
                "api_status": ConfigWidgetState(
                    text="正在后台自动拉起本地翻唱 API，请稍候 3~10 秒后刷新…",
                    tone="warning",
                ),
                "quick_start": ConfigWidgetState(text="拉起中…", tone="warning", disabled=True),
            }
        detected = self._discover_api_dir()
        if self._last_start_error:
            tip = f"拉起失败：{self._last_start_error}"
            tone = "error"
        elif detected:
            tip = f"服务离线（{base}）· 已识别目录：{detected}，点右侧按钮可直接拉起"
            tone = "warning"
        elif (self.config.cover.api_start_cmd or "").strip():
            tip = f"服务离线（{base}）· 已配置自定义启动命令，点右侧按钮可拉起"
            tone = "warning"
        elif self._is_local_api():
            tip = f"服务离线（{base}）· 请填写「API 文件夹路径」后保存，无需手写启动命令"
            tone = "error"
        else:
            tip = f"无法连接远程服务（{base}）· 请确认远端 API 已运行"
            tone = "error"
        return {
            "api_status": ConfigWidgetState(text=tip, tone=tone),
            "quick_start": ConfigWidgetState(text="立即检测并拉起", tone="neutral"),
        }

    async def on_config_widget_action(self, action_id: str) -> None:
        if action_id != "quick_start":
            return
        if await self._health_ok() or self._starting:
            return
        if self._can_auto_start(ignore_switch=True):
            self._starting = True
            self._last_start_error = ""
            self._spawn(self._start_api())

    # ------------------------------------------------------------ HTTP 客户端
    def _client(self) -> "httpx.AsyncClient":
        """复用的 AsyncClient（带 api_base 与可选 Bearer Key；配置变更自动重建）。"""
        if httpx is None:
            raise RuntimeError("缺少 httpx 依赖，检查 manifest 的 dependencies")
        base = (self.config.cover.api_base or "").rstrip("/")
        key = (self.config.cover.api_key or "").strip()
        sig = (base, key)
        if self._http is None or self._http_sig != sig:
            old = self._http
            if old is not None:
                with contextlib.suppress(Exception):
                    self._spawn(old.aclose())
            headers: dict[str, str] = {}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            self._http = httpx.AsyncClient(
                base_url=base,
                headers=headers,
                timeout=30.0,
            )
            self._http_sig = sig
        return self._http

    async def _health_info(self, timeout: float = 2.0) -> dict[str, Any] | None:
        """探测 API 是否在线并返回 /health JSON。"""
        try:
            resp = await self._client().get("/health", timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            pass
        return None

    async def _health_ok(self, timeout: float = 2.0) -> bool:
        """探测 API 是否在线。"""
        return (await self._health_info(timeout=timeout)) is not None

    # ------------------------------------------------ 零命令本地服务自动拉起/关闭
    def _api_port(self) -> int:
        parsed = urlparse((self.config.cover.api_base or "http://127.0.0.1:19655").strip())
        return parsed.port or (443 if parsed.scheme == "https" else 80)

    def _is_local_api(self) -> bool:
        try:
            url = urlparse((self.config.cover.api_base or "").strip())
            return (url.scheme in ("http", "https") and url.hostname in ("localhost", "127.0.0.1", "::1")
                    and url.username is None and url.password is None and url.port != 0
                    and url.path in ("", "/") and not url.query and not url.fragment)
        except ValueError:
            return False

    @staticmethod
    def _win_to_wsl_path(raw: str) -> str:
        s = (raw or "").strip().strip('"').strip("'")
        m = re.match(r"^([a-zA-Z]):[\\/](.*)$", s)
        if m:
            drive = m.group(1).lower()
            rest = m.group(2).replace("\\", "/").rstrip("/")
            return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"
        return s.replace("\\", "/").rstrip("/")

    def _detect_wsl_distro(self) -> str:
        """自动选择可用的 WSL Linux 发行版（跳过 docker-desktop）。"""
        if self._wsl_distro_cache is not None:
            return self._wsl_distro_cache
        if os.name != "nt" or not shutil.which("wsl.exe"):
            self._wsl_distro_cache = ""
            return ""
        try:
            res = subprocess.run(
                ["wsl.exe", "-l", "-q"],
                capture_output=True,
                timeout=3.0,
                check=False,
            )
            raw = res.stdout or b""
            text = raw.decode("utf-16-le", errors="ignore") if b"\x00" in raw else raw.decode("utf-8", errors="ignore")
            distros = [line.strip().lstrip("\ufeff") for line in text.splitlines() if line.strip()]
            linux_distros = [d for d in distros if not d.lower().startswith("docker-desktop")]
            preferred = [d for d in linux_distros if any(k in d.lower() for k in ("ubuntu", "debian"))]
            self._wsl_distro_cache = (preferred or linux_distros or [""])[0]
        except Exception:  # noqa: BLE001
            self._wsl_distro_cache = ""
        return self._wsl_distro_cache

    def _discover_api_dir(self) -> str:
        """返回用户配置的 API 目录，或自动探测本机常见路径。"""
        configured = (self.config.cover.api_dir or "").strip().strip('"').strip("'")
        if configured:
            return os.path.expanduser(configured) if os.name != "nt" else configured
        if not self._is_local_api():
            return ""
        candidates: list[Path] = []
        home = Path.home()
        if os.name == "nt":
            for drv in ("G:", "D:", "E:", "C:"):
                candidates.extend([Path(f"{drv}\\feiyu-cover"), Path(f"{drv}\\feiyu-cover-api")])
        candidates.extend([
            home / "feiyu-cover-api",
            home / "feiyu-cover",
            Path.cwd().parent / "feiyu-cover-api",
            Path.cwd().parent / "feiyu-cover",
        ])
        for cand in candidates:
            with contextlib.suppress(OSError):
                if (cand / "cover_api" / "app.py").is_file():
                    return str(cand)
        return ""

    def _can_auto_start(self, *, ignore_switch: bool = False) -> bool:
        if self._unloading or not self._is_local_api():
            return False
        if not ignore_switch and not self.config.cover.auto_start_api:
            return False
        if (self.config.cover.api_start_cmd or "").strip():
            return True
        if self._discover_api_dir():
            return True
        # Windows + WSL 场景下，即使宿主未扫到目录，也可能装在 WSL 的 ~/feiyu-cover-api
        return self._is_local_api() and (os.name != "nt" or bool(self._detect_wsl_distro()))

    def _write_started_flag(self, meta: dict[str, Any]) -> None:
        flag = self._started_flag
        if not flag:
            return
        with contextlib.suppress(OSError):
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    def _read_started_flag(self) -> dict[str, Any]:
        flag = self._started_flag
        if not flag or not flag.is_file():
            return {}
        try:
            raw = flag.read_text(encoding="utf-8").strip()
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            pass
        return {"mode": "legacy"}

    def _build_wsl_start_script(self, wsl_dir: str, port: int) -> str:
        """构造通过 stdin 喂给 `wsl.exe -- bash -s` 的启动脚本。

        关键设计：
        1. 通过 stdin 传输脚本，彻底规避 Windows/wsl.exe 剥离命令行引号导致 exit 2 的 Bug；
        2. 前台 `exec uvicorn` 让后台无窗 wsl.exe 保活，彻底解决 WSL2 无交互终端 8 秒自动关机问题。
        """
        lines = ["set -e"]
        if wsl_dir:
            lines.append(f"DIR={shlex.quote(wsl_dir)}")
        else:
            lines.extend([
                'DIR=""',
                'for d in "$HOME/feiyu-cover-api" "$HOME/feiyu-cover" /mnt/g/feiyu-cover /mnt/d/feiyu-cover /mnt/c/feiyu-cover; do',
                '  if [ -f "$d/cover_api/app.py" ]; then DIR="$d"; break; fi',
                'done',
            ])
        lines.extend([
            'if [ -z "$DIR" ] || [ ! -f "$DIR/cover_api/app.py" ]; then',
            '  echo "找不到 cover_api/app.py (DIR=$DIR)" >&2',
            '  exit 2',
            'fi',
            f'if curl -s --max-time 2 "http://127.0.0.1:{port}/health" >/dev/null 2>&1; then',
            '  exit 0',
            'fi',
            'PY=""',
            'for p in "$DIR/runtime/service-venv/bin/python" "$DIR/.venv/bin/python" "$HOME/.local/share/feiyu-voice/service/bin/python" "$(command -v python3)"; do',
            '  if [ -n "$p" ] && [ -x "$p" ]; then PY="$p"; break; fi',
            'done',
            'if [ -z "$PY" ]; then',
            '  echo "未找到可用的 Python 虚拟环境" >&2',
            '  exit 3',
            'fi',
            'cd "$DIR"',
            f'exec "$PY" -m uvicorn cover_api.app:app --host 0.0.0.0 --port {port} >> /tmp/feiyu-cover-api.log 2>&1',
        ])
        return "\n".join(lines) + "\n"

    def _use_wsl(self, directory: str) -> bool:
        runtime = self.config.cover.api_runtime.lower()
        if os.name != "nt" or runtime in ("local", "本机直接运行"):
            return False
        return "wsl" in runtime or not any(
            directory and (Path(directory) / folder / "Scripts/python.exe").is_file()
            for folder in ("runtime/service-venv", ".venv")
        )

    async def _start_api(self) -> tuple[bool, str]:
        """自动拉起翻唱 API 并等待就绪（防重入、零引号问题、WSL2 保活、秒级报错）。"""
        async with self._get_start_lock():
            if self._unloading or not self._is_local_api():
                self._starting = False
                return False, "只在插件启用期间管理本地 API"
            if await self._health_ok(timeout=1.5):
                self._starting = False
                self._last_start_error = ""
                return True, "服务在线"

            self._starting = True
            self._last_start_error = ""
            meta: dict[str, Any] = {"ts": time.time(), "port": self._api_port()}
            watched_proc: subprocess.Popen[bytes] | None = None
            try:
                custom_cmd = (self.config.cover.api_start_cmd or "").strip()
                if custom_cmd:
                    meta["mode"] = "custom"
                    proc = await asyncio.create_subprocess_shell(
                        custom_cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(proc.wait(), timeout=10.0)
                    if proc.returncode not in (None, 0):
                        err_txt = ""
                        if proc.stderr:
                            with contextlib.suppress(Exception):
                                err_txt = (await proc.stderr.read()).decode("utf-8", errors="ignore").strip()[-160:]
                        msg = f"自定义启动命令退出码 {proc.returncode}" + (f"：{err_txt}" if err_txt else "")
                        self._last_start_error = msg
                        self._starting = False
                        return False, msg
                else:
                    api_dir = self._discover_api_dir()
                    port = self._api_port()
                    if self._bg_proc is not None and self._bg_proc.poll() is None:
                        self._starting = False
                        return False, "已有启动进程，服务尚未就绪"
                    if self._use_wsl(api_dir):
                        distro = self._detect_wsl_distro()
                        wsl_dir = self._win_to_wsl_path(api_dir) if api_dir else ""
                        script = self._build_wsl_start_script(wsl_dir, port)
                        argv = ["wsl.exe"]
                        if distro:
                            argv.extend(["-d", distro])
                        argv.extend(["--", "bash", "-s"])
                        popen_kw: dict[str, Any] = {
                            "stdin": subprocess.PIPE,
                            "stdout": subprocess.DEVNULL,
                            "stderr": subprocess.PIPE,
                        }
                        if os.name == "nt":
                            popen_kw["creationflags"] = (
                                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                            )
                        child = subprocess.Popen(argv, **popen_kw)
                        assert child.stdin is not None
                        child.stdin.write(script.encode("utf-8"))
                        child.stdin.close()
                        self._bg_proc = child
                        watched_proc = child
                        meta.update({"mode": "wsl", "distro": distro, "dir": wsl_dir, "pid": child.pid})
                    else:
                        if not api_dir or not (Path(api_dir) / "cover_api" / "app.py").is_file():
                            msg = "未找到 API 文件夹，请在插件设置填写「API 文件夹路径」"
                            self._last_start_error = msg
                            self._starting = False
                            return False, msg
                        base_dir = Path(api_dir)
                        py_cands = [
                            base_dir / "runtime" / "service-venv" / "Scripts" / "python.exe",
                            base_dir / "runtime" / "service-venv" / "bin" / "python",
                            base_dir / ".venv" / "Scripts" / "python.exe",
                            base_dir / ".venv" / "bin" / "python",
                            Path.home() / ".local" / "share" / "feiyu-voice" / "service" / "bin" / "python",
                            Path(sys.executable),
                        ]
                        py_bin = next((str(p) for p in py_cands if p.is_file()), sys.executable)
                        popen_kw = {
                            "cwd": str(base_dir),
                            "stdout": subprocess.DEVNULL,
                            "stderr": subprocess.PIPE,
                            "stdin": subprocess.DEVNULL,
                        }
                        if os.name == "nt":
                            popen_kw["creationflags"] = (
                                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                            )
                        else:
                            popen_kw["start_new_session"] = True
                        child = subprocess.Popen(
                            [py_bin, "-m", "uvicorn", "cover_api.app:app", "--host", "0.0.0.0", "--port", str(port)],
                            **popen_kw,
                        )
                        self._bg_proc = child
                        watched_proc = child
                        meta.update({"mode": "local", "pid": child.pid, "dir": str(base_dir)})
            except Exception as exc:  # noqa: BLE001
                msg = f"拉起本地服务失败：{exc}"
                self._last_start_error = msg
                self._starting = False
                return False, msg

            # 等 API 就绪，最多约 25 秒；若子进程提前异常退出则秒级报错，绝不白等
            for _ in range(18):
                if await self._health_ok(timeout=1.2):
                    self._write_started_flag(meta)
                    self._last_start_error = ""
                    self._starting = False
                    return True, "服务已自动启动"
                if watched_proc is not None and watched_proc.poll() is not None:
                    rc = watched_proc.returncode
                    err_txt = ""
                    if watched_proc.stderr:
                        with contextlib.suppress(Exception):
                            err_txt = watched_proc.stderr.read().decode("utf-8", errors="ignore").strip()[-180:]
                    # 进程以 0 退出（例如端口已在线），再补测一次 health
                    if rc == 0 and await self._health_ok(timeout=1.5):
                        self._write_started_flag(meta)
                        self._last_start_error = ""
                        self._starting = False
                        return True, "服务已就绪"
                    msg = f"启动进程异常退出（code={rc}）" + (f"：{err_txt}" if err_txt else "")
                    self._last_start_error = msg
                    self._starting = False
                    return False, msg
                await asyncio.sleep(1.2)

            msg = "已尝试启动，但服务在 25 秒内未就绪（请检查 API 文件夹路径或 /tmp/feiyu-cover-api.log）"
            self._last_start_error = msg
            self._starting = False
            return False, msg

    def _stop_listener(self, directory: str, port: int) -> int:
        helper = Path(__file__).with_name("api_listener.py")
        if self._use_wsl(directory):
            distro = self._detect_wsl_distro()
            if not distro:
                raise RuntimeError("未识别到 WSL 发行版")
            argv = ["wsl.exe", "-d", distro, "--exec", "python3", "-I", "-",
                    self._win_to_wsl_path(directory), str(port)]
            res = subprocess.run(argv, input=helper.read_bytes(), capture_output=True, timeout=14,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if res.returncode:
                raise RuntimeError(res.stderr.decode("utf-8", errors="replace").strip() or f"退出码 {res.returncode}")
            return int(res.stdout.strip())
        import runpy
        root = Path(directory).expanduser().resolve()
        if not directory or not (root / "cover_api/app.py").is_file():
            raise RuntimeError("未找到绑定的 API 项目目录")
        helpers = runpy.run_path(str(helper))
        return helpers["windows_stop" if os.name == "nt" else "linux_stop"](root, port)

    async def _stop_api(self) -> str:
        cover = self.config.cover
        if not cover.auto_stop_api or not self._is_local_api():
            return "已跳过（自动关闭已禁用或非本地 API）"
        try:
            meta = self._read_started_flag()
            if not meta and not cover.auto_start_api:
                return "未接管外部服务"
            if (cover.api_stop_cmd.strip() and meta.get("mode") == "custom"
                    and meta.get("port") == self._api_port()):
                proc = await asyncio.create_subprocess_shell(cover.api_stop_cmd)
                if await asyncio.wait_for(proc.wait(), timeout=15):
                    raise RuntimeError(f"自定义停止命令退出码 {proc.returncode}")
                message = "自定义停止命令执行成功"
            else:
                pid = await asyncio.to_thread(self._stop_listener, self._discover_api_dir(), self._api_port())
                message = f"已关闭绑定的本地 API (PID={pid})" if pid else "本地 API 已停止"
            if self._started_flag:
                self._started_flag.unlink(missing_ok=True)
            self._bg_proc = None
            return message
        except Exception as exc:
            return f"停止服务失败：{exc or type(exc).__name__}"

    async def _ensure_service(self) -> tuple[bool, str]:
        """确保 API 可用：在线直接返回；否则按配置尝试自动拉起。

        返回 (可用, 提示)。提示在服务不可用时给出可操作建议。
        """
        if await self._health_ok():
            return True, ""
        if not self.config.cover.auto_start_api:
            return False, (
                f"翻唱服务连接不可用（{self.config.cover.api_base}）。"
                "请先启动 feiyu-cover-api；如需自动拉起，"
                "请在插件设置中开启「自动拉起本地服务」并填写「API 文件夹路径」。"
            )
        ok, msg = await self._start_api()
        if ok:
            return True, ""
        return False, (
            f"翻唱服务连接不可用，自动拉起也失败：{msg}。"
            "请检查「API 文件夹路径」是否正确，或手动启动 feiyu-cover-api 服务。"
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

    def _presets_path(self) -> Path:
        """命名音色预设：与当前生效的 voice.json 分开存，互不覆盖。"""
        return Path(self.ctx.paths.data_dir) / "voice_presets.json"

    def _load_presets(self) -> dict[str, dict[str, Any]]:
        """读取全部预设 {名称: 音色配置}，文件不存在/损坏时返回空。"""
        try:
            path = self._presets_path()
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("读取音色预设失败：%s", exc)
        return {}

    def _save_presets(self, presets: dict[str, dict[str, Any]]) -> None:
        path = self._presets_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(presets, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def _parse_payload(payload: str) -> dict[str, Any]:
        """WebUI 传来的 JSON 字符串 → dict，非法则抛 ValueError。"""
        try:
            data = json.loads(payload)
        except (ValueError, RecursionError):
            raise ValueError("参数不是合法 JSON") from None
        if not isinstance(data, dict):
            raise ValueError("参数必须是对象")
        return data

    @staticmethod
    def _voice_body(voice: dict[str, Any], pitch_override: Any = None) -> dict[str, Any]:
        """把内部音色配置收敛成 API 接受的 voice 字典（类型强转 + 可选模型对）。

        pitch_override 用于 /cover 顶层的 pitch 参数覆盖；None 表示用音色里的值。
        """
        pitch = voice.get("pitch", 0) if pitch_override is None else pitch_override
        f0 = voice.get("f0_method", "rmvpe")
        if f0 not in VOICE_F0_METHODS:
            f0 = "rmvpe"
        return {
            "pitch": int(pitch),
            "auto_pitch": bool(voice.get("auto_pitch", True)),
            "index_rate": float(voice.get("index_rate", 0.28)),
            "protect": float(voice.get("protect", 0.45)),
            "volume_envelope": float(voice.get("volume_envelope", 0.25)),
            "f0_method": f0,
            "f0_autotune": bool(voice.get("f0_autotune", False)),
            "f0_autotune_strength": float(voice.get("f0_autotune_strength", 0.5)),
            "formant_shifting": bool(voice.get("formant_shifting", False)),
            "formant_qfrency": float(voice.get("formant_qfrency", 1.0)),
            "vocal_reverb": bool(voice.get("vocal_reverb", True)),
            **({"model": voice["model"]} if voice.get("model") else {}),
            **({"index": voice["index"]} if voice.get("index") else {}),
        }

    @staticmethod
    def _slice_audio(data: bytes, chunk: int, size: int = 240 * 1024) -> tuple[str, int, int]:
        """把音频按 size 切片，返回 (base64片段, 实际块号, 总块数)。

        240KB 原始 ≈ 320KB base64，留足宿主单次 512KB 返回上限的余量。
        """
        total = max(1, (len(data) + size - 1) // size)
        idx = min(max(int(chunk), 0), total - 1)
        piece = data[idx * size:(idx + 1) * size]
        return base64.b64encode(piece).decode("ascii"), idx, total

    def _load_voice(self) -> dict[str, Any]:
        data = dict(VOICE_DEFAULTS)
        try:
            path = self._voice_path()
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data.update({k: v for k, v in raw.items()
                                 if k in VOICE_DEFAULTS or k in ("model", "index", "preset")})
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("读取 voice.json 失败，先用默认值：%s", exc)
        return data

    def _voice_equal(self, a: dict[str, Any], b: dict[str, Any]) -> bool:
        """比较两份音色配置的核心声学参数与模型路径是否一致（用于匹配当前生效预设）。"""
        for k, default in VOICE_DEFAULTS.items():
            va = a.get(k, default)
            vb = b.get(k, default)
            if isinstance(default, bool):
                if bool(va) != bool(vb):
                    return False
            elif isinstance(default, (int, float)):
                try:
                    if abs(float(va) - float(vb)) > 1e-3:
                        return False
                except (TypeError, ValueError):
                    return False
            elif str(va) != str(vb):
                return False
        for k in ("model", "index"):
            if str(a.get(k) or "").strip() != str(b.get(k) or "").strip():
                return False
        return True

    def _match_active_preset(self, voice: dict[str, Any], presets: dict[str, dict[str, Any]]) -> str:
        """返回与当前生效音色完全匹配的预设名；优先保留 voice 中记录的 preset 名称。"""
        hint = str(voice.get("preset") or "").strip()
        if hint and hint in presets and self._voice_equal(voice, presets[hint]):
            return hint
        for name, cfg in sorted(presets.items()):
            if self._voice_equal(voice, cfg):
                return name
        return ""

    def _save_voice(self, incoming: dict[str, Any], persist: bool = True,
                    preset_name: str | None = None) -> dict[str, Any]:
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
            elif isinstance(default, bool):
                value = bool(value)
            clean[key] = value

        for key in ("model", "index"):
            value = incoming.get(key)
            if isinstance(value, str) and value.strip():
                clean[key] = value.strip()

        if not persist:
            return clean          # 试听用：只校验范围，不写入，别影响群里点歌的音色

        # 记录当前命中的预设名，重进页面时下拉框能准确回到上次保存的预设
        if preset_name is not None:
            if preset_name.strip():
                clean["preset"] = preset_name.strip()
        else:
            raw_p = str(incoming.get("preset") or "").strip()
            presets = self._load_presets()
            if raw_p and raw_p in presets and self._voice_equal(clean, presets[raw_p]):
                clean["preset"] = raw_p
            else:
                matched = self._match_active_preset(clean, presets)
                if matched:
                    clean["preset"] = matched

        path = self._voice_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return clean

    def _api_base(self) -> str:
        return (self.config.cover.api_base.strip() or "http://127.0.0.1:19655").rstrip("/")

    @API("jingge_state", description="读取音色参数、服务地址与健康状态")
    async def jingge_state(self) -> dict[str, Any]:
        ok = await self._health_ok()
        if ok:
            self._last_start_error = ""
            health = {"ok": True, "starting": False, "message": "服务在线"}
        elif self._starting:
            # 正在自动拉起：给一个明确的缓冲提示，前端用黄色脉冲点显示
            health = {"ok": False, "starting": True,
                      "message": "服务正在拉起中，请稍候…"}
        else:
            # 页面查询是只读的，不触发自动拉起；给出可操作提示（若有上次失败原因则直接展示）
            if self._last_start_error:
                tip = f"拉起失败：{self._last_start_error}"
            elif self._can_auto_start():
                tip = "服务未启动；点击右侧「检测」可自动拉起，或直接点歌"
            elif self.config.cover.auto_start_api:
                tip = "服务离线；请在插件设置填写「API 文件夹路径」即可自动拉起"
            else:
                tip = "服务离线；请在插件设置开启「自动拉起本地服务」"
            health = {"ok": False, "starting": False, "message": tip}
        return {
            "voice": self._load_voice(),
            "defaults": dict(VOICE_DEFAULTS),
            "model_import_url": f"{self._api_base()}/models/import-page",
            "limits": {k: list(v) for k, v in VOICE_LIMITS.items()},
            "f0_methods": list(VOICE_F0_METHODS),
            "api_base": self.config.cover.api_base,
            "health": health,
        }

    @API("jingge_recheck", description="重新检测并在需要时拉起服务（返回最新状态）")
    async def jingge_recheck(self) -> dict[str, Any]:
        """手动触发：若服务离线且配置了自动拉起，则后台拉起并立即返回（规避 WebUI 10 秒 RPC 超时）。"""
        if await self._health_ok():
            self._last_start_error = ""
            return {"ok": True, "starting": False, "message": "服务在线"}
        if self._starting:
            return {"ok": False, "starting": True, "message": "服务正在拉起中，请稍候…"}
        if self._can_auto_start():
            self._starting = True
            self._last_start_error = ""
            self._spawn(self._start_api())
            return {"ok": False, "starting": True, "message": "正在后台拉起服务，请稍候…"}
        return {
            "ok": False,
            "starting": False,
            "message": "服务离线；请在插件设置开启「自动拉起本地服务」并填「API 文件夹路径」，或手动启动 API",
        }

    @API("jingge_save", description="保存音色参数，下一次点歌立即生效")
    async def jingge_save(self, payload: str) -> dict[str, Any]:
        data = self._parse_payload(payload)
        saved = self._save_voice(data)
        self.ctx.logger.info("音色参数已更新：%s", json.dumps(saved, ensure_ascii=False))
        return {"ok": True, "voice": saved}

    @API("jingge_preset_list", description="列出所有已保存的命名音色预设")
    async def jingge_preset_list(self) -> dict[str, Any]:
        presets = self._load_presets()
        current = self._load_voice()
        active = self._match_active_preset(current, presets)
        # 只回名称和对应模型标签，列表不必带全部参数（前端加载时再取详情）
        items = [
            {"name": name, "model": cfg.get("model", "")}
            for name, cfg in sorted(presets.items())
        ]
        return {"ok": True, "presets": items, "active": active}

    @API("jingge_preset_save", description="把当前参数存成一个命名音色预设")
    async def jingge_preset_save(self, name: str, payload: str) -> dict[str, Any]:
        name = (name or "").strip()
        if not name:
            raise ValueError("请填写预设名称")
        if len(name) > 40:
            raise ValueError("预设名称太长（最多 40 字）")
        data = self._parse_payload(payload)
        # 用同一套校验/范围收敛，预设里也带上 model/index
        clean = self._save_voice(data, persist=False)
        presets = self._load_presets()
        presets[name] = clean
        self._save_presets(presets)
        # 若当前生效参数与刚存的预设一致，顺手把 preset 名称同步写入 voice.json
        if self._voice_equal(self._load_voice(), clean):
            self._save_voice(clean, persist=True, preset_name=name)
        self.ctx.logger.info("音色预设已保存：%s", name)
        return {"ok": True, "name": name, "voice": clean}

    @API("jingge_preset_load", description="把某个命名预设加载到当前语音路由")
    async def jingge_preset_load(self, name: str) -> dict[str, Any]:
        name = (name or "").strip()
        presets = self._load_presets()
        if name not in presets:
            raise ValueError(f"找不到预设「{name}」")
        # 走 _save_voice 落盘到 voice.json —— 群里点歌立即生效，同时记住预设名
        saved = self._save_voice(presets[name], preset_name=name)
        self.ctx.logger.info("音色预设已加载：%s", name)
        return {"ok": True, "name": name, "voice": saved}

    @API("jingge_preset_delete", description="删除一个命名音色预设")
    async def jingge_preset_delete(self, name: str) -> dict[str, Any]:
        name = (name or "").strip()
        presets = self._load_presets()
        if name not in presets:
            raise ValueError(f"找不到预设「{name}」")
        presets.pop(name, None)
        self._save_presets(presets)
        self.ctx.logger.info("音色预设已删除：%s", name)
        return {"ok": True, "name": name}

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

    @API("jingge_cache_stats", description="读取翻唱 API 的缓存占用与清理配置")
    async def jingge_cache_stats(self) -> dict[str, Any]:
        try:
            resp = await self._client().get("/cache/stats", timeout=10.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    @API("jingge_cache_clean", description="立即清理翻唱 API 的缓存与旧任务")
    async def jingge_cache_clean(self, force: bool = False) -> dict[str, Any]:
        try:
            if force:
                stats = await self.jingge_cache_stats()
                if stats.get("force_preserves_jobs") is not True:
                    return {"ok": False, "error": "当前 API 未确认清缓存会保留作品。请更新并重启翻唱 API 后重试。"}
            resp = await self._client().post("/cache/clean", json={"force": bool(force)}, timeout=60.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    @API("jingge_cache_config", description="保存缓存清理阈值到 API 配置")
    async def jingge_cache_config(self, enabled: bool = True, max_age_days: int = 7,
                                  max_size_mb: int = 2048, keep_jobs: int = 30,
                                  check_interval_hours: int = 24) -> dict[str, Any]:
        payload = {
            "enabled": bool(enabled),
            "max_age_days": int(max_age_days),
            "max_size_mb": int(max_size_mb),
            "keep_jobs": int(keep_jobs),
            "check_interval_hours": int(check_interval_hours),
        }
        try:
            resp = await self._client().post("/cache/config", json=payload, timeout=15.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    @staticmethod
    def _unwrap_streams(raw: Any) -> list[dict[str, Any]]:
        """兼容宿主 chat.get_all_streams 返回的列表或嵌套包装字典。"""
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
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    @staticmethod
    def _stream_is_group(item: dict[str, Any]) -> bool:
        """优先使用宿主显式声明的 is_group_session / chat_type 判断群聊或私聊。"""
        if "is_group_session" in item and item.get("is_group_session") is not None:
            return bool(item.get("is_group_session"))
        chat_type = str(item.get("chat_type") or "").strip().lower()
        if chat_type in ("group", "private"):
            return chat_type == "group"
        return bool(str(item.get("group_id") or "").strip())

    @API("jingge_sessions", description="列出可推送成品的全平台群聊与私聊会话")
    async def jingge_sessions(self, platform: str = "all_platforms") -> dict[str, Any]:
        req_plat = (platform or "").strip()
        if not req_plat or req_plat.lower() in ("all", "*", "all_platforms"):
            req_plat = "all_platforms"
        try:
            raw = await self.ctx.chat.get_all_streams(platform=req_plat)
            items = self._unwrap_streams(raw)
            # 兼容极少数旧版宿主不识别 all_platforms 的情况：空结果时补查默认 qq
            if not items and req_plat == "all_platforms":
                items = self._unwrap_streams(await self.ctx.chat.get_all_streams(platform="qq"))
        except Exception as exc:  # noqa: BLE001
            return {"sessions": [], "error": f"{type(exc).__name__}: {exc}"}

        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in items:
            sid = str(item.get("session_id") or item.get("stream_id") or "").strip()
            if not sid or sid in seen:
                continue
            seen.add(sid)
            is_grp = self._stream_is_group(item)
            name = (item.get("group_name") or item.get("session_name")
                    or item.get("name") or item.get("nickname"))
            if not name:
                who = (item.get("user_cardname") or item.get("user_nickname")
                       or item.get("user_id") or "用户")
                name = str(who) if is_grp else f"{who}的私聊"
            out.append({
                "stream_id": sid,
                "name": str(name),
                "group_id": str(item.get("group_id") or ""),
                "user_id": str(item.get("user_id") or ""),
                "platform": str(item.get("platform") or ""),
                "is_group": is_grp,
            })
        return {"sessions": out}

    @API("jingge_render", description="按当前音色参数生成一首翻唱")
    async def jingge_render(self, payload: str) -> dict[str, Any]:
        data = self._parse_payload(payload)
        song = str(data.get("song") or "").strip()
        if not song:
            raise ValueError("请先填写歌名")

        voice = self._save_voice(data.get("voice") if isinstance(data.get("voice"), dict) else {})
        body = {"song": song, "voice": self._voice_body(voice), "requester": "studio"}
        resp = await self._client().post("/cover", json=body)
        resp.raise_for_status()
        job = resp.json()
        return {
            "job_id": str(job.get("id") or ""),
            "queue_position": job.get("queue_position", 1),
            "eta_seconds": job.get("eta_seconds", 240),
        }

    def _drop_source_upload(self, token: str) -> None:
        entry = self._source_uploads.pop(token, None)
        if entry is None:
            return
        entry['expiry'].cancel()
        if entry.get('task'):
            entry['task'].cancel()
        entry['chunks'].clear()

    def _expire_source_later(self, token: str, entry: dict[str, Any]) -> None:
        if entry.get('expiry'):
            entry['expiry'].cancel()
        entry['expiry'] = asyncio.get_running_loop().call_later(600, self._drop_source_upload, token)

    async def _forward_source(self, token: str, entry: dict[str, Any]) -> None:
        async def blocks():
            for block in entry['chunks']:
                yield block

        try:
            resp = await self._client().post('/sources/uploads', content=blocks(), headers={
                'Content-Type': 'application/octet-stream', 'Content-Length': str(entry['size']),
                'X-Audio-Extension': entry['extension'],
            }, timeout=120.0, follow_redirects=False)
            if not resp.is_success:
                raise ValueError(f'上传音源失败（HTTP {resp.status_code}）')
            upload_id = resp.json().get('upload_id')
            if not isinstance(upload_id, str) or not re.fullmatch(r'[0-9a-f]{32}', upload_id):
                raise ValueError('翻唱 API 返回的音源标识无效')
            entry['result'] = {'ok': True, 'status': 'done', 'upload_id': upload_id}
        except ValueError as exc:
            entry['result'] = {'ok': False, 'status': 'failed', 'error': str(exc)}
        except Exception:
            entry['result'] = {'ok': False, 'status': 'failed', 'error': '音源传输失败，请检查翻唱 API 连接并重新上传'}
        finally:
            entry['chunks'].clear()
            if token in self._source_uploads:
                self._expire_source_later(token, entry)

    @API("jingge_source_upload", description="串行分片上传试听音源并查询转发进度")
    async def jingge_source_upload(self, op: str, token: str = '', name: str = '', size: int = 0,
                                   index: int = -1, data: str = '') -> dict[str, Any]:
        if op == 'start':
            ext = Path(name).suffix.lower()[1:]
            if ext not in {'mp3', 'wav', 'flac', 'ogg', 'opus', 'm4a', 'aac'}:
                return {'ok': False, 'error': '音源文件后缀不受支持'}
            if type(size) is not int or not 0 < size <= 32 * 1024 * 1024:
                return {'ok': False, 'error': '音源须大于 0 字节且不超过 32 MiB'}
            entries = self._source_uploads.values()
            if len(entries) >= 256 or sum(e['result']['status'] in ('receiving', 'uploading') for e in entries) >= 2:
                return {'ok': False, 'error': '上传会话已满，请稍后再试'}
            token = uuid.uuid4().hex
            entry = {'size': size, 'extension': ext, 'chunks': [], 'received': 0,
                     'chunk_size': 24 * 1024,
                     'result': {'ok': True, 'status': 'receiving', 'token': token, 'chunk_size': 24 * 1024}}
            self._source_uploads[token] = entry
            self._expire_source_later(token, entry)
            return entry['result']
        entry = self._source_uploads.get(token)
        if entry is None:
            return {'ok': False, 'error': '上传会话已过期，请重新选择音源'}
        if op == 'cancel':
            self._drop_source_upload(token)
            return {'ok': True, 'status': 'cancelled'}
        if op == 'status' or (op == 'finish' and entry['result']['status'] != 'receiving'):
            return entry['result']
        if op == 'chunk' and entry['result']['status'] == 'receiving':
            if index == 0 and len(data) > 32768:
                entry['chunk_size'] = 240 * 1024
            expected = min(entry['chunk_size'], entry['size'] - entry['received'])
            try:
                if (type(index) is not int or index != len(entry['chunks']) or not expected
                        or len(data) != 4 * ((expected + 2) // 3)):
                    raise ValueError
                block = base64.b64decode(data, validate=True)
                if len(block) != expected:
                    raise ValueError
            except (ValueError, TypeError):
                self._drop_source_upload(token)
                return {'ok': False, 'error': '音源分片顺序、编码或长度无效，请重新上传'}
            entry['chunks'].append(block)
            entry['received'] += expected
            self._expire_source_later(token, entry)
            return entry['result']
        if op == 'finish':
            if entry['received'] != entry['size']:
                self._drop_source_upload(token)
                return {'ok': False, 'error': '音源尚未完整接收，请重新上传'}
            entry['result'] = {'ok': True, 'status': 'uploading'}
            entry['task'] = asyncio.create_task(self._forward_source(token, entry))
            await asyncio.wait({entry['task']}, timeout=0.25)
            return entry['result']
        return {'ok': False, 'error': '不支持的上传操作或会话状态'}

    @API("jingge_preview", description="试听工作台：用临时参数生成，不影响已保存的语音路由")
    async def jingge_preview(self, payload: str) -> dict[str, Any]:
        try:
            data = self._parse_payload(payload)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        song = str(data.get("song") or "").strip()
        selected_track = data.get("selected_track") if isinstance(data.get("selected_track"), dict) else None
        bvid = str(data.get("bvid") or "").strip()
        upload_id = data.get('upload_id')
        if upload_id and (not isinstance(upload_id, str) or not re.fullmatch(r'[0-9a-f]{32}', upload_id)):
            return {'ok': False, 'error': '上传音源标识无效，请重新上传'}
        if not upload_id and not song and not selected_track and not bvid:
            return {"ok": False, "error": "请先填写歌名或选择一个搜索结果"}

        if not await self._health_ok():
            if self._can_auto_start():
                if not self._starting:
                    self._starting = True
                    self._last_start_error = ""
                    self._spawn(self._start_api())
                return {"ok": False, "error": "翻唱服务正在后台拉起中，请等顶部变绿后再点生成"}
            return {"ok": False, "error": f"翻唱服务离线（{self.config.cover.api_base}），请先启动 API"}

        draft = data.get("voice") if isinstance(data.get("voice"), dict) else {}
        merged = {**self._load_voice(), **draft}
        voice = self._save_voice(merged, persist=False)

        preview_seconds = min(max(int(data.get("preview_seconds") or 0), 0), 180)
        source_hint = str(data.get("source_hint") or "auto").strip() or "auto"

        body: dict[str, Any] = {
            "song": song or (selected_track.get("title") if selected_track else bvid),
            "voice": self._voice_body(voice),
            "requester": "playground",
            "preview_seconds": preview_seconds,
            "source_hint": source_hint,
        }
        if upload_id:
            body.pop('song')
            body.pop('source_hint')
            body['upload_id'] = upload_id
        else:
            if selected_track:
                body['selected_track'] = selected_track
            if bvid:
                body['bvid'] = bvid

        try:
            resp = await self._client().post("/cover", json=body, timeout=8.5)
            resp.raise_for_status()
            job = resp.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"提交任务失败：{exc}"}
        return {
            "ok": True,
            "job_id": str(job.get("id") or ""),
            "queue_position": job.get("queue_position", 1),
            "eta_seconds": job.get("eta_seconds", 240),
            "voice": voice,
        }

    @API("jingge_search", description="多平台音源搜索预览，供试听工坊选歌")
    async def jingge_search(self, keyword: str, limit: int = 8, source_hint: str = "auto") -> dict[str, Any]:
        kw = (keyword or "").strip()
        if not kw:
            return {"ok": False, "tracks": [], "error": "请输入歌名"}
        try:
            resp = await self._client().get(
                "/sources/search",
                params={"keyword": kw, "limit": max(1, min(int(limit), 15)), "source_hint": source_hint or "auto"},
                timeout=20.0,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "tracks": [], "error": f"{type(exc).__name__}: {exc}"}

    @API("jingge_jobs", description="读取近期翻唱任务列表（含群聊与试听工坊）")
    async def jingge_jobs(self, limit: int = 10) -> dict[str, Any]:
        try:
            resp = await self._client().get("/jobs", params={"limit": max(1, min(int(limit), 30))}, timeout=6.0)
            resp.raise_for_status()
            return {"ok": True, "jobs": resp.json().get("jobs") or []}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "jobs": [], "error": f"{type(exc).__name__}: {exc}"}

    @API("jingge_system", description="读取显卡显存、RVC 常驻状态与队列概览")
    async def jingge_system(self, refresh: bool = False) -> dict[str, Any]:
        try:
            resp = await self._client().get("/system/status", params={"refresh": bool(refresh)}, timeout=6.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    @API("jingge_unload", description="手动释放 RVC 常驻显存")
    async def jingge_unload(self) -> dict[str, Any]:
        try:
            resp = await self._client().post("/system/unload", timeout=8.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    @API("jingge_cancel", description="取消排队中或渲染中的翻唱任务")
    async def jingge_cancel(self, job_id: str) -> dict[str, Any]:
        jid = (job_id or "").strip()
        if not jid:
            return {"ok": False, "message": "缺少任务号"}
        try:
            resp = await self._client().post(f"/jobs/{jid}/cancel", timeout=8.0)
            if resp.status_code == 404:
                return {"ok": False, "message": "任务不存在"}
            resp.raise_for_status()
            self._pending.pop(jid, None)
            self._save_state()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"取消失败：{exc}"}

    @API("jingge_voice_info", description="查询成品的语音分段数与总时长")
    async def jingge_voice_info(self, job_id: str, seconds: int = 60) -> dict[str, Any]:
        jid = (job_id or "").strip()
        if not jid:
            return {"ok": False, "parts": 1, "error": "缺少任务号"}
        try:
            resp = await self._client().get(
                f"/jobs/{jid}/voice-info",
                params={"seconds": max(15, min(int(seconds), 120))},
                timeout=8.0,
            )
            resp.raise_for_status()
            return {"ok": True, **resp.json()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "parts": 1, "error": f"{type(exc).__name__}: {exc}"}

    async def _audio_rpc_chunk(self, url: str, chunk: int, timeout: float) -> tuple[str, int, int, int, str]:
        async with self._audio_lock:
            if self._unloading:
                raise RuntimeError("插件正在卸载")
            client = self._client()
            key = (client, url)
            entries = self._audio_cache
            now = time.monotonic()
            for old_key, (old_stream, _, _, created) in list(entries.items()):
                if now - created > 300:
                    old_stream.close()
                    entries.pop(old_key, None)
            if key not in entries:
                # Serialize fills so the bound includes partial downloads: 4 x 64 MiB.
                if len(entries) >= 4:
                    entries.pop(next(iter(entries)))[0].close()
                task = asyncio.create_task(self._spool_audio(client, url, timeout))
                self._audio_fill = task
                try:
                    stream, size, mime = await task
                    if (size + 240 * 1024 - 1) // (240 * 1024) > 100:
                        stream.close()
                        raise ValueError("音频超过前端 100 片上限")
                    entries[key] = (stream, size, mime, time.monotonic())
                finally:
                    self._audio_fill = None
            stream, size, mime, created = entries.pop(key)
            entries[key] = (stream, size, mime, time.monotonic())
            n = 240 * 1024
            count = max(1, (size + n - 1) // n)
            idx = min(max(0, int(chunk)), count - 1)
            stream.seek(idx * n)
            return base64.b64encode(stream.read(n)).decode("ascii"), idx, count, size, mime

    @staticmethod
    async def _spool_audio(client, url, timeout):
        import tempfile
        limit = 64 * 1024 * 1024
        async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}, timeout=timeout) as resp:
            if resp.status_code != 200:
                raise ValueError(f"音频尚未准备好（HTTP {resp.status_code}）")
            if resp.headers.get("content-encoding", "identity").lower() != "identity":
                raise ValueError("音频响应编码不受支持")
            length = resp.headers.get("content-length")
            expected = int(length) if length is not None else None
            if expected is not None and not 0 <= expected <= limit:
                raise ValueError("音频大小超过限制")
            stream = tempfile.TemporaryFile()
            try:
                size = 0
                async for block in resp.aiter_raw(64 * 1024):
                    size += len(block)
                    if size > limit:
                        raise ValueError("音频大小超过限制")
                    stream.write(block)
                if expected is not None and size != expected:
                    raise ValueError("音频长度不一致")
                return stream, size, resp.headers.get("content-type") or "audio/mpeg"
            except BaseException:
                stream.close()
                raise

    @API("jingge_audio", description="分片读取 60 秒试听音频")
    async def jingge_audio(self, job_id: str, part: int = 0, chunk: int = 0) -> dict[str, Any]:
        if not job_id.strip():
            raise ValueError("缺少任务号")
        part = int(part)
        encoded, idx, total, size, content_type = await self._audio_rpc_chunk(
            f"/jobs/{job_id}/voice-part/{part}", chunk, 120.0)
        return {"audio": encoded, "chunk": idx, "chunks": total,
                "content_type": content_type, "size_kb": round(size / 1024, 1), "part": part}

    @API("jingge_file", description="分片读取完整成品音频")
    async def jingge_file(self, job_id: str, chunk: int = 0) -> dict[str, Any]:
        if not job_id.strip():
            raise ValueError("缺少任务号")
        encoded, idx, total, size, content_type = await self._audio_rpc_chunk(
            f"/jobs/{job_id}/audio", chunk, 180.0)
        return {"audio": encoded, "chunk": idx, "chunks": total,
                "total_kb": round(size / 1024, 1), "content_type": content_type}

    @API("jingge_save_to", description="保存完整成品；path=:native-dialog: 打开宿主桌面另存为，:native-status:<token> 查询结果")
    async def jingge_save_to(self, job_id: str, path: str) -> dict[str, Any]:
        """保存完整音频；桌面另存为由后台工作线程执行，不占用 RPC。"""
        if (path or "").strip() == ":native-dialog:":
            return self._native_save_start(job_id)
        raw_status = (path or "").strip()
        if raw_status.startswith(":native-status:"):
            return self._native_save_status(raw_status[len(":native-status:"):])
        if not job_id.strip():
            raise ValueError("缺少任务号")
        raw = (path or "").strip().strip('"')
        if not raw:
            raise ValueError("请填写保存路径")

        target = Path(raw).expanduser()
        # 拒绝路径穿越（..），避免写到预期之外的地方
        if ".." in target.parts:
            raise ValueError("路径不能包含 ..")
        # 目录 or 以 .mp3 结尾当作文件名（自动用歌曲原名命名，而非写死「麦弦翻唱.mp3」）
        if target.suffix.lower() != ".mp3":
            title_stem = "麦弦翻唱"
            with contextlib.suppress(Exception):
                meta_resp = await self._client().get(f"/jobs/{job_id}", timeout=8.0)
                if meta_resp.status_code == 200:
                    jdata = meta_resp.json()
                    raw_title = (jdata.get("meta") or {}).get("title") or jdata.get("song") or ""
                    safe = "".join(ch for ch in raw_title if ch not in '\\/:*?"<>|').strip()[:40]
                    if safe:
                        title_stem = f"{safe}-麦弦翻唱"
            target = target / f"{title_stem}.mp3"

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

        temp = target.with_name(target.name + f".{uuid.uuid4().hex}.tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with temp.open("xb") as stream:
                stream.write(resp.content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, target)
        except OSError as exc:
            raise ValueError(f"写入失败：{exc}") from None
        finally:
            with contextlib.suppress(OSError):
                temp.unlink()

        return {
            "path": str(target),
            "size_kb": round(len(resp.content) / 1024, 1),
        }

    def _native_save_start(self, job_id: str) -> dict[str, Any]:
        if not job_id.strip():
            return {"pending": False, "ok": False, "error": "缺少任务号"}
        if os.name != "nt":
            return {"pending": False, "ok": False, "error": "宿主不是 Windows，无法打开桌面另存为对话框"}
        desktop_error = self._native_desktop_error()
        if desktop_error:
            return {"pending": False, "ok": False, "error": desktop_error}
        with self._native_save_lock:
            now = time.time()
            self._native_save_states = {k: v for k, v in self._native_save_states.items() if v.get("pending") or now - v["created"] < 900}
            if self._native_save_active:
                if self._native_save_active["job_id"] == job_id:
                    return {"pending": True, "token": self._native_save_active["token"]}
                return {"pending": False, "ok": False, "error": "另一个保存对话框仍在等待操作，请稍后重试"}
            token = uuid.uuid4().hex
            state = {"token": token, "job_id": job_id, "created": now, "pending": True}
            self._native_save_active = state
            self._native_save_states[token] = state
        threading.Thread(target=self._native_save_worker, args=(token, job_id), daemon=True,
                         name="feiyu-native-save").start()
        return {"pending": True, "token": token}

    @staticmethod
    def _native_desktop_error() -> str:
        """仅检测当前 Windows 进程是否附着在交互桌面，不依赖 RPC 标准输入。"""
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            user32.OpenInputDesktop.restype = wintypes.HANDLE
            user32.CloseDesktop.argtypes = [wintypes.HANDLE]
            desktop = user32.OpenInputDesktop(0, False, 0x0100)
            if not desktop:
                return "宿主进程无法访问交互桌面，无法显示另存为对话框"
            user32.CloseDesktop(desktop)
            return ""
        except Exception as exc:  # noqa: BLE001
            return f"检测 Windows 交互桌面失败：{type(exc).__name__}: {exc}"

    @staticmethod
    def _native_save_dialog(suggested_name: str = "麦弦翻唱") -> str:
        """使用 comdlg32 原生 Unicode 文件对话框，不依赖 Tcl/Tk 或 PowerShell。"""
        import ctypes
        from ctypes import wintypes
        ole32 = ctypes.WinDLL("ole32", use_last_error=True)
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        ole32.CoInitializeEx.restype = ctypes.c_long
        co_result = int(ole32.CoInitializeEx(None, 0x2))
        if co_result < 0:
            raise OSError(co_result, "无法初始化文件对话框线程 COM STA")
        try:
            return FeiyuCoverPlugin._native_save_dialog_sta(suggested_name)
        finally:
            ole32.CoUninitialize()

    @staticmethod
    def _native_save_dialog_sta(suggested_name: str) -> str:
        import ctypes
        from ctypes import wintypes

        class OPENFILENAMEW(ctypes.Structure):
            _fields_ = [
                ("lStructSize", wintypes.DWORD), ("hwndOwner", wintypes.HWND),
                ("hInstance", wintypes.HINSTANCE), ("lpstrFilter", wintypes.LPCWSTR),
                ("lpstrCustomFilter", wintypes.LPWSTR), ("nMaxCustFilter", wintypes.DWORD),
                ("nFilterIndex", wintypes.DWORD), ("lpstrFile", wintypes.LPWSTR),
                ("nMaxFile", wintypes.DWORD), ("lpstrFileTitle", wintypes.LPWSTR),
                ("nMaxFileTitle", wintypes.DWORD), ("lpstrInitialDir", wintypes.LPCWSTR),
                ("lpstrTitle", wintypes.LPCWSTR), ("Flags", wintypes.DWORD),
                ("nFileOffset", wintypes.WORD), ("nFileExtension", wintypes.WORD),
                ("lpstrDefExt", wintypes.LPCWSTR), ("lCustData", wintypes.LPARAM),
                ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", wintypes.LPCWSTR),
                ("pvReserved", ctypes.c_void_p), ("dwReserved", wintypes.DWORD),
                ("FlagsEx", wintypes.DWORD),
            ]

        buffer = ctypes.create_unicode_buffer(32768)
        safe_name = "".join(ch for ch in suggested_name if ch not in '\\/:*?"<>|').strip()[:100] or "麦弦翻唱"
        buffer.value = safe_name if safe_name.lower().endswith(".mp3") else safe_name + ".mp3"
        dialog = OPENFILENAMEW()
        dialog.lStructSize = ctypes.sizeof(dialog)
        filter_buffer = ctypes.create_unicode_buffer("MP3 音频 (*.mp3)\0*.mp3\0所有文件 (*.*)\0*.*\0\0")
        dialog.lpstrFilter = ctypes.cast(filter_buffer, wintypes.LPCWSTR)
        dialog.nFilterIndex = 1
        dialog.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
        dialog.nMaxFile = len(buffer)
        dialog.lpstrTitle = "保存麦弦翻唱"
        dialog.lpstrDefExt = "mp3"
        dialog.Flags = 0x00000002 | 0x00000004 | 0x00000800 | 0x00080000
        comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)
        comdlg32.GetSaveFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
        comdlg32.GetSaveFileNameW.restype = wintypes.BOOL
        comdlg32.CommDlgExtendedError.restype = wintypes.DWORD
        if comdlg32.GetSaveFileNameW(ctypes.byref(dialog)):
            return buffer.value
        error = int(comdlg32.CommDlgExtendedError())
        if error == 0:
            return ""
        raise OSError(error, f"GetSaveFileNameW 对话框错误 0x{error:04X}")

    def _native_save_status(self, token: str) -> dict[str, Any]:
        with self._native_save_lock:
            state = self._native_save_states.get(token)
            if not state or (not state.get("pending") and time.time() - state["created"] >= 900):
                self._native_save_states.pop(token, None)
                return {"pending": False, "ok": False, "error": "保存状态已过期或 token 无效"}
            return {k: v for k, v in state.items() if k not in ("created", "job_id")}

    def _native_save_worker(self, token: str, job_id: str) -> None:
        result: dict[str, Any]
        try:
            suggested_name = asyncio.run(self._native_suggested_name(job_id))
            selected = self._native_save_dialog(suggested_name)
            if not selected:
                result = {"pending": False, "ok": False, "cancelled": True}
            else:
                audio = asyncio.run(self._native_download_audio(job_id))
                target = Path(selected)
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(target.name + "." + token + ".tmp")
                try:
                    with temp.open("xb") as stream:
                        stream.write(audio)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temp, target)
                finally:
                    with contextlib.suppress(OSError):
                        temp.unlink()
                result = {"pending": False, "ok": True, "path": str(target), "size_kb": round(len(audio) / 1024, 1)}
        except Exception as exc:  # noqa: BLE001
            result = {"pending": False, "ok": False, "error": f"另存为失败：{type(exc).__name__}: {exc}"}
        with self._native_save_lock:
            state = self._native_save_states.get(token)
            if state:
                state.update(result, created=time.time())
            if self._native_save_active and self._native_save_active.get("token") == token:
                self._native_save_active = None

    async def _native_suggested_name(self, job_id: str) -> str:
        fallback = "麦弦翻唱"
        try:
            if httpx is None:
                return fallback
            base = (self.config.cover.api_base or "").rstrip("/")
            key = (self.config.cover.api_key or "").strip()
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            async with httpx.AsyncClient(base_url=base, headers=headers, timeout=8.0) as client:
                resp = await client.get(f"/jobs/{job_id}")
            if resp.status_code == 200:
                data = resp.json()
                title = (data.get("meta") or {}).get("title") or data.get("song") or ""
                safe = "".join(ch for ch in str(title) if ch not in '\\/:*?\"<>|').strip()[:100]
                if safe:
                    return f"{safe}-麦弦翻唱"
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug("读取另存为默认文件名失败：%s", exc)
        return fallback

    async def _native_download_audio(self, job_id: str) -> bytes:
        if httpx is None:
            raise RuntimeError("缺少 httpx 依赖")
        base = (self.config.cover.api_base or "").rstrip("/")
        key = (self.config.cover.api_key or "").strip()
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        async with httpx.AsyncClient(base_url=base, headers=headers, timeout=180.0) as client:
            resp = await client.get(f"/jobs/{job_id}/audio")
            if resp.status_code != 200:
                raise RuntimeError(f"完整成品不可用（HTTP {resp.status_code}）")
            return resp.content

    @API("jingge_job", description="查询翻唱任务状态")
    async def jingge_job(self, job_id: str) -> dict[str, Any]:
        return await self._get_job(job_id)

    @API("jingge_send", description="把成品发送到指定会话（支持按次指定发歌模式）")
    async def jingge_send(self, job_id: str, stream_id: str,
                          group_id: str = "", user_id: str = "",
                          platform: str = "", deliver_mode: str = "") -> dict[str, Any]:
        try:
            resp = await self._client().get(f"/jobs/{job_id}", timeout=6.0)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"读取任务信息失败：{exc}"}
        # 语音分段发送可能需数秒，后台执行避免触碰宿主 WebUI 10 秒 RPC 超时上限
        self._spawn(self._deliver(job_id, data, {
            "stream_id": stream_id,
            "group_id": group_id,
            "user_id": user_id,
            "platform": (platform or "").strip(),
            "song": data.get("song", ""),
            "deliver_mode": (deliver_mode or "").strip(),
        }))
        return {"ok": True, "message": "正在发送到目标会话"}

    @API("jingge_studio_status", description="查询干声工坊（人声分离与训练）状态与素材库")
    async def jingge_studio_status(self) -> dict[str, Any]:
        base = (self.config.cover.api_base or "").rstrip("/")
        studio_url = f"{base}/studio" if base else ""
        try:
            resp = await self._client().get("/studio/status", timeout=6.0)
            if resp.status_code == 404:
                return {"ok": False, "studio_url": studio_url,
                        "error": "当前翻唱 API 版本较旧（未包含干声工坊接口），请更新并重启 API"}
            resp.raise_for_status()
            data = resp.json()
            return {"ok": True, "studio_url": studio_url, **data}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "studio_url": studio_url, "error": f"获取工坊状态失败：{exc}"}

    @API("jingge_studio_separate", description="启动干声人声分离任务（支持歌名/BV号/本地路径）")
    async def jingge_studio_separate(self, dataset: str = "my-voice", source: str = "") -> dict[str, Any]:
        try:
            resp = await self._client().post(
                "/studio/separate",
                json={"dataset": (dataset or "my-voice").strip(), "source": (source or "").strip()},
                timeout=7.0,
            )
            if resp.status_code >= 400:
                detail = ""
                with contextlib.suppress(Exception):
                    detail = str(resp.json().get("detail") or "")
                return {"ok": False, "error": detail or f"HTTP {resp.status_code}"}
            return {"ok": True, "task": resp.json()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"提交干声分离失败：{exc}"}

    @API("jingge_studio_train", description="启动一键 RVC 音色训练任务")
    async def jingge_studio_train(
        self,
        model_name: str,
        dataset: str,
        profile: str = "refinegan",
        epochs: int = 150,
        batch_size: int = 4,
    ) -> dict[str, Any]:
        try:
            resp = await self._client().post(
                "/studio/train",
                json={
                    "model_name": (model_name or "").strip(),
                    "dataset": (dataset or "").strip(),
                    "profile": (profile or "refinegan").strip(),
                    "epochs": max(20, min(int(epochs), 800)),
                    "batch_size": int(batch_size),
                },
                timeout=7.0,
            )
            if resp.status_code >= 400:
                detail = ""
                with contextlib.suppress(Exception):
                    detail = str(resp.json().get("detail") or "")
                return {"ok": False, "error": detail or f"HTTP {resp.status_code}"}
            return {"ok": True, "task": resp.json()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"启动训练失败：{exc}"}

    @API("jingge_model_uploads", description="读取音色删除清单及保护原因")
    async def jingge_model_uploads(self) -> dict[str, Any]:
        try:
            resp = await self._client().get("/models/uploads", timeout=6.0)
            if resp.status_code == 404:
                return {"error": "当前翻唱 API 未提供模型管理接口"}
            if resp.is_error:
                return {"error": resp.json().get("detail") or f"HTTP {resp.status_code}"}
            return resp.json()
        except Exception as exc:
            return {"error": f"读取模型管理清单失败：{exc}"}

    @API("jingge_model_delete", description="按已确认的文件清单删除音色")
    async def jingge_model_delete(self, model_id: str, confirm: str) -> dict[str, Any]:
        if not all(len(s) == 64 and all(c in "0123456789abcdef" for c in s) for s in (model_id, confirm)):
            return {"ok": False, "error": "模型标识或确认令牌无效"}
        try:
            resp = await self._client().delete(f"/models/uploads/{model_id}", params={"confirm": confirm}, timeout=7.0)
            if resp.is_error:
                return {"ok": False, "error": resp.json().get("detail") or f"HTTP {resp.status_code}"}
            return resp.json()
        except Exception:
            return {"ok": False, "error": "未能确认删除结果，请刷新模型列表后核对"}

    @API("jingge_model_import", description="导入外部 RVC WebUI / Applio 推理模型（.pth / .index / .zip / 目录）")
    async def jingge_model_import(
        self,
        model_name: str = "",
        pth_path: str = "",
        index_path: str = "",
    ) -> dict[str, Any]:
        ok, hint = await self._ensure_service()
        if not ok:
            return {"ok": False, "error": hint}
        try:
            resp = await self._client().post(
                "/models/import",
                json={
                    "model_name": (model_name or "").strip(),
                    "pth_path": (pth_path or "").strip(),
                    "index_path": (index_path or "").strip(),
                },
                timeout=60.0,
            )
            if resp.status_code >= 400:
                detail = ""
                with contextlib.suppress(Exception):
                    detail = str(resp.json().get("detail") or "")
                return {"ok": False, "error": detail or f"HTTP {resp.status_code}"}
            data = resp.json()
            data["import_page_url"] = f"{self._api_base()}/models/import-page"
            return data
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"导入模型失败：{exc}"}

    @API("jingge_studio_inspect", description="对干声素材集执行红绿灯声纹体检")
    async def jingge_studio_inspect(self, dataset: str) -> dict[str, Any]:
        try:
            resp = await self._client().post(
                "/studio/inspect",
                json={"dataset": (dataset or "").strip()},
                timeout=8.5,
            )
            if resp.status_code >= 400:
                detail = ""
                with contextlib.suppress(Exception):
                    detail = str(resp.json().get("detail") or "")
                return {"ok": False, "error": detail or f"HTTP {resp.status_code}"}
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"声纹体检失败：{exc}"}

    @API("jingge_studio_clean", description="对干声素材集执行一键静音门控清洗")
    async def jingge_studio_clean(self, dataset: str) -> dict[str, Any]:
        try:
            resp = await self._client().post(
                "/studio/clean",
                json={"dataset": (dataset or "").strip()},
                timeout=7.0,
            )
            if resp.status_code >= 400:
                detail = ""
                with contextlib.suppress(Exception):
                    detail = str(resp.json().get("detail") or "")
                return {"ok": False, "error": detail or f"HTTP {resp.status_code}"}
            return {"ok": True, "task": resp.json()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"启动清洗失败：{exc}"}

    @API("jingge_studio_cancel", description="停止当前干声分离或训练任务")
    async def jingge_studio_cancel(self) -> dict[str, Any]:
        try:
            resp = await self._client().post("/studio/cancel", timeout=6.0)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"停止任务失败：{exc}"}

    # ------------------------------------------------------------ 生命周期
    async def on_load(self) -> None:
        self._unloading = False
        self._state_file = Path(self.ctx.paths.data_dir) / "pending_jobs.json"
        self._started_flag = Path(self.ctx.paths.data_dir) / "api_started_by_plugin.flag"
        self._load_state()
        await self._get_bot_nickname(force=True)
        self.ctx.logger.info("麦弦插件已加载，API=%s，机器人昵称=%s，待恢复任务 %d 个",
                             self.config.cover.api_base, self._bot_nickname or "麦弦", len(self._pending))
        for job_id, record in list(self._pending.items()):
            self._spawn(self._watch(job_id, record))
        # 重载/加载时也按需拉起服务（后台执行，避免阻塞加载）
        if self._can_auto_start():
            self._spawn(self._auto_start_on_load())

    async def _auto_start_on_load(self) -> None:
        """加载时若服务离线则尝试拉起，结果只记日志，不阻塞 on_load。"""
        try:
            ok, msg = await self._ensure_service()
            if ok:
                self.ctx.logger.info("麦弦：翻唱服务已就绪")
            else:
                self.ctx.logger.warning("麦弦：加载时服务不可用：%s", msg)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("麦弦：加载时自动拉起异常：%s", exc)

    async def on_unload(self) -> None:
        self._unloading = True
        for token in list(self._source_uploads):
            self._drop_source_upload(token)
        if self._audio_fill is not None:
            self._audio_fill.cancel()
        async with self._audio_lock:
            for stream, _, _, _ in self._audio_cache.values():
                stream.close()
            self._audio_cache.clear()
        # 等待已经发起的启动操作，避免进程尚未监听便尝试关闭。
        async with self._get_start_lock():
            pass
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
        stop_msg = await self._stop_api()
        log = self.ctx.logger.warning if stop_msg.startswith("停止服务失败") else self.ctx.logger.info
        log("麦弦插件已卸载，任务表已保存；服务关闭：%s", stop_msg)

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        if scope == "bot":
            bot_sec = config_data.get("bot") if isinstance(config_data, dict) else None
            if isinstance(bot_sec, dict) and str(bot_sec.get("nickname") or "").strip():
                self._bot_nickname = str(bot_sec["nickname"]).strip()
            else:
                await self._get_bot_nickname(force=True)
            self.ctx.logger.info("配置已更新 (scope=%s, version=%s, nickname=%s)",
                                 scope, version, self._bot_nickname or "麦弦")

    async def _get_bot_nickname(self, *, force: bool = False) -> str:
        """动态读取麦麦全局设置里的机器人昵称（bot.nickname），取不到时回退「麦弦」。"""
        if self._bot_nickname and not force:
            return self._bot_nickname
        cfg_cap = getattr(self.ctx, "config", None)
        if cfg_cap is not None:
            try:
                raw = await cfg_cap.get("bot.nickname", "")
                if isinstance(raw, dict):
                    raw = raw.get("value", "")
                nick = str(raw or "").strip()
                if nick:
                    self._bot_nickname = nick
                    return nick
            except Exception:  # noqa: BLE001
                pass
        return self._bot_nickname or "麦弦"

    _SCOPE_PREFIXES: ClassVar[tuple[str, ...]] = (
        "qq", "qq_official", "onebot", "napcat", "telegram", "tg",
        "weixin", "weixin_clawbot", "wechat", "wx", "discord",
        "kook", "feishu", "dingtalk", "bilibili", "internal",
        "group", "private", "user",
    )

    @classmethod
    def _norm_scope_token(cls, raw: str) -> str:
        """归一化黑白名单条目：支持纯 ID、平台前缀（qq:/telegram:/weixin_clawbot: 等）或群名。"""
        s = (raw or "").strip()
        if not s:
            return ""
        if ":" in s:
            prefix, rest = s.split(":", 1)
            if prefix.lower() in cls._SCOPE_PREFIXES:
                s = rest.strip()
        return s.lower()

    @staticmethod
    def _send_ok(result: Any) -> bool:
        """统一判断宿主 send.* 返回值（兼容 bool、{'success': bool}、{'sent': bool}）。"""
        if isinstance(result, dict):
            if "sent" in result:
                return result.get("sent") is True
            if "success" in result:
                return result.get("success") is True
            return False
        return bool(result)

    @staticmethod
    def _platform_from_kwargs(kwargs: dict[str, Any]) -> str:
        """从 Tool/Command 入参或嵌套 MessageDict 中提取平台标识。"""
        direct = str(kwargs.get("platform") or "").strip().lower()
        if direct:
            return direct
        msg = kwargs.get("message")
        if isinstance(msg, dict):
            plat = str(msg.get("platform") or "").strip().lower()
            if plat:
                return plat
        return ""

    async def _resolve_stream_meta(self, stream_id: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        """解析当前会话的平台、群聊/私聊属性、群号、用户 ID 与名称（兼容全平台适配器）。"""
        group_id = self._group_id(kwargs)
        user_id = self._extract(kwargs, ("user_id", "sender_id", "qq", "operator_id"))
        platform = self._platform_from_kwargs(kwargs)
        name = ""
        is_group: bool | None = True if group_id else None

        if stream_id:
            now = time.time()
            cached = self._stream_cache.get(stream_id)
            if cached is None or (now - self._stream_cache_ts) > 120.0:
                try:
                    raw = await self.ctx.chat.get_all_streams(platform="all_platforms")
                    items = self._unwrap_streams(raw)
                    if not items:
                        items = self._unwrap_streams(await self.ctx.chat.get_all_streams(platform="qq"))
                    if items:
                        new_map: dict[str, dict[str, Any]] = {}
                        for item in items:
                            sid = str(item.get("session_id") or item.get("stream_id") or "").strip()
                            if not sid:
                                continue
                            gid = str(item.get("group_id") or "").strip()
                            uid = str(item.get("user_id") or "").strip()
                            plat = str(item.get("platform") or "").strip().lower()
                            cname = str(
                                item.get("group_name")
                                or item.get("session_name")
                                or item.get("user_cardname")
                                or item.get("user_nickname")
                                or ""
                            ).strip()
                            new_map[sid] = {
                                "is_group": self._stream_is_group(item),
                                "group_id": gid,
                                "user_id": uid,
                                "platform": plat,
                                "name": cname,
                            }
                        if new_map:
                            self._stream_cache = new_map
                            self._stream_cache_ts = now
                    cached = self._stream_cache.get(stream_id)
                except Exception:  # noqa: BLE001
                    cached = self._stream_cache.get(stream_id)

            if isinstance(cached, dict):
                if not group_id and cached.get("group_id"):
                    group_id = str(cached["group_id"])
                if not user_id and cached.get("user_id"):
                    user_id = str(cached["user_id"])
                if not platform and cached.get("platform"):
                    platform = str(cached["platform"]).lower()
                if cached.get("name"):
                    name = str(cached["name"])
                if is_group is None:
                    is_group = bool(cached.get("is_group"))

        if is_group is None:
            is_group = bool(group_id)
        return {
            "is_group": is_group,
            "group_id": group_id,
            "user_id": user_id,
            "platform": platform,
            "stream_id": stream_id,
            "name": name,
        }

    async def _resolve_platform(self, stream_id: str, kwargs: dict[str, Any] | None = None) -> str:
        """返回目标会话的归一化平台标识（如 qq / telegram / weixin_clawbot / internal）。"""
        plat = self._platform_from_kwargs(kwargs or {})
        if plat:
            return plat
        if stream_id:
            meta = await self._resolve_stream_meta(stream_id, kwargs or {})
            return str(meta.get("platform") or "").strip().lower()
        return ""

    @staticmethod
    def _should_use_qq_master(platform: str) -> bool:
        """仅对 QQ 系平台（含未知兜底）启用 SILK v3 防 AGC 抽吸母带；Telegram/Discord 等保留 44.1kHz 高保真。"""
        p = (platform or "").strip().lower()
        return (not p) or p in ("qq", "qq_official", "onebot", "napcat", "snowluma")

    async def _check_scope(self, stream_id: str, kwargs: dict[str, Any]) -> tuple[bool, str]:
        """校验当前会话是否符合插件设置里的群聊/私聊开关与黑白名单规则（兼容多平台 ID 与 Telegram 话题）。"""
        scope = getattr(self.config, "scope", None)
        if scope is None:
            return True, ""
        meta = await self._resolve_stream_meta(stream_id, kwargs)
        is_group = bool(meta["is_group"])

        if is_group:
            if not scope.allow_group:
                return False, "当前插件设置已关闭群聊翻唱功能"
            mode = str(scope.group_list_mode or "")
            entries = {self._norm_scope_token(x) for x in (scope.group_list or []) if self._norm_scope_token(x)}
            raw_vals = [meta["group_id"], meta["stream_id"], meta["name"]]
            # Telegram 论坛话题 group_id 形如 "-100123456:42"，同时把父群 ID 加入匹配集
            gid = str(meta["group_id"] or "")
            if ":" in gid:
                raw_vals.append(gid.split(":", 1)[0])
            cands = {self._norm_scope_token(v) for v in raw_vals if self._norm_scope_token(v)}
            matched = bool(cands & entries)
            if "白名单" in mode:
                if not matched:
                    return False, "当前群聊不在翻唱白名单内"
            elif "黑名单" in mode:
                if matched:
                    return False, "当前群聊已被加入翻唱黑名单"
        else:
            if not scope.allow_private:
                return False, "当前插件设置已关闭私聊翻唱功能"
            mode = str(scope.private_list_mode or "")
            entries = {self._norm_scope_token(x) for x in (scope.private_list or []) if self._norm_scope_token(x)}
            cands = {
                self._norm_scope_token(v)
                for v in (meta["user_id"], meta["stream_id"], meta["name"])
                if self._norm_scope_token(v)
            }
            matched = bool(cands & entries)
            if "白名单" in mode:
                if not matched:
                    return False, "当前私聊不在翻唱白名单内"
            elif "黑名单" in mode:
                if matched:
                    return False, "当前私聊已被加入翻唱黑名单"
        return True, ""

    # ------------------------------------------------------------ 工具：提交
    @Tool(
        "feiyu_cover",
        description=(
            "用当前机器人的歌声翻唱一首歌，成品生成后会自动发到群里。"
            "成品出现的快慢不固定，回复时不要提到任何时间或时长。\n"
            "\n"
            "【该调用】群友说「翻唱 XX」「点歌翻唱 XX」「唱一首 XX」「用你的声音唱 XX」"
            "「给我唱 XX」「你会唱 XX 吗」，或者回复某首歌说「来一首」。\n"
            "\n"
            "【重要规则 · 允许重唱且禁止自行拒单】\n"
            "- 本工具允许同一首歌反复重唱、也允许换歌手/版本重唱（如刚唱过《大鱼》又点「大鱼 周深」）。\n"
            "- 绝对不要以「刚唱过」「还在排队」「成品还没出」为由拒绝调用本工具！是否真的排队中或冷却中由后端代码实时判断。\n"
            "\n"
            "【不该调用】只是闲聊提到某首歌、问你听没听过；对方明确只是想听原唱。\n"
            "\n"
            "【参数】song 填歌名，可带歌手名帮助匹配，例如「大鱼 周深」「起风了」；"
            "pitch 留空或填 0 会由引擎按音色智能匹配最佳变调，只有用户明确要求升降调时才填。\n"
            "\n"
            "调用一次后会立即返回接单结果，不需要等待生成完成。\n"
            "接单播报由插件直接发到群里，调用本工具后本轮就结束了——"
            "不要再生成任何正文回复、不要评论、不要补充说明，"
            "否则会和插件的播报、稍后自动发送的成品重复刷屏。\n"
            "重要：不要擅自改写用户点播的歌曲信息。用户说了版本（如「司凤版」「女声版」「Live」）"
            "就按原样传，不要替换成原版，也不要自己加歌手名覆盖用户给的信息。"
        ),
        parameters=[
            ToolParameterInfo(
                name="song",
                param_type=ToolParamType.STRING,
                description=(
                    "歌曲名，可带歌手名帮助匹配，例如「大鱼 周深」「雨爱」。"
                    "**用户指定了版本、翻唱者或编曲时，必须原样保留，不得删改或替换成原版**，"
                    "例如「雨爱 司凤版」「起风了 女声版」「大鱼 唱片版」都要照抄。"
                    "只有用户没提版本时，才用「歌名 歌手」的形式。"
                ),
                required=True,
            ),
            ToolParameterInfo(
                name="pitch",
                param_type=ToolParamType.INTEGER,
                description="变调半音数。留空或 0 会自动按音色智能匹配最佳变调（防高音尖锐），仅在用户明确要求升降半音时才填",
                required=False,
            ),
            ToolParameterInfo(
                name="requester",
                param_type=ToolParamType.STRING,
                description="点歌群友的昵称（从聊天记录里直接填，不确定可留空）",
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
            "不传 job_id 会返回最近一个任务的状态，通常够用。\n"
            "注意：如果返回「没有任务在跑 / 已唱好并发送」，说明成品早就发到群里了，"
            "不要理解为失败，也不要向群友道歉说没唱成。"
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
            # 没有「进行中」任务 ≠ 失败。成品早就发过的话，要明确说出来，
            # 否则 planner 会把「无任务中」误读成「唱失败了」，对群里说抱歉。
            try:
                recent = await self._client().get("/jobs", timeout=10.0)
                recent.raise_for_status()
                jobs = recent.json().get("jobs") or []
                done = next((j for j in jobs if j.get("status") == "done"), None)
                if done:
                    title = (done.get("meta") or {}).get("title") or done.get("song") or "翻唱"
                    hit = "（这首是现成缓存，早就发到群里了）" if (done.get("meta") or {}).get("cache_hit") else "（成品已经发到群里了）"
                    return {"success": True,
                            "message": f"最近一次翻唱《{title}》已经唱好并发送了{hit}，目前没有新的任务在跑。"}
            except Exception:  # noqa: BLE001
                pass
            return {"success": True, "message": "目前没有正在进行的翻唱任务；如果刚点过歌，成品通常几秒内就会发到群里。"}
        if not job_id:
            job_id = list(self._pending.keys())[-1]
        try:
            data = await self._get_job(job_id)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "message": f"查询失败: {exc}"}
        return {"success": True, "message": self._describe(data)}

    @Tool(
        "feiyu_cover_cancel",
        description=(
            "取消当前正在进行或排队的翻唱任务。群友说「别唱了」「取消翻唱」「停一下」时调用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="job_id",
                param_type=ToolParamType.STRING,
                description="任务编号，留空则取消当前群里正在进行的翻唱",
                required=False,
            ),
        ],
    )
    async def handle_cancel(self, job_id: str = "", **kwargs: Any) -> dict[str, Any]:
        stream_id = str(kwargs.get("stream_id") or "")
        allowed, reason = await self._check_scope(stream_id, kwargs)
        if not allowed:
            return {"success": False, "message": reason}
        target_id = (job_id or "").strip()
        if not target_id:
            for jid, rec in reversed(list(self._pending.items())):
                if not stream_id or rec.get("stream_id") == stream_id:
                    target_id = jid
                    break
        if not target_id:
            return {"success": True, "message": "当前没有正在进行中的翻唱任务。"}
        try:
            res = await self.jingge_cancel(target_id)
            return {"success": bool(res.get("ok")), "message": res.get("message") or "已取消翻唱任务"}
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "message": f"取消失败：{exc}"}

    # ------------------------------------------------------------ 互动小游戏：麦麦清唱猜歌
    @Tool(
        "feiyu_cover_quiz",
        description=(
            "在群里发起一局「清唱猜歌」小游戏！\n"
            "【该调用】群友说「玩猜歌」「来一局猜歌」「清唱猜歌」「考考大家听歌」时调用。\n"
            "如果本地曲库有缓存会秒开局；没有缓存或群友要猜新歌时，会自动 60 秒极速现学一首热歌副歌再开唱。\n"
            "调用后插件会直接发送清唱语音条和规则提示，本轮无需再补充长文。"
        ),
        parameters=[
            ToolParameterInfo(
                name="fresh",
                param_type=ToolParamType.BOOLEAN,
                description="是否强制上网现学一首本地没唱过的新热歌（默认 false，优先复用本地曲库秒开局）",
                required=False,
            ),
        ],
    )
    async def handle_quiz_tool(self, fresh: bool = False, **kwargs: Any) -> dict[str, Any]:
        stream_id = str(kwargs.get("stream_id") or "")
        return await self._start_quiz(stream_id, kwargs, force_fresh=bool(fresh))

    @Tool(
        "feiyu_cover_guess",
        description=(
            "提交「麦麦清唱猜歌」的猜歌答案或揭晓谜底。\n"
            "【该调用】当前群里正在进行清唱猜歌，群友猜了一个歌名，或者说「猜不出来 / 揭晓答案 / 放弃」时调用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="guess",
                param_type=ToolParamType.STRING,
                description="群友猜的歌名；若是想放弃看谜底，传「揭晓答案」",
                required=True,
            ),
        ],
    )
    async def handle_guess_tool(self, guess: str = "", **kwargs: Any) -> dict[str, Any]:
        stream_id = str(kwargs.get("stream_id") or "")
        user_id = self._extract(kwargs, ("user_id", "sender_id", "qq", "operator_id"))
        nickname = await self._resolve_requester(kwargs, stream_id, fallback_uid=user_id) or "这位群友"
        return await self._check_quiz_guess(stream_id, guess, nickname, kwargs, user_id=user_id)

    @Tool(
        "feiyu_cover_rank",
        description=(
            "查看当前群聊的「清唱猜歌排行榜」（仅展示前 5 名歌神与积分）。\n"
            "【该调用】群友说「看猜歌排行榜」「谁是歌神」「猜歌积分榜」「猜歌排名」时调用。\n"
            "调用后插件会直接发送排行榜卡片图，本轮无需再补充长文。"
        ),
        parameters=[],
    )
    async def handle_rank_tool(self, **kwargs: Any) -> dict[str, Any]:
        stream_id = str(kwargs.get("stream_id") or "")
        return await self._show_quiz_rank(stream_id, kwargs)

    @Command(
        "feiyu_cover_quiz_cmd",
        description="发起一局 15 秒清唱猜歌小游戏：/猜歌（或 /猜歌 新歌 现学热歌）",
        pattern=r"^(?:/猜歌|清唱猜歌)(?:\s+(?P<mode>新歌|热歌))?$",
    )
    async def handle_quiz_cmd(self, stream_id: str = "", matched_groups: dict | None = None,
                              **kwargs: Any) -> tuple[bool, str, int]:
        force_fresh = bool(((matched_groups or {}).get("mode") or "").strip())
        res = await self._start_quiz(stream_id, kwargs, force_fresh=force_fresh)
        if not res.get("success") and not res.get("silent") and stream_id:
            await self.ctx.send.text(res.get("message", "开局失败"), stream_id)
        return True, res.get("message", ""), 2

    @Command(
        "feiyu_cover_guess_cmd",
        description="回答猜歌答案或揭晓谜底：/猜 <歌名>、/猜歌名 <歌名> 或 /揭晓答案",
        pattern=r"^(?:/猜歌名\s+(?P<guess1>.+)|/猜(?!歌(?:\s|$)|歌榜$|歌排行榜$|歌名$)\s*(?P<guess2>.+)|/揭晓答案|/看答案|/放弃猜歌)$",
    )
    async def handle_guess_cmd(self, stream_id: str = "", matched_groups: dict | None = None,
                               **kwargs: Any) -> tuple[bool, str, int]:
        mg = matched_groups or {}
        guess = (mg.get("guess1") or mg.get("guess2") or "揭晓答案").strip()
        user_id = self._extract(kwargs, ("user_id", "sender_id", "qq", "operator_id"))
        nickname = await self._resolve_requester(kwargs, stream_id, fallback_uid=user_id) or "这位群友"
        res = await self._check_quiz_guess(stream_id, guess, nickname, kwargs, user_id=user_id)
        if stream_id and res.get("reply") and not res.get("silent"):
            await self.ctx.send.text(res["reply"], stream_id)
        return True, res.get("message", ""), 2

    @Command(
        "feiyu_cover_rank_cmd",
        description="查看本群清唱猜歌排行榜（前 5 名）：/猜歌排行榜",
        pattern=r"^(?:/猜歌排行榜|/猜歌榜|猜歌排行榜)$",
    )
    async def handle_rank_cmd(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        res = await self._show_quiz_rank(stream_id, kwargs)
        if not res.get("success") and not res.get("silent") and stream_id:
            await self.ctx.send.text(res.get("message", "暂无排行数据"), stream_id)
        return True, res.get("message", ""), 2

    @staticmethod
    def _clean_song_title(raw: str) -> str:
        s = (raw or "").strip()
        # 去掉常见括注和歌手后缀，提取核心歌名
        s = re.sub(r"[（(\[【《》\])）].*?[）)\]】《》]", "", s)
        s = re.split(r"\s*[-—–·/|]\s*", s)[0].strip()
        return s or (raw or "").strip()

    @staticmethod
    def _norm_guess(text: str) -> str:
        s = (text or "").lower().strip()
        s = re.sub(r'[\s《》「」『』“”‘’"\'.,，。!?！？\-—_·()（）\[\]【】]', "", s)
        return s

    async def _send_quiz_prompt(self, stream_id: str, job_id: str, title: str, core: str,
                                audio: bytes, is_acapella: bool, *, platform: str = "") -> bool:
        """发送清唱猜歌 15 秒语音条并登记当前猜歌局；若平台不支持语音则明确提示且不登记空气局。"""
        mode_label = "纯清唱" if is_acapella else "副歌片段"
        segment = {
            "type": "voice",
            "data": f"猜歌 {mode_label} 15s",
            "hash": hashlib.sha256(audio).hexdigest(),
            "binary_data_base64": base64.b64encode(audio).decode("ascii"),
        }
        voice_ok = False
        try:
            sent = await self.ctx.send.hybrid([segment], stream_id, processed_plain_text="清唱猜歌")
            voice_ok = self._send_ok(sent)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("猜歌语音条发送失败 (platform=%s): %s", platform or "unknown", exc)
        if not voice_ok:
            plat_tip = f"（{platform}）" if platform else ""
            with contextlib.suppress(Exception):
                await self.ctx.send.text(
                    f"当前平台{plat_tip}暂不支持发送语音消息，无法开启清唱猜歌。",
                    stream_id,
                )
            return False

        self._quizzes[stream_id] = {
            "job_id": job_id,
            "title": title,
            "core": core,
            "attempts": 0,
            "started_at": time.time(),
            "platform": platform,
        }
        self._save_state()
        bot_name = await self._get_bot_nickname()
        with contextlib.suppress(Exception):
            await self.ctx.send.text(
                f"🎵 {bot_name}{mode_label}猜歌开始！听上面这 15 秒猜猜在唱哪首歌～\n"
                f"💡 提示：歌名共 {len(core)} 个字（发「/猜 <歌名>」抢答，如 /猜 大鱼；或发「/揭晓答案」看谜底）",
                stream_id,
            )
        await self._pet_notify("群里开始清唱猜歌啦，猜猜我在唱什么～ 🎵", event="happy")
        return True

    async def _prepare_hot_quiz_bg(self, stream_id: str, song_query: str, *, platform: str = "") -> None:
        """后台 60 秒热歌现学极速猜歌：仅分离+换声副歌 60s，约 15~25 秒自动发 15s 清唱开局。"""
        try:
            voice_cfg = self._load_voice()
            payload = {
                "song": song_query,
                "voice": self._voice_body(voice_cfg),
                "requester": "quiz",
                "stream_id": stream_id,
                "preview_seconds": 60,
            }
            data = await self._post("/cover", payload, timeout=15.0)
            job_id = str(data.get("id") or "")
            if not job_id:
                raise RuntimeError("未拿到任务号")

            deadline = time.time() + 180.0
            final_job: dict[str, Any] | None = None
            while time.time() < deadline:
                await asyncio.sleep(3.0)
                jdata = await self._get_job(job_id)
                st = jdata.get("status")
                if st == "done":
                    final_job = jdata
                    break
                if st in ("failed", "interrupted", "cancelled"):
                    raise RuntimeError(str(jdata.get("error") or jdata.get("message") or "生成失败"))

            if final_job is None:
                raise RuntimeError("热歌现学超时，请稍后再试")

            raw_title = (final_job.get("meta") or {}).get("title") or song_query.split()[0]
            core = self._clean_song_title(raw_title) or song_query.split()[0]
            for_qq = 1 if self._should_use_qq_master(platform) else 0
            clip_resp = await self._client().get(
                f"/jobs/{job_id}/highlight",
                params={"seconds": 15, "vocals_only": 1, "for_qq": for_qq},
                timeout=45.0,
            )
            if clip_resp.status_code != 200 or not clip_resp.content:
                raise RuntimeError(f"切取清唱失败（HTTP {clip_resp.status_code}）")
            await self._send_quiz_prompt(
                stream_id, job_id, raw_title, core,
                clip_resp.content, clip_resp.headers.get("x-acapella") == "1",
                platform=platform,
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("热歌现学猜歌失败 (%s): %s", song_query, exc)
            with contextlib.suppress(Exception):
                await self.ctx.send.text(f"现学猜歌开局失败啦：{str(exc)[:80]}，稍后再发「/猜歌」试试～", stream_id)
        finally:
            self._quiz_preparing.discard(stream_id)

    async def _start_quiz(self, stream_id: str, kwargs: dict[str, Any] | None = None,
                          *, force_fresh: bool = False) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return {"success": False, "silent": True, "message": "翻唱功能已关闭"}
        if not self.config.features.enable_quiz:
            return {"success": False, "silent": True, "message": "清唱猜歌小游戏已在插件设置中关闭"}
        if not stream_id:
            return {"success": False, "message": "缺少会话信息"}
        allowed, reason = await self._check_scope(stream_id, kwargs or {})
        if not allowed:
            return {"success": False, "silent": True, "message": reason}
        platform = await self._resolve_platform(stream_id, kwargs)
        if platform == "weixin_clawbot":
            return {"success": False, "message": "当前微信适配器（weixin_clawbot）暂不支持发送语音消息，无法开启清唱猜歌。"}
        if stream_id in self._quiz_preparing:
            return {"success": True, "stop_after_execution": True, "message": "正在现学热歌副歌，稍等十几秒马上开唱～"}
        ok, hint = await self._ensure_service()
        if not ok:
            return {"success": False, "message": hint}
        try:
            resp = await self._client().get("/jobs", timeout=10.0)
            resp.raise_for_status()
            jobs = resp.json().get("jobs") or []
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "message": f"读取曲库失败：{exc}"}

        done_jobs = [j for j in jobs if j.get("status") == "done" and ((j.get("meta") or {}).get("title") or j.get("song"))]
        seen: set[str] = set()
        pool: list[dict[str, Any]] = []
        for j in done_jobs:
            raw_title = (j.get("meta") or {}).get("title") or j.get("song") or ""
            core = self._clean_song_title(raw_title)
            if not core or len(core) > 20 or core in seen:
                continue
            seen.add(core)
            pool.append({"job": j, "title": raw_title, "core": core})

        # 本地曲库可用且未强制要求新歌 → 0 秒极速秒开局
        for_qq = 1 if self._should_use_qq_master(platform) else 0
        if pool and not force_fresh:
            prev = self._quizzes.get(stream_id)
            if prev and len(pool) > 1:
                pool = [p for p in pool if p["core"] != prev.get("core")] or pool
            random.shuffle(pool)
            for cand in pool[:6]:
                cid = str(cand["job"]["id"])
                try:
                    clip_resp = await self._client().get(
                        f"/jobs/{cid}/highlight",
                        params={"seconds": 15, "vocals_only": 1, "for_qq": for_qq},
                        timeout=45.0,
                    )
                    if clip_resp.status_code == 200 and clip_resp.content:
                        ok_sent = await self._send_quiz_prompt(
                            stream_id, cid, cand["title"], cand["core"],
                            clip_resp.content, clip_resp.headers.get("x-acapella") == "1",
                            platform=platform,
                        )
                        if not ok_sent:
                            return {"success": False, "silent": True, "message": "当前平台不支持语音消息"}
                        return {"success": True, "stop_after_execution": True, "message": "已发出 15 秒清唱猜歌题目"}
                except Exception:  # noqa: BLE001
                    continue

        # 本地曲库为空、音频都失效、或用户点了「/猜歌 新歌」→ 自动启动 60 秒热歌现学极速通道！
        fresh_pool = [s for s in QUIZ_HOT_SONGS if self._clean_song_title(s.split()[0]) not in seen]
        pick_song = random.choice(fresh_pool or list(QUIZ_HOT_SONGS))
        self._quiz_preparing.add(stream_id)
        bot_name = await self._get_bot_nickname()
        with contextlib.suppress(Exception):
            await self.ctx.send.text(
                f"🎵 {bot_name}清了清嗓子，正在上网现学一首热歌副歌考考你们（约 20 秒马上开唱）～",
                stream_id,
            )
        self._spawn(self._prepare_hot_quiz_bg(stream_id, pick_song, platform=platform))
        return {"success": True, "stop_after_execution": True, "message": "正在后台 60 秒极速现学热歌副歌，稍后自动开局"}

    async def _check_quiz_guess(self, stream_id: str, guess: str, nickname: str,
                                kwargs: dict[str, Any] | None = None,
                                *, user_id: str = "") -> dict[str, Any]:
        if not self.config.plugin.enabled or not self.config.features.enable_quiz:
            return {"success": False, "silent": True, "message": "清唱猜歌小游戏已关闭"}
        allowed, reason = await self._check_scope(stream_id, kwargs or {})
        if not allowed:
            return {"success": False, "silent": True, "message": reason}
        quiz = self._quizzes.get(stream_id)
        if not quiz:
            return {"success": True, "reply": "当前没有进行中的猜歌局哦，发「/猜歌」来开一局吧～", "message": "无进行中的猜歌"}
        g = (guess or "").strip()
        core = str(quiz["core"])
        full_title = str(quiz["title"])
        job_id = str(quiz["job_id"])

        quiz_platform = str(quiz.get("platform") or "")
        if any(k in g for k in ("揭晓", "答案", "放弃", "不知道", "投降")):
            self._quizzes.pop(stream_id, None)
            self._save_state()
            self._record_quiz_round_only(stream_id)
            reply = f"🎉 谜底揭晓：是《{full_title}》！马上放一段完整带伴奏高潮给你们听～"
            self._spawn(self._send_highlight(job_id, full_title, stream_id, seconds=58, platform=quiz_platform))
            return {"success": True, "reply": reply, "message": reply}

        n_guess = self._norm_guess(g)
        n_core = self._norm_guess(core)
        n_full = self._norm_guess(full_title)
        hit = bool(n_guess) and (
            n_guess == n_core
            or n_guess == n_full
            or (len(n_guess) >= 2 and (n_guess in n_core or n_core in n_guess))
        )
        if hit:
            attempts = int(quiz.get("attempts", 0))
            self._quizzes.pop(stream_id, None)
            self._save_state()
            pts = 3 if attempts == 0 else (2 if attempts == 1 else 1)
            meta = await self._resolve_stream_meta(stream_id, kwargs or {})
            stat = self._record_quiz_win(
                stream_id,
                player_key=user_id or nickname,
                nickname=nickname,
                points=pts,
                first_try=(attempts == 0),
                group_name=str(meta.get("name") or ""),
            )
            tag = "一发入魂 " if attempts == 0 else ""
            streak_txt = f" · 🔥{stat['streak']}连胜" if stat["streak"] >= 2 else ""
            reply = (
                f"🏆 恭喜 {nickname} {tag}猜对啦！答案就是《{full_title}》～\n"
                f"✨ +{pts} 分（总分 {stat['points']} · 本群第 {stat['rank']} 名{streak_txt}），奖励完整带伴奏副歌！"
            )
            self._spawn(self._send_highlight(
                job_id, full_title, stream_id, seconds=58,
                platform=quiz_platform or str(meta.get("platform") or ""),
            ))
            await self._pet_notify(f"{nickname} 猜对了《{core}》！拿到 +{pts} 分～ ✨", event="happy")
            return {"success": True, "reply": reply, "message": reply}

        quiz["attempts"] = int(quiz.get("attempts", 0)) + 1
        self._save_state()
        att = quiz["attempts"]
        if att == 1:
            hint = f"还差一点点～提示：歌名共 {len(core)} 个字，再听听看！"
        elif att == 2 and len(core) >= 2:
            hint = f"再给个大提示：第一个字是「{core[0]}」（共 {len(core)} 字）！"
        else:
            hint = f"还没猜中呢～提示：「{core[0]}{'○' * max(0, len(core) - 2)}{core[-1] if len(core) > 1 else ''}」（发「/揭晓答案」可直接揭秘）"
        return {"success": True, "reply": f"不对哦 {nickname}，{hint}", "message": hint}

    # -------------------------------------------------------- 群独立猜歌排行榜
    def _quiz_scores_path(self) -> Path | None:
        try:
            return Path(self.ctx.paths.data_dir) / "quiz_scores.json"
        except Exception:  # noqa: BLE001
            return None

    def _load_quiz_scores(self) -> dict[str, Any]:
        p = self._quiz_scores_path()
        if p is None or not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    def _save_quiz_scores(self, data: dict[str, Any]) -> None:
        p = self._quiz_scores_path()
        if p is None:
            return
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(p)
        except OSError as exc:
            self.ctx.logger.warning("保存猜歌排行榜失败：%s", exc)

    def _record_quiz_round_only(self, stream_id: str) -> None:
        """无人猜中/揭晓答案时仅累计本群局数并清零连胜记录。"""
        if not stream_id:
            return
        all_scores = self._load_quiz_scores()
        bucket = all_scores.setdefault(stream_id, {"group_name": "", "rounds": 0, "last_winner": "", "players": {}})
        bucket["rounds"] = int(bucket.get("rounds", 0)) + 1
        bucket["last_winner"] = ""
        self._save_quiz_scores(all_scores)

    def _record_quiz_win(
        self,
        stream_id: str,
        *,
        player_key: str,
        nickname: str,
        points: int,
        first_try: bool,
        group_name: str = "",
    ) -> dict[str, int]:
        """写入单群独立积分榜并返回该玩家最新总分、排名与连胜。"""
        all_scores = self._load_quiz_scores()
        bucket = all_scores.setdefault(stream_id, {"group_name": group_name, "rounds": 0, "last_winner": "", "players": {}})
        if group_name:
            bucket["group_name"] = group_name
        bucket["rounds"] = int(bucket.get("rounds", 0)) + 1
        players: dict[str, dict[str, Any]] = bucket.setdefault("players", {})

        key = str(player_key or nickname or "guest").strip()
        rec = players.get(key) or {"name": nickname, "points": 0, "wins": 0, "first_try": 0, "streak": 0, "best_streak": 0}
        rec["name"] = nickname or rec.get("name") or key
        rec["points"] = int(rec.get("points", 0)) + int(points)
        rec["wins"] = int(rec.get("wins", 0)) + 1
        if first_try:
            rec["first_try"] = int(rec.get("first_try", 0)) + 1
        if bucket.get("last_winner") == key:
            rec["streak"] = int(rec.get("streak", 0)) + 1
        else:
            rec["streak"] = 1
        rec["best_streak"] = max(int(rec.get("best_streak", 0)), int(rec["streak"]))
        rec["updated_at"] = int(time.time())
        players[key] = rec
        bucket["last_winner"] = key
        self._save_quiz_scores(all_scores)

        ranked = sorted(
            players.items(),
            key=lambda kv: (-int(kv[1].get("points", 0)), -int(kv[1].get("wins", 0)), -int(kv[1].get("first_try", 0))),
        )
        rank = next((idx + 1 for idx, (k, _) in enumerate(ranked) if k == key), len(ranked))
        return {"points": int(rec["points"]), "wins": int(rec["wins"]), "streak": int(rec["streak"]), "rank": rank}

    @staticmethod
    def _build_rank_card_html(
        *,
        bot_name: str,
        group_name: str,
        top_players: list[dict[str, Any]],
        total_players: int,
        total_rounds: int,
    ) -> str:
        b_esc = html_lib.escape((bot_name or "麦弦")[:10])
        g_esc = html_lib.escape((group_name or "本群")[:16])
        max_pts = max(1, max(int(p.get("points", 0)) for p in top_players))
        rows_html: list[str] = []
        medal_cls = {1: "r1", 2: "r2", 3: "r3"}
        for idx, p in enumerate(top_players, 1):
            pts = int(p.get("points", 0))
            wins = int(p.get("wins", 0))
            ft = int(p.get("first_try", 0))
            bs = int(p.get("best_streak", 0))
            name_esc = html_lib.escape(str(p.get("name") or "群友")[:14])
            pct = max(12, min(100, int(round(pts * 100 / max_pts))))
            cls = medal_cls.get(idx, "rn")
            meta_parts = [f"猜对 {wins} 首"]
            if ft > 0:
                meta_parts.append(f"一发 {ft} 次")
            if bs >= 2:
                meta_parts.append(f"最高 {bs} 连胜")
            badge_inner = "👑" if idx == 1 else str(idx)
            rows_html.append(
                f'<div class="row {cls}">'
                f'  <div class="bar" style="width:{pct}%"></div>'
                f'  <div class="rank-badge {cls}">{badge_inner}</div>'
                f'  <div class="p-info">'
                f'    <div class="p-name">{name_esc}</div>'
                f'    <div class="p-meta">{" · ".join(meta_parts)}</div>'
                f'  </div>'
                f'  <div class="p-score"><span class="pts">{pts}</span><span class="unit">分</span></div>'
                f'</div>'
            )
        rows_str = "\n".join(rows_html)
        card_h = 116 + len(top_players) * 48
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:transparent;font-family:"PingFang SC","Microsoft YaHei","Hiragino Sans GB",sans-serif;padding:8px}}
.card{{
  width:416px;min-height:{card_h}px;border-radius:18px;
  background:linear-gradient(145deg,#FCFBF8 0%,#F5F1E9 58%,#ECF2FA 100%);
  border:1.5px solid rgba(30,41,59,.22);position:relative;overflow:hidden;
  padding:14px 16px 12px;
  box-shadow:0 8px 22px rgba(15,23,42,.10), inset 0 1px 0 rgba(255,255,255,.95);
}}
.card::before{{
  content:"";position:absolute;inset:5px;border-radius:13px;
  border:1px solid rgba(193,157,96,.40);pointer-events:none;
}}
.glow{{
  position:absolute;right:-28px;top:-28px;width:160px;height:160px;border-radius:50%;
  background:radial-gradient(circle,rgba(193,157,96,.18) 0%,rgba(59,125,216,.07) 55%,transparent 75%);
  pointer-events:none;
}}
.hd{{display:flex;align-items:center;justify-content:space-between;position:relative;z-index:1;margin-bottom:10px}}
.eyebrow{{font-size:10px;font-weight:700;letter-spacing:1.2px;color:#8D6E3B;text-transform:uppercase}}
.title{{font-size:18px;font-weight:800;color:#161E2E;line-height:1.2;margin-top:2px}}
.top-tag{{
  font-size:10.5px;font-weight:700;color:#7E5B1E;background:rgba(247,236,214,.92);
  border:1px solid rgba(193,157,96,.50);padding:3px 9px;border-radius:99px;white-space:nowrap;
}}
.rows{{display:flex;flex-direction:column;gap:6px;position:relative;z-index:1}}
.row{{
  position:relative;display:flex;align-items:center;gap:10px;height:42px;
  padding:0 11px 0 8px;border-radius:11px;overflow:hidden;
  background:rgba(255,255,255,.76);border:1px solid rgba(22,30,46,.08);
}}
.row.r1{{
  background:linear-gradient(90deg,rgba(252,243,222,.92) 0%,rgba(255,253,248,.88) 100%);
  border-color:rgba(193,157,96,.48);
}}
.row .bar{{
  position:absolute;left:0;top:0;bottom:0;pointer-events:none;
  background:linear-gradient(90deg,rgba(59,125,216,.07),rgba(59,125,216,.02));
}}
.row.r1 .bar{{background:linear-gradient(90deg,rgba(193,157,96,.16),rgba(193,157,96,.03))}}
.rank-badge{{
  width:24px;height:24px;border-radius:8px;flex:0 0 24px;position:relative;z-index:1;
  display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;
  font-variant-numeric:tabular-nums;
}}
.rank-badge.r1{{background:linear-gradient(135deg,#F3D082,#C99432);color:#fff;font-size:13px;box-shadow:0 2px 6px rgba(193,157,96,.35)}}
.rank-badge.r2{{background:linear-gradient(135deg,#DCE7F8,#9FB7DC);color:#1E3A6D}}
.rank-badge.r3{{background:linear-gradient(135deg,#ECCBB3,#C68860);color:#fff}}
.rank-badge.rn{{background:#EEF2F7;color:#64748B}}
.p-info{{flex:1;min-width:0;position:relative;z-index:1}}
.p-name{{font-size:13px;font-weight:700;color:#161E2E;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.p-meta{{font-size:10px;color:#6B778C;margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.p-score{{position:relative;z-index:1;display:flex;align-items:baseline;gap:2px;font-variant-numeric:tabular-nums}}
.p-score .pts{{font-size:17px;font-weight:800;color:#1D3566}}
.row.r1 .p-score .pts{{color:#8B5E16}}
.p-score .unit{{font-size:10px;font-weight:600;color:#737E91}}
.foot{{
  margin-top:9px;padding-top:7px;border-top:1px solid rgba(22,30,46,.08);
  display:flex;justify-content:space-between;align-items:center;
  font-size:10.5px;color:#737E91;position:relative;z-index:1;
}}
</style></head><body>
<div class="card">
  <div class="glow"></div>
  <div class="hd">
    <div>
      <div class="eyebrow">{b_esc} · 猜歌风云榜</div>
      <div class="title">{g_esc}</div>
    </div>
    <span class="top-tag">TOP {len(top_players)} 歌神</span>
  </div>
  <div class="rows">
    {rows_str}
  </div>
  <div class="foot">
    <span>本群 {total_players} 人上榜 · 累计 {total_rounds} 局</span>
    <span>发 /猜歌 抢分冲榜</span>
  </div>
</div>
</body></html>"""

    async def _show_quiz_rank(self, stream_id: str, kwargs: dict[str, Any] | None = None) -> dict[str, Any]:
        """渲染并发送当前群聊独立的清唱猜歌前 5 名排行榜卡片（渲染不可用时优雅降级纯文本）。"""
        if not self.config.plugin.enabled or not self.config.features.enable_quiz:
            return {"success": False, "silent": True, "message": "清唱猜歌小游戏已关闭"}
        if not stream_id:
            return {"success": False, "message": "缺少会话信息"}
        allowed, reason = await self._check_scope(stream_id, kwargs or {})
        if not allowed:
            return {"success": False, "silent": True, "message": reason}

        all_scores = self._load_quiz_scores()
        bucket = all_scores.get(stream_id) or {}
        players_map = bucket.get("players") if isinstance(bucket.get("players"), dict) else {}
        if not players_map:
            msg = "🎵 本群猜歌排行榜还是空的呢～发「/猜歌」抢下第一位歌神宝座吧！"
            with contextlib.suppress(Exception):
                await self.ctx.send.text(msg, stream_id)
            return {"success": True, "stop_after_execution": True, "message": msg}

        ranked = sorted(
            players_map.values(),
            key=lambda p: (-int(p.get("points", 0)), -int(p.get("wins", 0)), -int(p.get("first_try", 0))),
        )
        top_players = ranked[:5]
        total_players = len(ranked)
        total_rounds = max(int(bucket.get("rounds", 0)), sum(int(p.get("wins", 0)) for p in ranked))
        meta = await self._resolve_stream_meta(stream_id, kwargs or {})
        group_name = str(meta.get("name") or bucket.get("group_name") or "本群")
        bot_name = await self._get_bot_nickname()

        # 优先使用宿主 html2png 渲染精致香槟金排行榜卡片
        render = getattr(self.ctx, "render", None)
        sent_img = False
        if render is not None:
            try:
                html_str = self._build_rank_card_html(
                    bot_name=bot_name,
                    group_name=group_name,
                    top_players=top_players,
                    total_players=total_players,
                    total_rounds=total_rounds,
                )
                vp_h = 148 + len(top_players) * 48
                res = await render.html2png(
                    html_str,
                    selector=".card",
                    viewport={"width": 440, "height": vp_h},
                    device_scale_factor=2.0,
                    omit_background=True,
                    allow_network=False,
                    render_timeout_ms=8000,
                )
                b64 = res.get("image_base64") if isinstance(res, dict) else None
                if b64:
                    img_res = await self.ctx.send.image(str(b64), stream_id)
                    sent_img = self._send_ok(img_res)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.debug("排行榜卡片渲染降级纯文本：%s", exc)

        if not sent_img:
            medals = ("🥇", "🥈", "🥉", "4️⃣", "5️⃣")
            lines = [f"🏆 {bot_name} · {group_name} 猜歌排行榜（TOP {len(top_players)}）"]
            for i, p in enumerate(top_players):
                lines.append(
                    f"{medals[i]} {p.get('name', '群友')} — {p.get('points', 0)} 分（猜对 {p.get('wins', 0)} 首）"
                )
            lines.append(f"本群共 {total_players} 人参与 · 累计 {total_rounds} 局")
            with contextlib.suppress(Exception):
                await self.ctx.send.text("\n".join(lines), stream_id)

        return {"success": True, "stop_after_execution": True, "message": f"已展示本群猜歌前 {len(top_players)} 名排行榜"}

    # ------------------------------------------------------------ 命令：兜底
    @Command(
        "feiyu_cover_cmd",
        description="翻唱一首歌，例如：/翻唱 大鱼",
        pattern=r"^(?:/翻唱|翻唱)\s*(?P<song>.+)$",
    )
    async def handle_cover_cmd(self, stream_id: str = "", matched_groups: dict | None = None,
                               **kwargs: Any) -> tuple[bool, str, int]:
        song = ((matched_groups or {}).get("song") or "").strip()
        if not song:
            allowed, _ = await self._check_scope(stream_id, kwargs)
            if allowed and stream_id:
                await self.ctx.send.text("用法：/翻唱 歌名", stream_id)
            return True, "缺少歌名", 2
        result = await self._submit(song, {**kwargs, "stream_id": stream_id}, from_tool=False)
        if not result.get("success") and not result.get("silent") and stream_id:
            await self.ctx.send.text(result.get("message", "提交失败"), stream_id)
        return True, result.get("message", ""), 2

    # ------------------------------------------------------------ 提交逻辑
    async def _submit(self, song: str, kwargs: dict[str, Any], *, from_tool: bool) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return {"success": False, "silent": True, "message": "翻唱功能已关闭"}

        stream_id = str(kwargs.get("stream_id") or "")
        allowed, reason = await self._check_scope(stream_id, kwargs)
        if not allowed:
            return {"success": False, "silent": True, "message": reason}

        song = (song or "").strip()
        if not song:
            return {"success": False, "message": "没有收到歌名"}

        # 服务可用性检查（必要时按配置自动拉起），失败直接给用户明确提示
        ok, hint = await self._ensure_service()
        if not ok:
            return {"success": False, "message": hint}

        user_id = self._extract(kwargs, ("user_id", "sender_id", "qq", "operator_id"))
        group_id = self._group_id(kwargs)
        platform = await self._resolve_platform(stream_id, kwargs)
        nickname = await self._resolve_requester(kwargs, stream_id, fallback_uid=user_id)

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
            "voice": self._voice_body(voice_cfg, pitch_override=pitch_value),
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

        record = {
            "job_id": job_id,
            "song": song,
            "stream_id": stream_id,
            "group_id": group_id,
            "user_id": user_id,
            "platform": platform,
            "nickname": nickname,
            # 自动换候选重试时要原样复用，否则会退回默认音色、丢点歌人
            "requester": nickname,
            "voice": payload["voice"],
            "created": time.time(),
            "announced": False,
        }
        self._pending[job_id] = record
        self._save_state()
        if user_id:
            now = time.time()
            cooldown_window = int(self.config.cover.cooldown_seconds)
            # 顺手清理过期的冷却记录，避免 _cooldown 随用户数无限增长
            for uid in [k for k, t in self._cooldown.items() if now - t > cooldown_window]:
                self._cooldown.pop(uid, None)
            self._cooldown[user_id] = now

        self._spawn(self._watch(job_id, record))
        self._spawn(self._pet_notify(f"收到点歌！我去准备《{song}》啦～ 🎵", event="happy"))

        if from_tool:
            # 播报由插件自己立即发出，不走 planner：
            # planner 生成一句话要十几秒，等它说完缓存成品早就到了，时序错乱还刷屏。
            # 这里插件直接说一句「开唱」，缓存成品由 worker 延迟几秒再发，
            # 群里就是「开唱 → 成品」的自然顺序。
            # 同时 stop_after_execution 让 planner 本轮别再插嘴评论（它只知道接单信息，
            # 说多了就会冒出「等几分钟」「听完了嘛」这类和现实矛盾的话）。
            if stream_id and self.config.cover.notify_progress:
                templates = [
                    f"好嘞，开始唱《{song}》啦～",
                    f"收到～这就开唱《{song}》",
                    f"来啦，《{song}》开唱咯～",
                ]
                with contextlib.suppress(Exception):
                    await self.ctx.send.text(random.choice(templates), stream_id)
            # 工具返回值只写给 planner 看（不进群）：告诉它任务状态，
            # stop 让它本轮不再生成正文，避免滞后又多余的评论。
            return {"success": True,
                    "message": f"《{song}》已开始翻唱，成品生成后会自动发到群里。"
                               "本轮到此为止，不要再回复。",
                    "job_id": job_id,
                    "stop_after_execution": True}

        # 命令路径（/翻唱）不经过 planner，由插件自己播报一句中性确认。
        # 刻意不写任何自称（机器人叫什么由 planner/人设负责），避免写死「麦麦」。
        if stream_id and self.config.cover.notify_progress:
            with contextlib.suppress(Exception):
                await self.ctx.send.text(f"收到，这就去唱《{song}》～", stream_id)
        return {"success": True, "message": f"已接单：{song}", "job_id": job_id}

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
                # 缓存命中会秒出：刻意延迟几秒再发，让「开唱播报 → 成品到」
                # 的时序自然，不像没唱就交付。
                if (data.get("meta") or {}).get("cache_hit"):
                    delay = max(0, int(self.config.cover.cache_deliver_delay))
                    if delay:
                        self.ctx.logger.info("[%s] 命中缓存，延迟 %ds 发送成品", job_id, delay)
                        await asyncio.sleep(delay)
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
                    await self._append_planner_note(
                        record["stream_id"],
                        f"[麦弦翻唱已结束] 《{record.get('song')}》未能完成，当前翻唱队列已空闲，可随时接新歌。",
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

    async def _resolve_requester(self, kwargs: dict[str, Any], stream_id: str, *, fallback_uid: str = "") -> str:
        """尽力获取点歌群友的真实昵称（kwargs / 嵌套 user_info / 最近消息记录），取不到返回空串。"""
        direct = self._extract(kwargs, ("requester", "nickname", "user_cardname", "user_nickname", "sender_name", "user_name"))
        if direct:
            return direct
        msg = kwargs.get("message")
        if isinstance(msg, dict):
            uinfo = msg.get("user_info") or (msg.get("message_info") or {}).get("user_info")
            if isinstance(uinfo, dict):
                for k in ("user_cardname", "user_nickname", "nickname", "name"):
                    v = str(uinfo.get(k) or "").strip()
                    if v:
                        return v
        # 兜底：通过官方 message.get_recent 读最近几条消息，取最后一位真实发言群友的昵称
        msg_cap = getattr(self.ctx, "message", None)
        if msg_cap is not None and stream_id:
            try:
                recent = await msg_cap.get_recent(stream_id, limit=6)
                items = recent if isinstance(recent, list) else (recent.get("messages") if isinstance(recent, dict) else [])
                for item in reversed(items or []):
                    if not isinstance(item, dict):
                        continue
                    if item.get("is_bot") or item.get("from_bot"):
                        continue
                    minfo = item.get("message_info") if isinstance(item.get("message_info"), dict) else {}
                    uinfo = (
                        minfo.get("user_info")
                        if isinstance(minfo.get("user_info"), dict)
                        else (item.get("user_info") if isinstance(item.get("user_info"), dict) else {})
                    )
                    acct_id = str(item.get("account_id") or "").strip()
                    uid_in_msg = str(uinfo.get("user_id") or item.get("user_id") or "").strip()
                    if acct_id and uid_in_msg and uid_in_msg == acct_id:
                        continue
                    for cand in (
                        item.get("user_cardname"),
                        item.get("user_nickname"),
                        item.get("sender_name"),
                        uinfo.get("user_cardname"),
                        uinfo.get("user_nickname"),
                    ):
                        name = str(cand or "").strip()
                        if name and name not in ("麦麦", "MaiBot", "鲸歌", "麦弦", self._bot_nickname):
                            return name
            except Exception:  # noqa: BLE001
                pass
        return fallback_uid

    async def _append_planner_note(self, stream_id: str, text: str) -> None:
        """向 Maisaka 对话上下文写入一条内部状态事件，让 Planner 明确知道翻唱已完成、队列已空闲。"""
        if not stream_id or not text:
            return
        maisaka = getattr(self.ctx, "maisaka", None)
        ctx_cap = getattr(maisaka, "context", None) if maisaka is not None else None
        if ctx_cap is None:
            return
        with contextlib.suppress(Exception):
            await ctx_cap.append(
                stream_id,
                [{"type": "text", "data": text}],
                visible_text=text,
                source_kind="feiyu_cover_event",
                replyable=False,
            )

    # -------------------------------------------------------- 桌宠联动 & 黑胶单曲卡
    async def _pet_notify(self, text: str, event: str | None = None) -> None:
        """若宿主开启了桌宠且插件玩法开关开启，轻柔弹出气泡/动画；否则静默跳过。"""
        if not self.config.features.enable_desktop_pet:
            return
        pet = getattr(self.ctx, "desktop_pet", None)
        if pet is None:
            return
        with contextlib.suppress(Exception):
            await pet.say(text[:200])
        if event:
            with contextlib.suppress(Exception):
                await pet.play_event(event)

    @staticmethod
    def _normalize_deliver_mode(raw: Any) -> str:
        """统一归一化发歌模式为 highlight / two_parts / full_voice / full_file。"""
        val = str(raw or "").strip()
        low = val.lower()
        if low in ("full_file", "file") or "mp3" in low or "文件" in val:
            return "full_file"
        if low in ("full_voice", "full", "all_parts") or val.startswith("整首") or "完整分段" in val:
            return "full_voice"
        if low == "two_parts" or val.startswith("从头") or "前2段" in val:
            return "two_parts"
        return "highlight"

    @staticmethod
    def _build_song_card_html(
        *,
        title: str,
        artist: str,
        pitch: int,
        requester: str,
        highlight: bool = True,
        duration_sec: float = 0.0,
        bot_name: str = "麦弦",
        deliver_mode: str = "",
        expected_parts: int = 0,
    ) -> str:
        t_esc = html_lib.escape((title or "翻唱单曲")[:22])
        a_esc = html_lib.escape((artist or "翻唱原曲")[:20])
        r_esc = html_lib.escape((requester or "群友点播")[:14])
        b_esc = html_lib.escape((bot_name or "麦弦")[:10])
        pitch_txt = f"智能变调 {pitch:+d}" if pitch != 0 else "原调演绎"
        mode_key = deliver_mode or ("highlight" if highlight else "two_parts")
        if mode_key == "full_file":
            mode_txt = "320k 完整音频"
        elif mode_key == "full_voice":
            mode_txt = f"整首分段 · 共{expected_parts}段" if expected_parts >= 2 else "整首完整演唱"
        elif mode_key == "two_parts":
            mode_txt = "分段演唱 · 前2段"
        else:
            mode_txt = "副歌高潮直出"
        dur_m = int(duration_sec // 60)
        dur_s = int(duration_sec % 60)
        dur_txt = f"{dur_m}:{dur_s:02d}" if duration_sec > 0 else "Hi-Fi"
        return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:transparent;font-family:"PingFang SC","Microsoft YaHei","Hiragino Sans GB",sans-serif;padding:8px}}
.card{{
  width:404px;height:142px;border-radius:18px;
  background:linear-gradient(135deg,#FCFBF8 0%,#F5F1E9 60%,#ECF1F8 100%);
  border:1.5px solid rgba(30,41,59,.22);position:relative;overflow:hidden;
  display:flex;align-items:center;gap:12px;padding:11px 15px 11px 11px;
  box-shadow:0 8px 22px rgba(15,23,42,.10), inset 0 1px 0 rgba(255,255,255,.95);
}}
.card::before{{
  content:"";position:absolute;inset:5px;border-radius:13px;
  border:1px solid rgba(193,157,96,.40);pointer-events:none;
}}
.glow{{
  position:absolute;left:-20px;top:-20px;width:155px;height:155px;border-radius:50%;
  background:radial-gradient(circle,rgba(193,157,96,.18) 0%,rgba(59,125,216,.07) 52%,transparent 72%);
  pointer-events:none;
}}
.disc-wrap{{
  flex:0 0 116px;height:116px;display:flex;align-items:center;justify-content:center;
  position:relative;z-index:1;filter:drop-shadow(0 5px 10px rgba(15,23,42,.18));
}}
.info{{
  flex:1;min-width:0;display:flex;flex-direction:column;justify-content:center;
  gap:5px;position:relative;z-index:1;
}}
.top-row{{display:flex;align-items:center;justify-content:space-between;gap:6px}}
.eyebrow{{
  font-size:10px;font-weight:700;letter-spacing:1.1px;color:#8D6E3B;
  display:flex;align-items:center;gap:4px;white-space:nowrap;
}}
.title{{
  font-size:19px;font-weight:800;color:#161E2E;line-height:1.16;letter-spacing:-.2px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}}
.sub{{
  font-size:11.5px;color:#647084;line-height:1.2;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}}
.pills{{display:flex;align-items:center;gap:6px;margin-top:2px;flex-wrap:nowrap}}
.pill{{
  font-size:10.5px;font-weight:600;padding:2.5px 8px;border-radius:99px;line-height:1.3;
  border:1px solid rgba(30,41,59,.14);letter-spacing:.15px;white-space:nowrap;
}}
.pill.gold{{background:rgba(247,236,214,.90);color:#7E5B1E;border-color:rgba(193,157,96,.48)}}
.pill.slate{{background:rgba(235,242,252,.94);color:#274C88;border-color:rgba(59,125,216,.28)}}
.foot{{
  margin-top:3px;padding-top:6px;border-top:1px solid rgba(22,30,46,.08);
  display:flex;justify-content:space-between;align-items:center;gap:6px;
  font-size:10.5px;color:#737E91;white-space:nowrap;
}}
.foot b{{color:#334155;font-weight:600}}
.mono{{font-variant-numeric:tabular-nums;letter-spacing:.15px}}
</style></head><body>
<div class="card">
  <div class="glow"></div>
  <div class="disc-wrap">
    <svg width="116" height="116" viewBox="0 0 136 136" fill="none" xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient id="vinylSheen" x1="12" y1="10" x2="122" y2="126" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stop-color="#37455C"/>
          <stop offset="35%" stop-color="#1A2232"/>
          <stop offset="68%" stop-color="#27344A"/>
          <stop offset="100%" stop-color="#131926"/>
        </linearGradient>
        <linearGradient id="labelGrad" x1="44" y1="44" x2="90" y2="90" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stop-color="#E6A17E"/>
          <stop offset="100%" stop-color="#C56E52"/>
        </linearGradient>
        <linearGradient id="armGold" x1="98" y1="14" x2="120" y2="86" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stop-color="#E5C58A"/>
          <stop offset="100%" stop-color="#A88240"/>
        </linearGradient>
      </defs>
      <circle cx="64" cy="68" r="57" fill="url(#vinylSheen)" stroke="#0F172A" stroke-width="1.6"/>
      <circle cx="64" cy="68" r="49" stroke="#FFFFFF" stroke-opacity=".11" stroke-width=".9"/>
      <circle cx="64" cy="68" r="42" stroke="#FFFFFF" stroke-opacity=".09" stroke-width=".9" stroke-dasharray="18 8"/>
      <circle cx="64" cy="68" r="35" stroke="#FFFFFF" stroke-opacity=".10" stroke-width=".9"/>
      <circle cx="64" cy="68" r="28" stroke="#FFFFFF" stroke-opacity=".08" stroke-width=".8"/>
      <circle cx="64" cy="68" r="21" fill="url(#labelGrad)" stroke="#E5C58A" stroke-width="1.5"/>
      <circle cx="64" cy="68" r="14" stroke="#FFFDF7" stroke-opacity=".45" stroke-width=".9"/>
      <circle cx="64" cy="68" r="5" fill="#FCFBF8" stroke="#1E293B" stroke-width="1.4"/>
      <!-- Brushed-gold turntable tonearm -->
      <circle cx="117" cy="25" r="7.5" fill="#E5C58A" stroke="#785B28" stroke-width="1.3"/>
      <circle cx="117" cy="25" r="2.8" fill="#FCFBF8"/>
      <path d="M115 32 L107 74 L96 89" stroke="url(#armGold)" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>
      <rect x="88" y="85" width="11.5" height="6" rx="2" transform="rotate(-38 88 85)" fill="#24344D" stroke="#E5C58A" stroke-width="1"/>
    </svg>
  </div>
  <div class="info">
    <div class="top-row">
      <div class="eyebrow">MAICHORD · {b_esc}献唱</div>
      <svg width="22" height="13" viewBox="0 0 24 14" fill="none">
        <rect x="1" y="5" width="2.4" height="8" rx="1.2" fill="#C19D60"/>
        <rect x="6" y="2" width="2.4" height="11" rx="1.2" fill="#3B7DD8"/>
        <rect x="11" y="4" width="2.4" height="9" rx="1.2" fill="#C19D60"/>
        <rect x="16" y="1" width="2.4" height="12" rx="1.2" fill="#3B7DD8"/>
        <rect x="21" y="6" width="2.4" height="7" rx="1.2" fill="#C19D60"/>
      </svg>
    </div>
    <div class="title">《{t_esc}》</div>
    <div class="sub">原唱 · {a_esc}</div>
    <div class="pills">
      <span class="pill gold">{mode_txt}</span>
      <span class="pill slate">{html_lib.escape(pitch_txt)}</span>
    </div>
    <div class="foot">
      <span>点歌 · <b>{r_esc}</b></span>
      <span class="mono">{dur_txt} · 44.1kHz</span>
    </div>
  </div>
</div>
</body></html>"""

    async def _send_song_card(self, meta: dict[str, Any], title: str, stream_id: str,
                              requester: str, highlight: bool = True,
                              *, deliver_mode: str = "", expected_parts: int = 0) -> bool:
        """渲染精致紧凑的黑胶单曲卡并发送到群聊；宿主不支持或关闭时静默跳过。"""
        if not stream_id or not self.config.cover.send_song_card:
            return False
        render = getattr(self.ctx, "render", None)
        if render is None:
            return False
        try:
            bot_name = await self._get_bot_nickname()
            html_str = self._build_song_card_html(
                title=title,
                artist=str(meta.get("artist") or ""),
                pitch=int(meta.get("effective_pitch", meta.get("pitch", 0)) or 0),
                requester=requester,
                highlight=highlight,
                duration_sec=float(meta.get("duration") or 0.0),
                bot_name=bot_name,
                deliver_mode=deliver_mode,
                expected_parts=expected_parts,
            )
            res = await render.html2png(
                html_str,
                selector=".card",
                viewport={"width": 430, "height": 168},
                device_scale_factor=2.0,
                omit_background=True,
                allow_network=False,
                render_timeout_ms=8000,
            )
            b64 = res.get("image_base64") if isinstance(res, dict) else None
            if not b64:
                return False
            img_res = await self.ctx.send.image(str(b64), stream_id)
            return self._send_ok(img_res)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug("黑胶单曲卡跳过（宿主渲染不可用）：%s", exc)
            return False

    async def _send_highlight(self, job_id: str, title: str, stream_id: str,
                              seconds: int = 58, *, platform: str = "") -> int:
        """发送 58 秒副歌高潮直出语音条（QQ 走 SILK 防抽吸专调，Telegram/其他平台保留 44.1kHz 高保真）。"""
        if not stream_id:
            return 0
        for_qq = 1 if self._should_use_qq_master(platform) else 0
        try:
            resp = await self._client().get(
                f"/jobs/{job_id}/highlight",
                params={"seconds": max(15, min(60, int(seconds))), "for_qq": for_qq},
                timeout=90.0,
            )
            if resp.status_code != 200:
                return 0
            audio = resp.content
            segment = {
                "type": "voice",
                "data": f"{title} 副歌高潮",
                "hash": hashlib.sha256(audio).hexdigest(),
                "binary_data_base64": base64.b64encode(audio).decode("ascii"),
            }
            sent = await self.ctx.send.hybrid(
                [segment], stream_id, processed_plain_text=title,
            )
            return 1 if self._send_ok(sent) else 0
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("副歌高潮发送失败，退回常规分段：%s", exc)
            return 0

    async def _deliver(self, job_id: str, data: dict, record: dict[str, Any]) -> None:
        stream_id = record.get("stream_id") or ""
        meta = data.get("meta") or {}
        title = meta.get("title") or record.get("song") or "翻唱"
        requester = str(record.get("nickname") or record.get("requester") or "")
        platform = str(record.get("platform") or "").strip().lower()
        if not platform and stream_id:
            platform = await self._resolve_platform(stream_id, record)
        deliver_override = str(record.get("deliver_mode") or "").strip()
        mode_key = self._normalize_deliver_mode(deliver_override or self.config.cover.deliver_mode)
        max_full_parts = max(2, min(15, int(getattr(self.config.cover, "max_voice_parts", 8) or 8)))
        part_interval = max(0.5, min(10.0, float(getattr(self.config.cover, "voice_part_interval", 1.8) or 1.8)))
        dur_sec = float(meta.get("duration") or 0.0)
        est_parts = min(max_full_parts, max(1, int((dur_sec + 59.99) // 60))) if (mode_key == "full_voice" and dur_sec > 0) else 0

        # Telegram 暂不支持 file 段但支持语音条：选择「完整 MP3 文件」时自动优雅切换为「整首完整分段语音」
        effective_mode = "full_voice" if (mode_key == "full_file" and platform == "telegram") else mode_key

        # 1. 可选：先发一张黑胶单曲卡（支持所有允许发图的平台；渲染不可用时静默跳过，绝不阻断发歌）
        await self._send_song_card(
            meta, title, stream_id, requester,
            highlight=(effective_mode == "highlight"),
            deliver_mode=effective_mode,
            expected_parts=est_parts,
        )

        # 2. 微信 ClawBot 明确不支持 voice / file 出站，直接给出清晰指引并释放 Planner 记忆
        if platform == "weixin_clawbot":
            if stream_id:
                with contextlib.suppress(Exception):
                    await self.ctx.send.text(
                        f"《{title}》已制作完成。当前微信适配器暂不支持直接发送语音或音频文件，请前往 MaiBot 侧边栏「麦弦 → 试听工坊」在线播放或下载 MP3。",
                        stream_id,
                    )
                await self._append_planner_note(
                    stream_id,
                    f"[麦弦翻唱已完成] 《{title}》已生成完毕（当前平台暂不支持语音出站，已提示用户前往试听工坊收听），翻唱队列已空闲。",
                )
            return

        # 3. 四档发歌模式与智能兜底链：
        #    - full_file  : 直接发完整 MP3 文件 → 失败回退整首分段语音 → 文本提示
        #    - full_voice : 按 60 秒连续发完整首（受 max_voice_parts 护栏保护）→ 失败降级完整文件 → 文本提示
        #    - two_parts  : 从头连发前 2 段×60 秒 → 失败降级完整文件 → 文本提示
        #    - highlight  : 58 秒副歌高潮直出 → 失败回退前 2 段 → 完整文件 → 文本提示
        if effective_mode == "full_file":
            if await self._send_file(job_id, title, stream_id, platform=platform):
                self._spawn(self._pet_notify(f"《{title}》完整音频发好啦，快去听听看！✨", event="happy"))
                await self._append_planner_note(
                    stream_id,
                    f"[麦弦翻唱已完成并发送] 《{title}》已经唱完并以完整 MP3 音频文件发到会话中，当前翻唱队列已空闲，可随时继续点歌。",
                )
                return
            self.ctx.logger.warning("完整文件发送失败 (platform=%s)，回退为整首分段语音：%s", platform or "unknown", job_id)
            sent = await self._send_voice_parts(
                job_id, title, stream_id, platform=platform,
                max_parts=max_full_parts, interval=part_interval,
            )
            if sent > 0:
                self._spawn(self._pet_notify(f"《{title}》唱好啦，快去听听看！✨", event="happy"))
                await self._append_planner_note(
                    stream_id,
                    f"[麦弦翻唱已完成并发送] 《{title}》已经唱完并以整首分段语音（共 {sent} 段）发到会话中，当前翻唱队列已空闲，可随时继续点歌或重唱。",
                )
                return
        else:
            sent = 0
            if effective_mode == "highlight":
                sent = await self._send_highlight(job_id, title, stream_id, seconds=58, platform=platform)
                if sent <= 0:
                    sent = await self._send_voice_parts(
                        job_id, title, stream_id, platform=platform,
                        max_parts=2, interval=part_interval,
                    )
            elif effective_mode == "full_voice":
                sent = await self._send_voice_parts(
                    job_id, title, stream_id, platform=platform,
                    max_parts=max_full_parts, interval=part_interval,
                )
            else:
                sent = await self._send_voice_parts(
                    job_id, title, stream_id, platform=platform,
                    max_parts=2, interval=part_interval,
                )

            if sent > 0:
                self._spawn(self._pet_notify(f"《{title}》唱好啦，快去听听看！✨", event="happy"))
                mode_note = f"整首分段语音（共 {sent} 段）" if effective_mode == "full_voice" else (
                    f"分段语音（共 {sent} 段）" if sent > 1 else "语音条"
                )
                await self._append_planner_note(
                    stream_id,
                    f"[麦弦翻唱已完成并发送] 《{title}》已经唱完并以{mode_note}发到会话中，当前翻唱队列已空闲，可随时继续点歌或重唱。",
                )
                return
            self.ctx.logger.warning("语音条发送失败 (platform=%s)，尝试降级为完整文件：%s", platform or "unknown", job_id)
            if await self._send_file(job_id, title, stream_id, platform=platform):
                await self._append_planner_note(
                    stream_id,
                    f"[麦弦翻唱已完成并发送] 《{title}》已以音频文件形式发送，当前翻唱队列已空闲，可随时继续点歌。",
                )
                return
        if stream_id:
            plat_note = f"（当前平台 {platform} 可能不支持语音/音频文件出站）" if platform else ""
            with contextlib.suppress(Exception):
                await self.ctx.send.text(
                    f"《{title}》已唱好，但发送到聊天窗口失败{plat_note}，请前往 MaiBot 侧边栏「麦弦 → 试听工坊」收听或下载。",
                    stream_id,
                )
            await self._append_planner_note(
                stream_id,
                f"[麦弦翻唱已结束] 《{title}》已生成但未能发送到当前会话，当前翻唱队列已空闲。",
            )

    async def _send_voice_parts(
        self,
        job_id: str,
        title: str,
        stream_id: str,
        *,
        platform: str = "",
        max_parts: int = 2,
        interval: float = 1.8,
    ) -> int:
        """按 60 秒切段连续发送语音条（支持前 2 段或整首完整模式，带防限频间隔与单段瞬时重试）。"""
        if not stream_id:
            return 0
        seconds = 60
        limit = max(1, min(15, int(max_parts or 2)))
        gap = max(0.5, min(10.0, float(interval or 1.8)))
        for_qq = 1 if self._should_use_qq_master(platform) else 0
        try:
            info = await self._client().get(
                f"/jobs/{job_id}/voice-info", params={"seconds": seconds}, timeout=30.0,
            )
            info.raise_for_status()
            total_parts = max(1, int(info.json().get("parts", 1) or 1))
            parts = min(total_parts, limit)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("获取语音分段信息失败：%s", exc)
            return 0

        ok_count = 0
        for i in range(parts):
            try:
                resp = await self._client().get(
                    f"/jobs/{job_id}/voice-part/{i}",
                    params={"seconds": seconds, "for_qq": for_qq}, timeout=120.0,
                )
                if resp.status_code != 200:
                    self.ctx.logger.warning("第 %d/%d 段不可用：HTTP %s", i + 1, parts, resp.status_code)
                    break
                audio = resp.content
                segment = {
                    "type": "voice",
                    "data": f"{title} {i+1}/{parts}",
                    "hash": hashlib.sha256(audio).hexdigest(),
                    "binary_data_base64": base64.b64encode(audio).decode("ascii"),
                }
                sent_ok = False
                for attempt in range(2):
                    sent = await self.ctx.send.hybrid(
                        [segment], stream_id, processed_plain_text=title,
                    )
                    if self._send_ok(sent):
                        sent_ok = True
                        break
                    if attempt == 0:
                        self.ctx.logger.warning("第 %d/%d 段首次发送未成功，1.5 秒后重试：%s", i + 1, parts, sent)
                        await asyncio.sleep(1.5)
                if not sent_ok:
                    self.ctx.logger.warning("第 %d/%d 段重试仍未成功，中止后续分段", i + 1, parts)
                    break
                ok_count += 1
                if i < parts - 1:
                    await asyncio.sleep(gap)   # 段间留出安全间隔，避免触发平台限频与语音条乱序
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.error("第 %d/%d 段失败：%s", i + 1, parts, exc)
                break

        if ok_count:
            self.ctx.logger.info(
                "语音条发送完成：%d/%d 段（全曲共 %d 段，上限 %d，platform=%s）",
                ok_count, parts, total_parts, limit, platform or "unknown",
            )
        return ok_count

    async def _send_file(self, job_id: str, title: str, stream_id: str, *, platform: str = "") -> bool:
        """降级发送完整音频文件：兼容 OneBot/NapCat（URL）与 QQ 官方机器人（Base64），跳过不支持 file 段的平台。"""
        if not stream_id:
            return False
        if (platform or "").strip().lower() in ("telegram", "weixin_clawbot"):
            return False
        base = (self.config.cover.api_base or "").rstrip("/")
        if not base:
            return False
        safe_title = "".join(ch for ch in title if ch not in '\\/:*?"<>|')[:32] or "cover"
        file_data: dict[str, Any] = {
            "name": f"{safe_title}.mp3",
            "url": f"{base}/jobs/{job_id}/audio",
            "mime_type": "audio/mpeg",
        }
        # 音频 <= 10 MB 时附带 base64（约 13.3 MB，安全低于 MaiBot 15 MiB IPC 上限），兼容 QQ 官方机器人
        with contextlib.suppress(Exception):
            raw = await self._download_audio(job_id)
            if raw:
                file_data["size"] = str(len(raw))
                if len(raw) <= 10 * 1024 * 1024:
                    file_data["base64"] = base64.b64encode(raw).decode("ascii")
        segment = {"type": "file", "data": file_data}
        try:
            sent = await self.ctx.send.hybrid([segment], stream_id, processed_plain_text=safe_title)
            ok = self._send_ok(sent)
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
        """从 message 结构里取群号/群 ID（兼容 QQ/QQ官方/Telegram 话题）；私聊返回空串。"""
        message = kwargs.get("message")
        if isinstance(message, dict):
            info = message.get("message_info")
            if isinstance(info, dict):
                group = info.get("group_info")
                if isinstance(group, dict) and group.get("group_id"):
                    return str(group["group_id"])
                extra = info.get("additional_config")
                if isinstance(extra, dict) and extra.get("platform_io_target_group_id"):
                    return str(extra["platform_io_target_group_id"])
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
            info = message.get("message_info")
            if isinstance(info, dict):
                uinfo = info.get("user_info")
                if isinstance(uinfo, dict):
                    for key in keys:
                        value = uinfo.get(key)
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
            quizzes = data.pop("__quizzes__", {})
            self._pending = {k: v for k, v in data.items() if isinstance(v, dict)}
            if isinstance(cooldown, dict):
                self._cooldown = {str(k): float(v) for k, v in cooldown.items()}
            if isinstance(quizzes, dict):
                now = time.time()
                self._quizzes = {
                    str(k): v
                    for k, v in quizzes.items()
                    if isinstance(v, dict) and (now - float(v.get("started_at", 0))) <= 1800
                }
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
            payload["__quizzes__"] = self._quizzes
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
