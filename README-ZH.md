# Infinity-Live

**一句话总结：** 就是 [alex-remade/infinte-tv](https://github.com/alex-remade/infinte-tv)，
但很便宜——跑在 FastWan 上，用画质换成本。

面向 bilibili 直播的实时 AI 视频生成，由弹幕驱动。

由 LLM「导演」写出下一个节拍，文生视频模型把它画出来，最后以一条不中断的 RTMP 流推出去。
观众在弹幕里就能推动剧情：加入新角色、制造事件，并在一个节拍之内看到影片作出回应。

English README: [README.md](README.md)

本项目受 [alex-remade/infinte-tv](https://github.com/alex-remade/infinte-tv) 启发，
该项目用 LTX Video（本地流水线，或 fal.ai 上的 LTX 2.3 Fast）实现了由聊天驱动的实时 AI 电视。本仓库是**独立实现，并非其分支**——
模型不同、平台不同、应对延迟的思路也不同。详见[致谢](#致谢)。

## 效果示例

以下为实际运行中的画面。这部剧是绘本风奇幻冒险；角色、世界与画风都只是配置。

![带烧录字幕的镜头](docs/img/sample-shot.jpg)

*带对白字幕的镜头。本项目没有声音，所以台词被烧录在画面上。*

![更宽的场景](docs/img/sample-scene.jpg)

*角色之所以能在互相独立的片段之间保持一致，全靠把外观描述注入到每一条提示词里。*

![字幕卡](docs/img/sample-card.jpg)

*一张字幕卡。当下一个节拍是时间跳跃、路途或某个拍不出来的声响时，导演就会输出它。
字幕卡在本地约半秒渲染完成，且不花钱。*

## 思路

[alex-remade/infinte-tv](https://github.com/alex-remade/infinte-tv) 用的是 LTX Video——
既可以是本地的 LTX v1 流水线，也可以是 fal.ai 托管的 **LTX 2.3 Fast（22B 模型）**。
那是「画质优先」的一条路；而按次计费的大模型，长时间挂着跑是很贵的。

本项目押的是相反的方向：**FastWan 1.3B QAD**——体量约为其十七分之一——按播出时长算
大约**每小时 7 美元**（每个 5 秒片段约 $0.0125；当导演使用字幕卡时还会更低）。

问题在于 FastWan 没有音频，画质也偏低。以下四点让它变得够用，而第四点最容易被忽略：

- **字幕卡（title card）。** 当下一个节拍是时间跳跃、路途或某个声响时，导演会输出一张字幕卡
  而不是一个镜头。字幕卡在本地约 0.5 秒渲染完成且不花钱，因此当生成速度跟不上时，它同时也是
  一个延迟缓冲。这并非强制——由导演逐节拍决定。
- **字幕（subtitle）。** 本项目没有声音，所以台词是烧录在画面上的，而不是配音。对白的成本是
  一个字体，而不是一次 TTS 调用。
- **黑白调色。** 颗粒与对比度可以掩盖片段之间的角色形象漂移——这正是拼接独立片段时最明显的
  弱点，而彩色会让它暴露无遗。
- **刻意挑选能掩盖模型弱点的设定。** 绘本风奇幻并不是审美偏好，而是一种规避手段。小模型不擅长
  精细纹理、文字、手部、人群、硬直边缘，以及任何观众能对照现实去挑错的东西。所以这个世界只有
  村庄、森林、湖泊和遗迹：都是有机形状，没有招牌、没有车辆、没有必须画对的建筑结构。画风简单
  扁平、角色眼睛大而细节少——这恰恰是模型能稳定画好的东西。选一个模型本来就擅长的设定，同样的
  权重看起来会好得多；反过来，若选一座有制服、印刷海报和街道招牌的历史城市，就会把所有弱点
  一次性暴露出来。

第五点差异是结构性的，而非表面装饰。上游同样有「导演」——它的系统提示词开头就是
"You are the writer and director of an ongoing animated story"——但它是朝着**求新**去调的：
提示词要求它「引入新的地点、角色、物件或事件」，并使用「爆炸、传送门、突变」这类戏剧化转场，
其记忆是最近约 100 秒提示词的滚动窗口。

本项目走的是相反的路，而且是不得不这么走：22B 模型能把一个全新地点画得像样，1.3B 不能。
所以这里不是一个「会发明东西」的提示词生成器，而是一部**剧集设定（show bible）**，导演只能在
其中工作：

- **固定角色表。** 每个角色都有一段外观描述，会被原样注入到他出现的每一条提示词里。导演不能
  凭空造人；观众可以添加（有上限），一旦加入同样是永久的。
- **固定地点集合。** 一共四个。导演只能在其中切换，永远不能临时多造一个。
- **持久的剧情记忆**，而不是滚动窗口：只追加的编年史、从原始条目重新生成的摘要，以及能跨越
  重启存活的长期主线。
- **会规划「场景」的导演**，而不只是下一个镜头——每个场景都有目标和明确的收束点。

与其说它是提示词生成器，不如说它是一个小型的「节目统筹」：同一个世界、同一批面孔，每一个节拍
都如此，只要直播还开着。这正是让一个弱模型能连看几小时、而不是几秒钟的原因。

其余部分则是在 bilibili 上持续运行所必需的：

- **单连接连续 RTMP。** 互相独立的片段被拼接成一条 H.264 裸流，接收端因此永远不会认为流结束了。
- **弹幕而非 Twitch 聊天**，通过 bilibili 直播 WebSocket 读取，并经由**直播姬**推流。
- **剧情记忆。** 编年史、摘要、主线三层结构，使剧情在数百个节拍之后依然连贯。

## 功能

- 由离散片段构成的连续 RTMP 输出，片段之间不重连
- LLM 导演逐节拍决定：拍一个镜头，还是切一张字幕卡
- 观众引导剧情：添加角色、制造事件；破坏世界观的请求会被拒绝
- 剧集设定（show bible）：固定角色表、固定场景、逐角色外观描述
- 画风、角色与世界可替换，无需改动该模块以外的任何代码
- 可选的调试叠加层，把导演的规划烧录到画面上

## 架构

```
bilibili 弹幕（WebSocket）
        |
        v
   观众信号    ---->    LLM 导演    ---->   节拍：镜头，或字幕卡
 （90 秒后衰减）      编年史 / 主线 /              |
                       场景记忆                   v
                                          文生视频（5 秒片段）
                                                  |
                                                  v
                             调色 + 字幕 + 可选的调试叠加
                                                  |
                                                  v
              唯一一个长驻 ffmpeg ----> RTMP ----> 直播姬 ----> bilibili
```

## 核心组件

- `src/infinity_live/story.py` — 剧集设定与 LLM 导演。画风、角色、场景、前提设定与三层剧情
  记忆都在这里。
- `src/infinity_live/continuous.py` — 主循环。导演队列、生成缓冲、直播推送。
- `src/infinity_live/streamer.py` — ffmpeg。裸流推流器、默片调色、字幕卡与字幕渲染。
- `src/infinity_live/video_client.py` — 文生视频服务商（DeepInfra、Seedance、Wan、mock）。
- `src/infinity_live/danmaku/` — bilibili 直播 WebSocket 读取器与 mock 数据源。
- `src/infinity_live/safety.py` — 弹幕过滤与提示词审核。
- `src/infinity_live/journal.py` — 从观众文本到提示词再到片段的只追加记录。

## 快速开始

### 前置条件

- Python 3.11
- PATH 中的 FFmpeg，需包含 `libx264`、`drawtext`（freetype）以及 `setts` 比特流过滤器。
  `setts` 需要 FFmpeg 7.0 及以上版本。
- **直播姬**（哔哩哔哩直播姬），运行在可访问的主机上。它是接收 RTMP 并转推到 bilibili 的一端。
- 一个支持中日韩字符的字体，用于字幕与字幕卡。在 Windows 上代码会依次尝试
  `msyh.ttc`、`simhei.ttf`、`palab.ttf`。
- 文生视频服务与 LLM 的 API 密钥。

### 1. 克隆与安装

```bash
git clone <this-repo>
cd Infinite_Live
uv pip install --python .venv/Scripts/python.exe -e .
```

### 2. 环境配置

```bash
cp .env.example .env
```

最小可用配置：

```ini
DEEP_INFRA_API=...          # 文生视频服务
DEEPSEEK_API_KEY=...        # 导演 LLM
BILI_ROOM_ID=...            # 你的直播间号
BILI_DANMAKU_MODE=selfhosted
BILI_PUSH_URL=rtmp://<直播姬所在主机>:1935/live
BILI_STREAM_KEY=livehime
```

### 3. 先启动直播姬

直播姬只有在**收到**流之后才会开播，因此顺序很重要：

1. 启动直播姬，让它处于等待推流的状态
2. 启动本程序
3. 直播姬收到推流，房间随即开播

### 4. 运行

```bash
.venv/Scripts/python.exe -m infinity_live.cli stream \
    --provider deepinfra --danmaku selfhosted --room <房间号>
```

其他子命令：

```bash
# 真实生成一个片段，用于验证密钥是否可用
.venv/Scripts/python.exe -m infinity_live.cli probe

# 离线渲染 N 个片段到文件，不推流
.venv/Scripts/python.exe -m infinity_live.cli stream --clips 5 --out demo.mp4
```

删除 `assets/story_state.json` 即可从开场镜头重新开始一个故事；保留该文件则会接着已有的故事继续。

## 把它改成你自己的剧

仓库自带的这部剧是一个绘本风奇幻冒险，它**只是一份配置**。定义这部剧的所有内容都在
`src/infinity_live/story.py` 里。

### 角色

`CAST` 把名字映射到一段外观描述，该描述会被原样注入到这个角色出现的每一条提示词中。
视频模型没有参考图条件控制，因此这段描述是**唯一**能让角色在不同片段之间保持辨识度的东西。
要写得具体且可重复：剪影、发型、一件标志性道具。

```python
CAST: dict[str, str] = {
    "Mira": ("a young adventurer hero, auburn bob hair, a green hooded cloak, a leather "
             "satchel with a brass compass, brave and curious"),
    "Pip":  ("a tiny magical wisp companion, a glowing soft-blue body, tiny translucent "
             "wings, a little silver bell, floats in the air"),
}
```

角色描述会压过全局画风。当画风设为 moe 时，一个被描述为 `"a gaunt man in his fifties"`
的角色仍会被画成写实风格，而其他人都是 moe 风。换画风时，请重新检查每一条角色描述里是否有
与新画风冲突的词。

- `MAX_CAST_IN_SHOT`（默认 2）限制单个镜头出现几个角色，超过两个会明显损害一致性。
- `MAX_TOTAL_CAST`（默认 8）限制观众最多能添加多少角色。

### 世界（场景）

`SETTINGS` 是一份固定且简短的地点清单。导演可以在这些地点之间移动，但永远不能新建一个——
已知地点的生成效果远好于临时编造的地点。

```python
SETTINGS: dict[str, str] = {
    "village": "a small storybook village square: cobblestone ground, a round stone well...",
    "forest":  "a gentle forest path: tall simple trees, a worn dirt path, dappled sunlight...",
}
DEFAULT_SETTING = "village"
```

### 画风

预设放在 `story.py` 顶部，通过 `ANIME_STYLE` 变量选择：

- `storybook`（默认）— 柔和的 3D 动画绘本风。别名：`tv`、`3d`、`default`
- `kyoani` — 干净利落的 2D
- `chibi` — Q 版，大而闪亮的眼睛
- `weimar` — 1920 年代低饱和正剧，表现主义打光。别名：`drama`

填入无法识别的值时会回退到 `storybook`。

```bash
ANIME_STYLE=kyoani .venv/Scripts/python.exe -m infinity_live.cli stream ...
```

要新增自己的画风，写好字符串并注册到 `_STYLE_BY_KEY`。实践中有两点很关键：

- **要具体，并且要写否定词。** 只写 `"moe anime style"` 锚定力太弱，而
  `"soft rounded faces"` 会把模型带向 Q 版搞笑漫画。点明具体范式（如
  `light-novel illustration`、`key visual`），再加上明确的 `no chibi, no caricature`，
  效果要好得多。
- **让它输出彩色。** 这类模型处理彩色比黑白更擅长，所以请生成彩色画面，若想要复古质感，
  再用 `SILENT_FILM=1` 在下游调成黑白。

### 前提设定

`Story._system_prompt()` 开头有一段写给导演的剧集介绍：题材、基调、角色是谁、冲突是什么。
改写这一段，整部剧就变了。它同时也包含硬性内容禁令；请保留这些禁令，并按你的设定作相应调整。

### 开场镜头

`OPENING_PROMPT` 是一个全新故事的第一个片段。

## 配置项

美术与呈现：

- `ANIME_STYLE`（默认 `storybook`）— 画风预设
- `SILENT_FILM`（默认 `1`）— 黑白调色、颗粒、暗角。设为 `0` 则保持彩色
- `SILENT_GRAIN`（默认 `12`）— 颗粒强度。越高越能掩盖片段之间的形象漂移，代价是码率
- `SILENT_FLICKER`（默认 `0`）— 放映机闪烁，默认关闭是有意为之。早期版本以 6 Hz 与 17 Hz
  闪烁，落在光敏性癫痫的危险频段内
- `DEBUG_OVERLAY`（默认 `1`）— 把导演的规划烧录到画面上。**观众看得到**，正式直播请设为 `0`

生成与推流：

- `DEEPINFRA_MODEL`（默认 `FastVideo/FastWan-QAD-FP8-1.3B`）— 视频模型。实测
  `FastVideo/FastWan2.2-TI2V-5B-FullAttn-Diffusers` 延迟相同、略更便宜，且明显更能贯彻美术设定
- `BUFFER_TARGET`（默认 `3`）— 预生成的片段数量
- `SIGNAL_WINDOW_S`（默认 `90`）— 一条观众弹幕在信号中保留多久
- `STREAM_WIDTH` / `STREAM_HEIGHT` / `STREAM_FPS`（默认 `832` / `480` / `16`）— 标准编码参数。
  所有片段必须一致，否则解码器会在流中途重新初始化

## 项目结构

```
src/infinity_live/
    cli.py                    入口与子命令
    config.py                 基于环境变量的配置
    continuous.py             主循环：导演队列、缓冲、直播推送
    story.py                  剧集设定、LLM 导演、剧情记忆
    streamer.py               ffmpeg：推流器、调色、字幕卡、字幕
    video_client.py           文生视频服务商
    events.py                 观众信号聚合
    journal.py                提示词与片段的只追加记录
    safety.py                 弹幕过滤与提示词审核
    danmaku/
        bilibili_websocket.py bilibili 直播 WebSocket 读取器
        mock_source.py        离线模拟观众
```

运行时产物写入 `assets/`，不纳入版本管理。

## 疑难排查

**收不到任何弹幕，但连接看起来一切正常。** bilibili 读取器必须把访客 `buvid` 设备指纹放进
WebSocket **认证包体**里，而不只是放在 HTTP 头中。缺少它时，平台会接受连接、推送所有房间事件，
唯独悄悄地不发弹幕。收到 `LOG_IN_NOTICE` 只是提示信息，并不是拒绝；本项目**不需要**登录 Cookie。

**房间未开播时收不到弹幕。** bilibili 不会为未开播的房间推送弹幕。读取器仍会先行连接，
并在房间开播时自动重连。

**角色画面抖动或闪烁。** 含 B 帧的片段，其解码顺序与显示顺序不一致；此时若按包序号重建时间戳，
帧就会被放在错误的时刻上。因此所有片段都以 `-bf 0` 重新编码，使解码顺序即显示顺序。

**接收端在片段之间显示加载圈。** 说明每个片段都在用各自的 RTMP 连接推流。请设置
`CONTINUOUS_PUSH=1`，让同一个长驻推流器贯穿所有片段。

**推流到直播姬时提示连接被拒绝。** 必须先启动直播姬并让它处于等待推流的状态，再启动本程序。

## 已知限制

- 默认视频模型固定为 5 秒片段。更长的时长只有价格约十倍以上的模型才提供。
- 没有参考图条件控制，角色形象会在片段之间漂移。靠角色描述与调色才勉强可接受。
- bilibili 弹幕读取是非官方方式，平台一旦调整就可能失效。
- 成本随播出时长增长：大约每 5 秒视频 $0.01，导演选择字幕卡时更低。

## 许可

本仓库尚未选择开源许可证——在添加许可证之前保留所有权利。

## 致谢

- [alex-remade/infinte-tv](https://github.com/alex-remade/infinte-tv) — 启发了本项目。
  它用 LTX Video + FAL 展示了由聊天驱动的实时 AI 电视，其 README 声明采用 MIT 许可证。
  本仓库**未使用其任何代码**，所借鉴的是这个想法。感谢 alex-remade 将其开源。
- [FastVideo](https://github.com/hao-ai-lab/FastVideo) — 本项目使用的 FastWan 模型
- [DeepInfra](https://deepinfra.com) — 文生视频推理
- [DeepSeek](https://www.deepseek.com) — 导演 LLM
- [FFmpeg](https://ffmpeg.org) — 所有与视频相关的处理
