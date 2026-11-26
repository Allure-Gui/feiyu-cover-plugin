<div align="center">
  <img src="https://fastly.jsdelivr.net/gh/Allure-Gui/feiyu-cover-plugin@main/assets/banner-plugin.webp" width="820" alt="麦弦 MaiChord">

  <br>
  <img src="https://fastly.jsdelivr.net/gh/Allure-Gui/feiyu-cover-plugin@main/assets/icon.png" width="76" alt="麦弦图标">
  <h1>麦弦 (MaiChord) · MaiBot AI 翻唱插件</h1>
  <p>让麦麦不止陪你聊天，还能用自己的声音为你唱歌：群聊点歌、58 秒副歌 / 整首完整语音 / MP3 直发、清唱猜歌，练出专属音色。</p>

  <p>
    <img src="https://img.shields.io/badge/Version-v0.2.2-3B7DD8?style=for-the-badge&labelColor=24344D" alt="Version v0.2.2">
    <img src="https://img.shields.io/badge/MaiCore-1.4.6+-4FA374?style=for-the-badge&labelColor=24344D" alt="MaiCore 1.4.6+">
    <img src="https://img.shields.io/badge/API-v0.3.1-E87A64?style=for-the-badge&labelColor=24344D" alt="API v0.3.1">
    <img src="https://img.shields.io/badge/License-MIT-E8B44C?style=for-the-badge&labelColor=24344D" alt="MIT License">
  </p>

  <p>
    <a href="https://allure-gui.github.io/feiyu-cover-plugin/" target="_blank">在线试听（《雨爱》完整翻唱）</a>
    &nbsp;·&nbsp;
    <a href="https://github.com/Allure-Gui/feiyu-cover-api">配套翻唱 API 仓库</a>
    &nbsp;·&nbsp;
    <a href="https://github.com/Allure-Gui/feiyu-cover-api/blob/main/TRAINING_GUIDE.md">部署与音色训练指南</a>
    &nbsp;·&nbsp;
    <a href="https://github.com/Allure-Gui/feiyu-cover-plugin/blob/main/CHANGELOG.md">更新日志</a>
  </p>
</div>

---

## 界面预览

<table>
  <tr>
    <td width="50%" align="center">
      <a href="https://fastly.jsdelivr.net/gh/Allure-Gui/feiyu-cover-plugin@main/assets/shot-play.png"><img src="https://fastly.jsdelivr.net/gh/Allure-Gui/feiyu-cover-plugin@main/assets/shot-play.png" width="100%" alt="试听工坊界面"></a>
      <br>
      <sub><b>试听工坊</b>：搜歌、上传音源、60 秒极速试听、整曲导出与按需推送</sub>
    </td>
    <td width="50%" align="center">
      <a href="https://fastly.jsdelivr.net/gh/Allure-Gui/feiyu-cover-plugin@main/assets/shot-vocal.png"><img src="https://fastly.jsdelivr.net/gh/Allure-Gui/feiyu-cover-plugin@main/assets/shot-vocal.png" width="100%" alt="干声工坊界面"></a>
      <br>
      <sub><b>干声工坊</b>：提取纯净人声、声纹体检与一键训练专属音色</sub>
    </td>
  </tr>
</table>

<p align="center"><sub>点击图片可查看大图</sub></p>

<div align="center">
  <img src="https://fastly.jsdelivr.net/gh/Allure-Gui/feiyu-cover-plugin@main/assets/story-flow.svg" width="880" alt="麦弦工作流程">
</div>

<br>

> [!NOTE]
> 本插件运行于 MaiBot 端，负责群聊与私聊点歌交互、清唱猜歌、分群排行榜、侧边栏调音台及语音发送；人声分离、RVC 推理换声与音色训练由独立的 **[feiyu-cover-api](https://github.com/Allure-Gui/feiyu-cover-api)**（建议 `v0.3.1`）承担。
>
> - **本机配备 8GB 及以上 NVIDIA 显卡**：可直接在本机部署 API，空闲待机显存占用为 **0 MB**；在插件设置中开启「自动拉起本地服务」后，可按需自动启停。
> - **无独立显卡或多实例共用**：可将 API 部署在远程 GPU 服务器上，在插件设置中填写服务地址与访问密钥即可连接。

---

## 主要功能

插件 `0.2.2` 配套 API `0.3.1`。支持 **4 档发歌模式**（58 秒副歌高潮 / 从头前 2 段 / **整首完整分段语音** / **直接发送完整 320k MP3 文件**），并内置整首段数护栏与段间防限频停顿；试听工坊推送会话时亦可按次自由切换发送方式。安装清单声明 MaiCore `1.0.0–1.4.99`、SDK `2.13.0–2.99.99`；设置页状态卡依赖 SDK `2.23+`。

| 模块 | 说明 |
|---|---|
| **群聊与私聊点歌** | 支持自然语言点歌（如「唱一首大鱼」「唱首雨爱」）与 `/翻唱 <歌名/BV号>` 指令，支持随时取消排队或渲染中的任务。提供 **58 秒副歌高潮直出**、**前 2 段×60 秒**、**整首完整分段语音（可设最大段数与段间间隔防限频）** 与 **完整 320k MP3 文件直发** 4 种模式，并附带黑胶单曲卡片图（自动读取机器人昵称）；QQ 端自动启用防音量起伏专调母带，其他平台保留 44.1kHz 高音质。 |
| **清唱猜歌与群排行榜** | 支持 `/猜歌`、`/猜 <歌名>`、`/揭晓答案` 与 `/猜歌排行榜`。优先复用本地缓存曲库开局；曲库为空或发送 `/猜歌 新歌` 时，通过 60 秒预切片通道在约 15~20 秒内截取 15 秒无伴奏清唱开局。猜中后发送 58 秒带伴奏副歌并按群独立累计积分（首次 `+3` / 二次 `+2` / 多次 `+1`），排行榜自动生成 TOP 5 榜单图。 |
| **音色路由（WebUI）** | 支持命名预设管理、模型切换与外部模型导入（支持 RVC WebUI / Applio 导出的 `.pth`、`.zip` 或文件夹，并提供浏览器拖拽上传页地址）；支持智能变调、GPU 显存监控、一键释放显存与缓存管理。 |
| **试听工坊（WebUI）** | 内置多平台音源检索、上传本地音源与版本选择，支持 **60 秒极速试听**、4 阶段进度展示、中途取消、原生「另存为」保存本地 320k MP3、弹窗按模式推送至指定群聊或私聊，以及近期作品回放。 |
| **干声工坊（WebUI）** | 可视化提交 Mel-Band RoFormer 干声提取、素材声纹体检（有效时长/静音占比/削波/450Hz+ 高音域覆盖）、无损静音清洗与后台 RVC 训练（支持 RefineGAN 32k 与 HiFi-GAN 40k，轮数上限 800）。 |
| **权限与多平台适配** | 设置页支持独立开关清唱猜歌与桌宠联动，支持群聊/私聊总开关与黑白名单过滤；兼容 QQ OneBot / QQ 官方机器人 / Telegram / 微信 ClawBot / 内置网页聊天等平台。 |

---

## 安装与配置

### 1. 安装插件
- **插件市场**：在 MaiBot 插件市场中搜索 **麦弦** 安装。
- **Release 压缩包**：前往 [Releases](https://github.com/Allure-Gui/feiyu-cover-plugin/releases) 下载最新版本源码包，解压至 MaiBot 的 `plugins/` 目录并将文件夹命名为 **`feiyu_cover`**，在插件管理中刷新并启用。
- **Git 克隆**：
  ```bash
  cd <MaiBot根目录>/plugins
  git clone https://github.com/Allure-Gui/feiyu-cover-plugin.git feiyu_cover
  ```

> [!TIP]
> 覆盖升级时请保留原有的 `config.toml` 与 `data/plugins/feiyu.voice-cover/` 目录（包含音色预设、路由设置与猜歌积分）。请勿在 `plugins/` 目录下同时保留两份副本，以免引起插件 ID 冲突。

### 2. 连接翻唱 API
打开 MaiBot WebUI → **插件 → 麦弦设置**：
1. 确认 `服务地址` 为 `http://127.0.0.1:19655`（连接远程服务时请填写对应地址并配置一致的 `访问密钥`）；
2. 若 API 部署于本机，开启 **`自动拉起本地服务`** 与 **`卸载时自动关闭`**，并在 **`API 文件夹路径`** 中填写 `feiyu-cover-api` 所在目录（例如 `D:\feiyu-cover-api`，留空时将自动探测常见路径）；
3. 按需调整 **`群聊发歌模式`**（副歌高潮 / 前 2 段 / 整首完整分段 / 完整 MP3 文件）、玩法开关与群聊/私聊黑白名单；
4. 点击右上角 **保存**，随后点击设置页顶部的 **「一键检测 / 拉起服务」**，待状态显示在线即完成接入。

> Windows 环境下无需手动编写 `wsl.exe` 启停命令，插件会自动识别原生一键包环境（`runtime/service-venv`）或本机 WSL2 发行版，并在插件卸载时关闭由本插件拉起的服务进程。

### 3. 配置音色与试听
打开 MaiBot 左侧边栏 **「麦弦」**：
- **音色路由**：选择目标 `.pth` 模型（或点击「导入模型」导入已有 RVC 模型），建议开启「智能变调」，点击「保存并生效」；
- **试听工坊**：检索歌曲或上传本地音频并运行「极速试听（60秒）」验证参数效果，满意后点击「应用到路由」或直接弹窗推送到群聊/私聊；
- **干声工坊**：输入歌名、BV 号、本地路径或使用上传页提取干声，体检达标后即可发起音色训练。

---

## 耗时参考与推荐参数

### 耗时参考（搭配 API `v0.3.1` + Mel-Band RoFormer）

| 运行环境 | 首次整曲（3~4 分钟） | 60 秒试听 / 现学猜歌 | 复用分离缓存换音色 |
|---|---|---|---|
| **NVIDIA 8GB GPU（如 RTX 4060 Ti）** | 约 30 ~ 45 秒 | 约 15 ~ 20 秒 | 约 5 ~ 10 秒 |
| **纯 CPU** | 约 5 ~ 20 分钟 | 约 2 ~ 6 分钟 | 约 2 ~ 5 分钟 |
| **远程 GPU 服务器** | 30 ~ 45 秒 + 网络传输 | 15 ~ 20 秒 + 网络传输 | 5 ~ 10 秒 + 网络传输 |

### 推荐声学参数

| 参数 | 说明 | 推荐值 |
|---|---|---|
| **智能变调 (`auto_pitch`)** | 自动结合模型舒适音域与原唱高音匹配半音，伴奏同步移调 | **开启**（开启时手动滑块自动锁定，关闭后可手动调节） |
| **音色贴合 (`index_rate`)** | 控制音色索引检索权重，过高易产生电音感 | `RefineGAN`：`0.50 ~ 0.65`；`HiFi-GAN`：`0.25 ~ 0.35` |
| **辅音保护 (`protect`)** | 保护咬字与气口细节，减少吞字与齿音 | **`0.45`** |
| **音量包络 (`volume_envelope`)** | 数值越小越贴合原唱强弱动态，过高易放大底噪 | **`0.25`** |
| **基频算法 (`f0_method`)** | 原唱音高提取算法 | **`rmvpe`** |
| **人声空间混响 (`vocal_reverb`)** | 添加克制的早期反射空间感，提升人声与伴奏融合度 | **开启** |
| **共振峰偏移 (`formant_shifting`)** | 调整声线厚薄；关闭时跳过额外重采样以减少清晰度损耗 | 默认 **关闭** |

<details>
<summary><b>配置项（config.toml）与对接 API 接口清单</b></summary>

<br>

#### `config.toml` 配置项（完整样例见 [`config.example.toml`](https://github.com/Allure-Gui/feiyu-cover-plugin/blob/main/config.example.toml)）

| 配置节 / 字段 | 默认值 | 说明 |
|---|---|---|
| `[plugin].enabled` | `true` | 插件总开关 |
| `[cover].api_base` | `http://127.0.0.1:19655` | 翻唱 API 服务地址 |
| `[cover].api_key` | 空 | Bearer 访问密钥（远程部署建议必填） |
| `[cover].auto_start_api` | `true` | 服务离线时自动拉起本地 API |
| `[cover].api_dir` | 空 | 本地 `feiyu-cover-api` 目录路径（留空自动探测） |
| `[cover].api_runtime` | 自动识别（推荐） | 可选 Windows WSL2 或本机直接运行 |
| `[cover].auto_stop_api` | `true` | 卸载时关闭绑定的本地实例，共享部署可关闭此项 |
| `[cover].deliver_mode` | 副歌高潮直出（58秒·推荐） | 可选：副歌高潮 / 前2段×60秒 / **整首完整分段** / **完整 MP3 文件** |
| `[cover].max_voice_parts` | `8` | 整首完整分段模式的最大连续语音段数上限（2~15，防超长串烧刷屏） |
| `[cover].voice_part_interval` | `1.8` | 多段语音连续发送的段间停顿秒数（0.5~10.0，防限频与乱序） |
| `[cover].send_song_card` | `true` | 唱完是否附带渲染黑胶单曲卡片图 |
| `[cover].cooldown_seconds` | `600` | 同用户点歌冷却间隔（秒） |
| `[cover].poll_interval` | `10` | 任务进度轮询间隔（秒） |
| `[cover].max_wait_seconds` | `1800` | 单任务最长等待时间（秒） |
| `[cover].notify_progress` | `true` | 接单时是否在会话中发送简短提示 |
| `[cover].cache_deliver_delay` | `3` | 命中缓存时的延迟发送时间（秒） |
| `[cover].api_start_cmd` / `api_stop_cmd` | 空 | *(隐藏进阶项)* 仅在需要完全自定义启停命令时使用 |
| `[features].enable_quiz` | `true` | 启用清唱猜歌与群排行榜 |
| `[features].enable_desktop_pet` | `true` | 启用 MaiBot 桌宠联动 |
| `[scope].allow_group` / `allow_private` | `true` / `true` | 是否允许在群聊 / 私聊中使用 |
| `[scope].group_list_mode` / `group_list` | 不限制 / 空 | 群聊模式（不限制 / 仅白名单 / 排除黑名单）与名单列表 |
| `[scope].private_list_mode` / `private_list` | 不限制 / 空 | 私聊模式（不限制 / 仅白名单 / 排除黑名单）与名单列表 |

> 音色路由、命名预设与分群猜歌积分持久化于 `data/plugins/feiyu.voice-cover/` 目录下的 `voice.json`、`voice_presets.json` 与 `quiz_scores.json`。

#### 对接的 API 接口（配套 `feiyu-cover-api v0.3.1`）

```text
GET  /health                     健康检查
GET  /models                     可用音色模型列表
POST /models/import              本机路径导入外部 .pth / .zip / 文件夹
POST /models/upload              浏览器上传外部模型文件
GET  /models/import-page         外部模型拖拽上传页
GET  /sources/search             多平台并发搜歌
POST /cover                      提交翻唱任务
GET  /jobs/{id}                  查询任务状态与进度
POST /jobs/{id}/cancel           取消排队或执行中的任务
GET  /jobs/{id}/voice-info       查询可切分语音条数量
GET  /jobs/{id}/voice-part/{i}   下载第 i 段语音条（支持 for_qq=1）
GET  /jobs/{id}/highlight        58 秒副歌 / 15 秒清唱（vocals_only=1 / for_qq=1）
GET  /jobs/{id}/audio            下载完整 320k MP3 成品
GET  /system/status              查询 GPU 显存与 RVC 状态
POST /system/unload              主动释放 RVC 显存
GET  /cache/stats                查询缓存统计
POST /cache/clean                清理缓存
GET  /studio                     干声工坊浏览器上传页
GET  /studio/status              查询工坊进度与素材库
POST /studio/inspect             干声素材声纹体检
POST /studio/clean               无损静音清洗
POST /studio/separate            后台 RoFormer 干声提取
POST /studio/train               后台 RVC 训练
POST /studio/cancel              中止当前工坊任务
```

</details>

<details>
<summary><b>说明与开源致谢</b></summary>

<br>

- **已知限制**：受聊天软件单条语音 60 秒时长限制，群聊默认发送 58 秒副歌片段；如需完整收听，可在插件设置中切换为「整首完整分段发送」或「直接发送完整 MP3 音频文件」（亦可在试听工坊在线播放或导出 320k MP3）。8GB 显存环境下由服务端串行排队执行以保障稳定性。
- **开源依赖**：本仓库不包含任何受版权保护的音频素材或模型权重。插件端依赖 [MaiBot SDK](https://github.com/MaiM-with-u/MaiBot) 与 [httpx](https://github.com/encode/httpx)，底层音频处理由 API 端调用 [Applio](https://github.com/IAHispano/Applio)、[python-audio-separator](https://github.com/nomadkaraoke/python-audio-separator) 及 [FFmpeg](https://ffmpeg.org/)。
- **视觉素材**：项目图标、横幅与流程示意图基于 MaiBot 官方角色形象绘制，仅用于本非商业开源项目展示。

</details>

---

<p align="center">
  <sub>MIT License — 详见 <a href="https://github.com/Allure-Gui/feiyu-cover-plugin/blob/main/LICENSE">LICENSE</a></sub>
</p>
