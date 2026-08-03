# Hermes Nexus — 人机交互 Mock 服务

> 基于 Hermes Agent 框架，实现**主动帮人预定餐馆**的端到端流程。
>
> 本项目既是 Hermes Agent 源码的深度解读，也是基于 Hermes Skill 系统构建的领域应用实例。

---

## 目录

- [Part A: 项目全景](#part-a-项目全景)
- [Part B: Hermes Agent 框架解读](#part-b-hermes-agent-框架解读)
- [Part C: 餐馆预定 — Skill 驱动实战](#part-c-餐馆预定--skill-驱动实战)
- [Part D: 源码分析索引](#part-d-源码分析索引)
- [项目结构](#项目结构)

---

## Part A: 项目全景

### A1 项目简介

**Hermes Nexus** 是一个基于 Hermes Agent 框架构建的人机交互 Mock 服务。它的核心场景是：**用户想找餐馆并预定，AI 代理自动完成从需求收集、POI 搜索、到模拟电话外呼确认的全流程**。

```
用户说"帮我找餐厅" → Agent 收集需求 → POI 搜索 → LLM 扮演行政人员打电话确认 → 返回预定结果
```

**核心特性：**

- **5 阶段流水线 (5-Phase Pipeline)**：信息输入 → 对象获取 → 信息输出 → 发起交互 → 结果整理
- **双 Skill 协作**：`chinese-poi-search` 负责 POI 搜索，`interactive-task-food` 负责对话编排
- **4 模块对话流**：基础信息 → 约束消解 → 增值服务 → 全局校验，模块间可回退修改
- **约束冲突检测**：自动检测宠物/包间/孕妇/排队等约束冲突，提供消解方案
- **LLM 驱动的电话外呼模拟**：LLM 扮演公司行政人员，自然地打电话给餐厅确认预定
- **FastAPI 服务化**：REST API 接口，支持 HTTP 调用和流式 SSE 响应
- **Mock/Live 双模式**：无 API Key 时自动 fallback 到 mock LLM，支持完整流程测试

### A2 核心架构

```
┌──────────────────────────────────────────────────────┐
│                   Hermes Agent (上游)                  │
│  Skill 加载 → 对话编排 → 工具调用 → LLM 推理           │
└────────────┬────────────────────────────┬──────────────┘
             │ 加载 Skill                  │ HTTP API 调用
             ▼                             ▼
┌────────────────────────┐    ┌──────────────────────────┐
│  chinese-poi-search    │    │    hermes-nexus (本项目)    │
│  (POI 搜索 Skill)      │    │                            │
│                        │    │  main.py    FastAPI 服务    │
│  amap_poi_tool.py      │    │  src/build_prompt.py       │
│  ├─ search_places()    │    │    组装 system_prompt       │
│  ├─ search_nearby()    │    │  src/chat.py               │
│  ├─ search_around()    │    │    ChatSession 编排多轮对话  │
│  ├─ resolve_           │    │  src/channel.py            │
│  │   restaurants()     │    │    TerminalChannel 通信渠道  │
│  └─ filter_restaurants │    │                            │
│       ()               │    └──────────────────────────┘
└────────────────────────┘
```

**hermes-nexus 核心组件：**

| 文件 | 作用 |
|------|------|
| `main.py` | FastAPI 服务，提供 `POST /api/v1/chat`、`POST /api/v1/chat/stream` 和 `GET /api/v1/health` 端点 |
| `src/build_prompt.py` | 将 Phrase 3 的 sections 格式化为 LLM system prompt，LLM 扮演行政人员打电话订座 |
| `src/chat.py` | `ChatSession` 编排：构建 prompt → 打开渠道 → 多轮 LLM 对话 → 检测 `[CONVERSATION_COMPLETE]` 标记 → 返回消息 |
| `src/channel.py` | `TerminalChannel` 通信渠道，支持真实终端输入和 mock 预设脚本两种模式 |

### A3 快速开始

```bash
# 1. 激活环境
conda activate hermes_nexus

# 2. 启动服务
python main.py
# → 服务启动在 http://localhost:8000

# 3. 健康检查
curl -s http://localhost:8000/api/v1/health
# → {"status":"ok","service":"hermes-nexus","version":"0.1.0"}

# 4. 发起对话（需要先准备好 Phrase 3 输出 JSON）
curl -s -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d @phrase3_output.json

# 5. 体验 Demo（三种模式）
python run_demo.py          # 交互模式（真实终端输入）
python run_demo.py --mock   # Mock 模式（预设脚本自动运行）
python run_demo.py --api    # API 模式（通过 HTTP 调用服务）
```

---

## Part B: Hermes Agent 框架解读

> 本项目包含对 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) v0.19.0 的 23 篇深度源码分析，覆盖全局架构、AI 核心、工具系统、智能体系统、平台基础设施五大模块。
>
> 基于 [luyao618/Claude-Code-Source-Study](https://github.com/luyao618/Claude-Code-Source-Study) V0.9.0 版本进行的更新变动与个人解读。
>
> 📖 **详见 [Hermes_Summary.md](Hermes_Summary.md)**

---

## Part C: 餐馆预定 — Skill 驱动实战

### C1 技能概览

本项目实现了 Hermes Agent 框架中**最核心的扩展机制 —— Skill（技能）** 的领域应用。

两个协作技能位于 `skills/` 目录：

```
skills/
├── chinese-poi-search/        # POI 搜索技能（Phase 2 的 Resolver）
│   ├── SKILL.md               #   技能定义（含 20 个实战 pitfall）
│   ├── scripts/
│   │   └── amap_poi_tool.py   #   高德 POI API 封装（33KB，6 个命令）
│   └── references/            #   5 篇参考文档
│       ├── amap-poi-api.md          # API 端点完整参考
│       ├── cross-skill-validation.md # 跨技能引用验证
│       ├── late-night-filtering.md   # 夜宵过滤策略
│       ├── compound-name-geocoding.md # 复合地名地理编码
│       └── hermes-nexus-phrase4.md   # Phrase 4 交互服务文档
│
└── interactive-task-food/     # 餐馆预定领域技能（主导 Skill）
    └── SKILL.md               #   44KB：5 阶段流水线 + 4 模块对话流
```

**技能协作关系：**

```
interactive-task-food (领域 Skill — 主导编排)
  │
  ├─ Phase 2: 获取可交互对象
  │   └─ 委托 → chinese-poi-search (工具 Skill)
  │              └─ amap_poi_tool.resolve_restaurants()
  │                  三种搜索模式: 地点名周边 / 城市区域 / 坐标周边
  │
  ├─ Phase 4: 发起交互
  │   └─ 委托 → hermes-nexus (本项目 FastAPI 服务)
  │              └─ POST /api/v1/chat
  │
  └─ Phase 5: 结果整理
      └─ 本地处理 → 逐项对比用户需求 vs 餐厅回复 → 输出决策建议
```

### C2 Phase 1-2: 信息收集与 POI 搜索

**Phase 1 — 信息输入**：定义三类信息用途及其如何分布到对话模块中：

| 用途 | 对应模块 | 示例信息项 |
|------|----------|-----------|
| **(1) 筛选可交互对象** | 模块一: 基础信息收集 | 菜系、城市、地点、评分、人均消费、人数 |
| **(2) 与交互对象确认** | 模块二: 约束消解 + 模块三: 增值服务 | 宠物、小孩、包间、停车、低消、生日 |
| **(3) 后置判断** | 模块四: 全局校验 | 所有信息作为 Phrase 5 对比的基准线 |

**Phase 2 — 获取可交互对象**：核心函数 `resolve_restaurants()`，三种搜索模式：

```python
# 模式 1: 地点名周边搜索（推荐）—— 用户说"左家庄附近"
resolve_restaurants(cuisine="螺蛳粉", place_name="左家庄南里", area="北京")

# 模式 2: 城市区域搜索 —— 用户说"上海浦东"
resolve_restaurants(cuisine="火锅", area="上海", district="浦东",
                    min_rating=4.5, max_cost=150, party_size=4)

# 模式 3: 坐标周边搜索 —— 已知经纬度
resolve_restaurants(cuisine="咖啡", location="121.4752,31.2297", radius=1000)
```

底层使用高德 POI API v3 端点（个人 Key 只有 v3 能返回评分/人均/电话）。SKILL.md 记录了 **20 个实战 Pitfalls**，包括：GCJ-02 vs WGS-84 坐标偏移、execute_code 沙箱不继承环境变量、复合地名 geocode 歧义、团建/高端餐厅特殊搜索策略等。

### C3 Phase 3: 对话式需求采集

Phase 3 采用 **4 模块对话流**，模块间可回退修改：

```
[模块一: 基础信息收集] ◀─────────────────────┐
  │ 用餐时间 / 人数 / 饮食偏好 / 城市 / 地点        │
  ▼                                              │
[模块二: 约束探测与消解] ◀─────────────────────┤
  │ 孕妇 / 小孩 / 宠物 / 包间 / 私密性 / 排队容忍    │
  │ └→ 内部循环: 冲突检测 → 方案推荐 → 用户选择      │
  ▼                                              │
[模块三: 增值服务关联] ◀─────────────────────┤
  │ 停车 / 特殊餐具 / 低消确认 / 生日定制            │
  ▼                                              │
[模块四: 全局校验与确认] ────(用户要求修改)────┘
  │ 汇总展示所有信息 → 用户确认或修改 → 锁定
  ▼
[进入 Phase 4]
```

**关键设计：**

| 机制 | 说明 |
|------|------|
| **约束冲突自动检测** | 如"宠物+包间"冲突，自动提供 A/B/C 三种消解方案 |
| **早停 (Early Exit)** | 任一模块约束无法满足，立即 `status: constraint_failed` |
| **可回退修改** | 模块四确认时可回到任何模块修改，修改后重新检测冲突 |
| **结构化输出** | 产出含 `sections` 字段的 JSON，数组顺序即对话推进顺序 |
| **默认值体系** | 评分≥4.0、人均≤200、排队≤30min 等合理默认值 |

输出 JSON 示例（完整版见 [skills/interactive-task-food/SKILL.md](skills/interactive-task-food/SKILL.md) 3.2 节）：

```json
{
  "task_id": "task_20260803_190000",
  "task_type": "food-finding",
  "status": "ready_to_dispatch",
  "sections": [
    { "name": "basic_info",           "status": "completed", "items": [...] },
    { "name": "constraint_resolution", "status": "completed", "items": [...] },
    { "name": "value_added_services",  "status": "completed", "items": [...] },
    { "name": "global_validation",     "status": "completed", "items": [] }
  ],
  "interaction_objects": [{ "object_id": "...", "name": "海底捞火锅", ... }]
}
```

### C4 Phase 4: 人机交互外呼

Phase 4 由 **hermes-nexus（本项目）** 实现。LLM 扮演公司行政人员，通过多轮对话模拟打电话给餐厅确认预定。

**架构流程：**

```
Phrase 3 JSON (sections + interaction_objects[0])
         │
         ▼
  ┌─────────────────┐
  │ build_prompt.py  │  sections → system_prompt
  │ 角色: 望京恒电    │  LLM 扮演行政人员
  │ 公司行政人员      │  餐厅信息注入 prompt
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │   chat.py        │  ChatSession 编排多轮对话
  │ LLM ↔ Channel    │  检测 [CONVERSATION_COMPLETE]
  └────────┬────────┘
           │
           ▼
  ┌─────────────────┐
  │  channel.py      │  TerminalChannel 通信渠道
  │ 真实终端/mock    │  mock 模式用预设脚本自动测试
  └────────┬────────┘
           │
           ▼
  { task_id, messages: [{role, content}], status }
```

**对话示例（mock 模式）：**

```
📞 客户: 喂～你好，请问是海底捞火锅望京SOHO店吗？
        我想预定一下明天的团建聚餐，方便聊两句吗？

🍽️ 餐厅: 您好，可以的，请问几位用餐？什么时间呢？

📞 客户: 明天晚上7点，4个人。我们有个3岁的小朋友，
        想问下有没有儿童座椅？另外想要个包间。

🍽️ 餐厅: 包间有的，儿童座椅也提供。包间最低消费800元。

📞 客户: 没问题。另外我们开车来，车牌京A12345，
        能帮登记一下停车吗？

🍽️ 餐厅: 好的，已登记。明晚7点，4位，包间，儿童座椅一套。

📞 客户: 对对，就这样。那明天见，谢谢您啊！
        [CONVERSATION_COMPLETE]
```

**关键设计：**

- `[CONVERSATION_COMPLETE]` 标记：LLM 确认预定成功后自动插入，ChatSession 检测到后结束对话
- 无 LLM API Key 时自动 fallback 到 mock LLM，返回预设的行政人员风格回复
- 支持 SSE 流式 (`/api/v1/chat/stream`)
- system_prompt 模板定义在 `src/build_prompt.py` 的 `SYSTEM_PROMPT_TEMPLATE` 中

### C5 Phase 5: 结果整理与决策

Phrase 5 将 Phrase 4 的通话结果与用户原始需求逐项对比：

```
✅ 已确认:                     ⚠️ 未满足:
  - 包间可用（低消 ¥800）         - （无，本次全部满足）
  - 儿童座椅已预留
  - 停车已登记（京A12345）
  - 时间/人数确认无误
```

**用户决策闭环（四种路径）：**

| 决策 | 行为 |
|------|------|
| `accept` | 完成预定，结束流程 |
| `decline` | 放弃本次预定 |
| `modify_constraints` | 回到 Phase 3 模块二，修改约束后重新来 |
| `try_alternative` | 用 `interaction_objects[1]` 下一家餐厅重新执行 Phase 4 |

### C6 端到端示例

**用户说：** "帮我在北京望京SOHO附近找火锅，4个人带3岁小孩，明天晚上7点，要包间，排队不超过20分钟"

```
Phase 1 (信息输入)
  └─ 意图: Action=find+reserve, Object=火锅,
     Constraints={望京SOHO, 4人, 3岁小孩, 包间, ≤20min}

Phase 3 (对话式需求采集)
  ├─ 模块一: 火锅/4人/明晚7点/望京SOHO/评分≥4.0/人均≤200 ✓
  ├─ 模块二: has_children=3岁/need_private_room=true/max_queue=20min
  │   └─ 冲突检测: 小孩+包间 → 无冲突，优先儿童座椅包间
  ├─ 模块三: need_parking=true/儿童餐具1套/确认低消
  ├─ 模块四: 汇总确认 → 用户确认 → 锁定
  └─ status: ready_to_dispatch

Phase 2 (POI 搜索)
  └─ resolve_restaurants(place_name="望京SOHO", cuisine="火锅",
       area="北京", min_rating=4.0, max_cost=200)
  └─ → [{海底捞火锅(望京SOHO店), 评分4.5, 人均¥150, 有包间, 停车位}]

Phase 4 (外呼确认)
  └─ POST /api/v1/chat → LLM 打电话给海底捞
  └─ 确认: 时间/人数/包间/儿童座椅/停车/低消 → completed

Phase 5 (结果整理)
  └─ 全部满足 ✓ → 用户 accept → 🎉 预定成功！
```

---

## Part D: 源码分析索引

> 基于 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) v0.19.0 的 23 篇深度源码分析。详见 [Hermes_Summary.md](Hermes_Summary.md)。

| Part | 篇章 | 文档数 |
|------|------|--------|
| Part 1 | 全局架构 | [01](docs/01-项目全景.md) - [04](docs/04-状态持久化.md)（4 篇） |
| Part 2 | AI 核心 | [05](docs/05-对话循环.md) - [10](docs/10-错误处理与韧性.md)（6 篇） |
| Part 3 | 工具系统 | [11](docs/11-工具注册表设计.md) - [15](docs/15-MCP-协议实现.md)（5 篇） |
| Part 4 | 智能体系统 | [16](docs/16-子智能体委托.md) - [19](docs/19-安全体系.md)（4 篇） |
| Part 5 | 平台与基础设施 | [20](docs/20-CLI-交互界面.md) - [23](docs/23-批处理与轨迹基础设施.md)（4 篇） |

---

## 项目结构

```
hermes-nexus/
├── main.py                     # FastAPI 服务入口 (REST + SSE streaming)
├── run_demo.py                 # 体验脚本（交互/mock/API 三种模式）
├── README.md                   # 本文件 — 项目总览
├── Hermes_Summary.md           # Hermes Agent 源码分析索引
├── 需求描述.md                  # 原始需求说明
│
├── src/                        # 核心源码
│   ├── build_prompt.py         #   System Prompt 组装引擎
│   ├── chat.py                 #   ChatSession 多轮对话编排器
│   └── channel.py              #   TerminalChannel 通信渠道
│
├── skills/                     # Hermes Skill 定义
│   ├── chinese-poi-search/     #   POI 搜索技能（高德 API）
│   │   ├── SKILL.md
│   │   ├── scripts/amap_poi_tool.py
│   │   └── references/ (x5)
│   └── interactive-task-food/  #   餐馆预定领域技能（5 阶段流水线）
│       └── SKILL.md
│
├── docs/                       # Hermes Agent 源码分析（23 篇深度文档）
│   ├── 00-目录与阅读引导.md
│   └── 01~23-各章节分析.md
│
└── test/                       # 测试用例
```

## 相关资源

- **Hermes Agent 源码**: [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)
- **参考上游**: [luyao618/Claude-Code-Source-Study](https://github.com/luyao618/Claude-Code-Source-Study)
- **高德开放平台**: https://lbs.amap.com
- **DeepSeek API**: https://platform.deepseek.com
