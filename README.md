# AI 漫剧制作系统 · 多 Agent 协作

基于 **LangGraph** 的多 Agent 协作系统，实现 AI 漫剧（动态漫画短视频）全流程制作：
需求理解 → 导演规划 → 剧本创作 → 机器审核 → **人工审核** → 锚定图确认 → 分镜/文生图/TTS/ffmpeg 合成。

## 五 Agent 三层结构

| 层 | Agent | 模型 | 职责 |
|---|---|---|---|
| 顶层 | 需求理解 | deepseek-chat | 理解用户想法；**未理解到位则直接追问，绝不臆测**；产出创作 brief |
| 中层 | 导演 | deepseek-reasoner（规划）/ deepseek-chat（监督） | 制作计划 + 角色圣经、内容监督、熔断策略 |
| 下层 | 文本创作 | qwen-max | 编写漫剧剧本 |
| 下层 | 机器审核 | deepseek-chat | 法律/道德/暴力/版权 合规底线审核 |
| 下层 | 漫剧制作 | qwen-plus | 分镜 → 文生图 → TTS → ffmpeg 合成竖屏短视频 |

**流程**：需求理解 → 导演规划 → 创作 → 机器审核 →（通过）导演监督 → **人工审核闸门** → 锚定图生成 → **锚定图确认闸门** → 制作视频。
任一审核不通过则带结构化反馈回创作；各类重试独立计数封顶，超限走 `escalate` 终止并保留产物。

## 需要配置的 API Key / 依赖

复制 `.env.example` 为 `.env` 并填入：

| Key / 依赖 | 用途 | 必需 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 需求理解 / 导演规划+监督 / 机器审核 | **是** |
| `DASHSCOPE_API_KEY` | 创作 qwen-max + 制作 qwen-plus + 通义万相文生图 + CosyVoice TTS（**一个 key 通吃阿里系**） | **是** |
| `MIMO_API_KEY` | 导演推理备选（deepseek-reasoner 不可用时降级 MiMo） | 否 |
| `VOLC_ACCESS_KEY` + `VOLC_SECRET_KEY` | 火山引擎 TTS（仅当 `TTS_PROVIDER=volcano`） | 否 |
| `ffmpeg` | 视频合成（系统二进制，非 key） | **是**（已验证 8.1.1） |

> 通义万相 / CosyVoice 均走 DashScope，与 Qwen 共用 `DASHSCOPE_API_KEY`，最少只需 DeepSeek + DashScope 两个 key。

## 快速开始

### 1. 安装依赖

```bash
pip install -e ".[dev]"
```

### 2. 配置 .env

```bash
cp .env.example .env
# 编辑 .env，填入 DEEPSEEK_API_KEY 和 DASHSCOPE_API_KEY
```

### 3. 运行

```bash
# Gradio Web UI（浏览器自动打开 http://127.0.0.1:7860）
python drama_app.py

# 或交互式命令行
python drama_app.py --cli
```

> **Windows 中文路径提示**：若 `pip install -e .` 在中文路径下写入错误的 `.pth`，运行前临时设置 `set PYTHONPATH=src`（不要用 `setx`）。

## 使用方式

### Web UI
- 在输入框写创作想法（如「做一部黛玉葬花的古风漫剧，30 秒，哀伤基调」）→「发送」。
- 系统若未理解到位会**追问**，直接在输入框回答 →「发送」。
- 剧本经机器审核 + 导演监督后，弹出**人工内容审核**：点「✅ 批准」继续，或填修订意见后「❌ 驳回」回创作。
- 锚定图生成后弹出**锚定图确认**：批准则锁定角色外观进入制作；驳回则重画。
- 完成后显示视频文件路径。

### CLI
`python drama_app.py --cli`，按提示输入想法与回答；人工审核处输入 `y/通过` 批准，否则视作驳回（输入即修订意见）。

## 跨分镜角色一致性（核心难点）

通义万相标准文生图不接受参考图，纯「ref 锁脸」不成立。本系统采用多层冗余：

1. **角色圣经（文本层）**：导演为每角色生成 FROZEN `appearance_spec`（脸/发/服饰/配色/画风，中英双语）+ `base_seed` + `voice_id`，确认后全流程不可变。
2. **冻结规格注入**：`build_image_prompt` 纯函数把每角色外观规格逐字拼入每张分镜图 prompt，同角色描述词每镜完全一致。
3. **锚定图（视觉 ground truth）**：人工审核通过后为每角色生成标准肖像，经 `confirm_anchors` 人工确认。
4. **参考图引导（可插拔）**：`T2IAdapter.supports_reference()` 声明能力；默认 WanxAdapter 不声称支持，切 img2img 端点或换可灵/即梦时启用。
5. **一致性校验闭环（可选降级）**：装 `insightface` 后做 face embedding 相似度校验；未装则降级为「seed + 文本锚定 + 人工抽检」（由 `confirm_anchors` 兜底）。
6. **TTS 音色锁定**：`voice_id` 写入角色卡，同角色全程同音色，防声音跳变。

## 人工审核中断（HITL）

三处 `interrupt()`，由 `SqliteSaver` 持久化，**进程重启后用同 `job_id`（=thread_id）即可从断点恢复**：

1. **需求澄清**：信息不足时追问，封顶 `MAX_CLARIFY=5` 轮，超限带 `assumptions` 出 brief。
2. **人工内容审核**（硬约束）：机器审核 + 导演监督通过后，必须人工批准才进制作。
3. **锚定图确认**：人工确认角色外观后才用于全片。

## 防死循环封顶

五个独立计数器，各自封顶后走确定性 `escalate` → `END`（`status=failed`，保留已生成产物）：

| 计数器 | 默认上限 | 触发 |
|---|---|---|
| `clarification_round` | 5 | 顶层澄清 |
| `machine_revision_count` | 3 | 机器/导演打回 |
| `human_revision_count` | 3 | 人工打回 |
| `anchor_round` | 2 | 锚定图重画 |
| `production_attempt_count` | 2 | 制作一致性升级 |

均可在 `.env` 覆盖。另有 `BUDGET_MAX_CNY` 预算熔断（默认 20 元），超限即 escalate。

## 测试

```bash
set PYTHONPATH=src
pytest tests/ -v
```

全部 62 个测试**无需任何 API key**（全部 mock）：覆盖状态 reducers、配置校验、LLM 工厂、文生图/TTS 适配器、ffmpeg 合成、一致性 prompt 构建、分镜解析、全图 happy path（含 2 处 HITL 中断）、澄清循环、机器驳回封顶、6 路由单元、运行时 start/resume/snapshot。

```bash
pytest tests/ --cov=manga_drama --cov-report=term-missing
```

## 项目结构

```
hongluomeng_RAG/
├── drama_app.py                 # Gradio UI / CLI 入口
├── pyproject.toml / .env.example
├── src/manga_drama/
│   ├── config.py                # 多 key 按 provider 校验
│   ├── llm.py                   # ChatOpenAI 工厂 + 成本采集 + JSON 宽松解析
│   ├── state.py                 # WorkflowState + reducers
│   ├── budget.py / errors.py / retry.py
│   ├── graph.py                 # 11 节点 + 边 + 6 条件路由 + SqliteSaver
│   ├── runtime.py               # start/resume/snapshot 封装
│   ├── nodes/                   # understand/director/write/review/human/produce/escalate
│   ├── tools/                   # t2i/tts/ffmpeg_tool/consistency/storyboard
│   └── prompts/                 # understand/director/write/review
├── tests/                       # 62 个测试（全 mock）
└── data/drama_output/           # 最终 mp4（gitignore）
```

## 模型路由理由

- **DeepSeek** 承担高频/规则化/结构化节点（理解、监督、审核）+ 推理节点（导演规划用 reasoner），快/稳/经济。
- **Qwen** 承担中文文学创作（创作用 qwen-max）+ 制作辅助（qwen-plus），中文创作最强。
- **MiMo** 作推理备选，避免单点供应商依赖。

三家均为 OpenAI 兼容，统一走 `langchain-openai` ChatOpenAI + 按 provider 切 `base_url`。

## 可插拔扩展

- **换文生图**：实现 `T2IAdapter`（`text_to_image`/`image_to_image`/`edit_image` + 能力声明），`register_t2i("keling", KelingAdapter)`，`.env` 设 `T2I_PROVIDER=keling`。
- **换 TTS**：实现 `TTSAdapter.synthesize`，`register_tts`，`TTS_PROVIDER=...`。
- **加强一致性**：`pip install scikit-learn insightface`，`tools/consistency.py` 的 `check_face_consistency` 即启用 face embedding 校验。

## 故障排查

- **MissingConfigError**：`.env` 缺 key，按提示配置。
- **文生图 size 报错**：`.env` 设 `T2I_SIZE` 为通义万相支持的尺寸（如 `720*1280`、`928*1664`）。
- **字幕中文不显示**：`subtitles` 滤镜用 `Microsoft YaHei`，未装时 `.env` 设 `FFMPEG_FONT` 指向一个中文字体文件。
- **ffprobe 找不到**：`.env` 设 `FFPROBE_PATH` 指向 ffprobe 可执行文件。
- **MiMo base_url**：小米 MiMo 接口地址若不同，`.env` 设 `MIMO_BASE_URL`。

## License
MIT
