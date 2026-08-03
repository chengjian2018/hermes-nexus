# Hermes Nexus — 人机交互 Mock 服务

> 基于 Hermes Agent 框架，实现**主动帮人预定餐馆**的端到端流程。
>
> 本项目既是 Hermes Agent 源码的深度解读，也是基于 Hermes Skill 系统构建的领域应用实例。

---

## 目录

- [Part A: 项目全景](#part-a-项目全景)
- [Part B: Hermes Agent 框架解读](#part-b-hermes-agent-框架解读)
- [Part C: 餐馆预定 — Skill 驱动实战](#part-c-餐馆预定--skill-驱动实战)
  - [C7 Skill Generator — 元 Skill 生成器](#c7-skill-generator--元-skill-生成器)
  - [C8 Car Sales — 汽车销售领域 Skill](#c8-car-sales--汽车销售领域-skill)
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
- **多 Skill 生态**：`chinese-poi-search` 负责 POI 搜索，`interactive-task-food` 负责餐馆预定对话编排，`interactive-task-skill-generator` 可生成任意领域的交互 Skill（如 `interactive-task-car-sales`）
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

四个技能位于 `skills/` 目录，包含一个**工具 Skill**、一个**领域 Skill**、一个**元 Skill（Skill 生成器）**以及一个由生成器产出的**汽车销售领域 Skill**：

```
skills/
├── chinese-poi-search/              # POI 搜索技能（Phase 2 的 Resolver）
│   ├── SKILL.md                     #   技能定义（含 20 个实战 pitfall）
│   ├── scripts/
│   │   └── amap_poi_tool.py         #   高德 POI API 封装（33KB，6 个命令）
│   └── references/                  #   5 篇参考文档
│       ├── amap-poi-api.md                # API 端点完整参考
│       ├── cross-skill-validation.md      # 跨技能引用验证
│       ├── late-night-filtering.md         # 夜宵过滤策略
│       ├── compound-name-geocoding.md      # 复合地名地理编码
│       └── hermes-nexus-phrase4.md         # Phrase 4 交互服务文档
│
├── interactive-task-food/           # 餐馆预定领域技能（参考实现）
│   └── SKILL.md                     #   44KB：5 阶段流水线 + 4 模块对话流
│
├── interactive-task-skill-generator/  # 元 Skill：领域 Skill 生成器
│   ├── SKILL.md                     #   5 阶段生成流水线 (G1-G5)
│   ├── templates/
│   │   └── domain-skill-template.md #   目标 Skill 骨架模板
│   └── references/
│       └── domain-heuristics.md     #   6 维度领域分析启发式指南
│
└── interactive-task-car-sales/      # 汽车销售领域技能（由 skill-generator 生成）
    ├── SKILL.md                     #   5 阶段流水线 + 4 模块对话流
    └── scripts/
        └── resolve_car_dealers.py   #   4S 店搜索 Resolver（包装 amap_poi_tool）
```

**技能协作关系：**

```
┌─────────────────────────────────────────────────────────────┐
│                  Skill 生态全景                               │
│                                                             │
│  interactive-task-skill-generator (元 Skill — 生成器)         │
│    │                                                        │
│    │ 5 阶段启发式流水线:                                      │
│    │   G1 领域分析 → G2 对象解析 → G3 模块设计 → G4 生成 → G5 校验  │
│    │                                                        │
│    ├─ 生成 ──→ interactive-task-food (参考实现)               │
│    └─ 生成 ──→ interactive-task-car-sales (汽车销售)           │
│                                                             │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  interactive-task-food / interactive-task-car-sales (领域 Skill)│
│    │                                                        │
│    ├─ Phase 2: 获取可交互对象                                  │
│    │   └─ 委托 → chinese-poi-search (工具 Skill)              │
│    │              ├─ amap_poi_tool.resolve_restaurants()     │
│    │              └─ resolve_car_dealers() (包装 amap_poi_tool)│
│    │                  三种搜索模式: 地点名周边 / 城市区域 / 坐标周边  │
│    │                                                        │
│    ├─ Phase 4: 发起交互                                       │
│    │   └─ 委托 → hermes-nexus (本项目 FastAPI 服务)            │
│    │              └─ POST /api/v1/chat                       │
│    │                                                        │
│    └─ Phase 5: 结果整理                                       │
│        └─ 本地处理 → 逐项对比用户需求 vs 对方回复 → 输出决策建议    │
└─────────────────────────────────────────────────────────────┘
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

### C7 Skill Generator — 元 Skill 生成器

`interactive-task-skill-generator` 是一个**元 Skill（Meta-Skill）**，它不直接处理用户业务，而是**生成其他领域 Skill**。给定一个领域描述（如"医院挂号"、"家政预约"、"汽车销售"），它通过启发式 5 阶段流水线自动生成完整的 `SKILL.md`。

**5 阶段生成流水线：**

```
用户描述领域
    │
    ▼
G1: 领域分析 ──┬── domain_definition (领域/动作/对象类型)
               ├── 6 维度启发式特征提取
               └── 信息按 3 种用途分类
    │
    ▼
G2: 对象解析 ──┬── 确定 resolver 策略 (poi_search / api / user_provided)
               ├── input_mapping (模块字段 → resolver 参数)
               └── output_schema + on_empty/on_multi 策略
    │
    ▼
G3: 模块设计 ──┬── 模块一: 基础信息 (用途1 → 筛选字段)
               ├── 模块二: 约束探测 (用途2 → 约束字段 + 冲突规则)
               ├── 模块三: 领域特色 (增值服务)
               ├── 模块四: 全局校验 (用途3 → 后置判断基准)
               └── 信息用途映射表
    │
    ▼
G4: Skill 生成 ─┬── 加载模板 (templates/domain-skill-template.md)
                ├── 填充 30+ 变量
                ├── 写入 SKILL.md
                └── 按需生成辅助文件 (resolver/references)
    │
    ▼
G5: 校验交付 ──┬── Frontmatter 合法性校验
               ├── 结构一致性检查 (12 项)
               ├── 占位符残留检查
               └── 交付确认
```

**G1: 6 维度领域启发式分析：**

| 维度 | 启发式问题 | 对模块设计的影响 |
|------|-----------|----------------|
| **时间敏感性** | 精确到天/小时/分钟？有时段/截止概念？ | 决定 time 字段类型 (date/datetime/enum) |
| **对象可变性** | 属性实时变化还是固定？信息会过期？ | 决定"容忍度"字段和实时确认需求 |
| **约束复杂度** | 硬约束有哪些？已知冲突模式？隐性约束？ | 决定模块二字段数量和冲突规则数量 |
| **增值服务** | 可选附加服务？需提前预约？额外费用？ | 决定模块三字段列表 |
| **信息不对称** | 哪些信息只有交互对象才知道？ | 决定用途(2)字段和 Phase 4 确认项 |
| **失败兜底** | 不可用时的备选？降级方案？替代渠道？ | 决定 Phase 5 alternatives 和早终止条件 |

**模板与辅助文件：**

| 文件 | 作用 |
|------|------|
| `templates/domain-skill-template.md` | 目标 Skill 骨架模板，含 30+ `{{变量}}` 占位符 |
| `references/domain-heuristics.md` | 6 维度领域分析详细指南，含分级标准和冲突发现方法 |

**关键设计原则：**
- 模块二和模块三的区分：模块二的字段影响对象筛选，模块三的字段不影响选择但需确认
- Phase 4/5 复用策略：默认复用 `hermes-nexus` 和 `chinese-poi-search`，不重复造轮子
- 字段数量控制：模块一 6-8 个、模块二 5-7 个、模块三 4-6 个
- `interactive-task-food` 作为参考实现，生成时做字段映射对照

---

### C8 Car Sales — 汽车销售领域 Skill

`interactive-task-car-sales` 是由 **skill-generator** 生成的汽车销售领域技能。它复用相同的 **5 阶段流水线 + 4 模块对话流**架构，专为 4S 店看车、试驾、询价、购车场景设计。

**领域定制概览：**

```
领域: car-sales (汽车销售)
动作: 看车/试驾/询价/购车
对象: 4S 店 / 汽车经销商
Resolver: resolve_car_dealers (包装 chinese-poi-search)

Phase 3 对话流:
[模块一: 基础信息] → [模块二: 约束消解] → [模块三: 增值服务] → [模块四: 全局校验]
```

**4 模块字段设计：**

| 模块 | 核心字段 | 用途 |
|------|---------|------|
| **模块一: 基础信息** | 品牌、车型偏好、预算范围、城市、地点/区县、能源类型、新车/二手车、用途、最低评分 | Phrase 2 搜索过滤 |
| **模块二: 约束消解** | 试驾需求/时间、限牌城市/牌照类型、贷款/首付、置换/旧车信息、提车时间、颜色偏好 | 需 4S 店确认的约束 |
| **模块三: 增值服务** | 保险/险种、代办上牌、装饰/明细、延保、赠品偏好 | 不影响筛选但需确认 |
| **模块四: 全局校验** | 汇总展示 → 用户确认/修改 → 锁定 | 后置判断基准线 |

**7 条领域冲突检测规则（模块二）：**

| 冲突场景 | 消解方案 |
|---------|---------|
| 限牌城市 + 燃油车 + 需新申请牌照 | A) 转新能源 B) 接受摇号等待 C) 竞价拍牌 |
| 热门新能源品牌 + 1 个月内提车 | A) 接受等车周期 B) 选有现车配置 C) 找其他经销商 |
| 限牌城市 + 纯电 + 非限牌城市 | 提醒非限牌城市新能源牌照优势不显著 |
| 二手车 + 试驾 + 1 周内提车 | 建议放宽至 2 周，或选已完成整备的二手车 |
| 贷款 + 置换 + 高里程旧车 | 提醒置换估价可能偏低，建议增加首付 |
| 10 万以内 + 全款 + 无置换 | 提醒选择面窄，建议考虑贷款扩大范围 |
| 二手车 + 工作日试驾 | 部分二手车商无固定试驾车，建议调整时间 |

**Phase 2 Resolver — `resolve_car_dealers.py`：**

```python
# 包装 chinese-poi-search 的 amap_poi_tool，专为汽车销售场景定制：
# - 自动拼接品牌 + "4S店" 为搜索关键词
# - 二手车/新车使用不同关键词策略
# - 默认搜索半径 5km（4S 店比餐厅分散）
# - 三种搜索模式：地点名周边 / 城市区域 / 坐标周边

from resolve_car_dealers import resolve_car_dealers

# 模式 1: 地点名周边搜索（推荐）
results = resolve_car_dealers(brand="比亚迪", place_name="望京", area="北京")

# 模式 2: 城市区域搜索
results = resolve_car_dealers(brand="丰田", area="上海", district="浦东")

# 模式 3: 坐标周边搜索
results = resolve_car_dealers(brand="宝马", location="121.4752,31.2297", radius=8000)
```

**Phase 4 领域定制（复用 hermes-nexus）：**

| 配置项 | food (参考) | car-sales (本技能) |
|--------|------------|-------------------|
| LLM 角色 | 公司行政人员订团建餐 | 购车客户致电 4S 店销售 |
| 交互对象 | 餐厅前台/经理 | 4S 店销售顾问 |
| 确认内容 | 时间/人数/包间/停车/低消 | 库存/试驾/优惠/贷款/置换/保险 |
| 渠道 | TerminalChannel | TerminalChannel（复用） |

**端到端示例：**

用户说："帮我在北京望京附近找比亚迪 4S 店，想看汉 EV，预算 25 万，周末试驾"

```
Phase 1 (信息输入)
  └─ 意图: Action=看车+试驾, Object=比亚迪4S店,
     Constraints={望京, 汉EV, 25万, 周末试驾}

Phase 3 (对话式需求采集)
  ├─ 模块一: 比亚迪/汉EV/25万/new/bev/通勤/北京/望京 ✓
  ├─ 模块二: need_test_drive=true(本周末)/限牌=true(需新申请)/
  │          贷款=true(首付8万)/无置换/1个月内提车/白色
  │   └─ 冲突检测: 限牌+纯电 → 绿牌免摇号，无冲突 ✓
  ├─ 模块三: 保险(true)/上牌(true)/装饰(true)/延保(false)
  ├─ 模块四: 汇总确认 → 用户确认 → 锁定
  └─ status: ready_to_dispatch

Phase 2 (POI 搜索)
  └─ resolve_car_dealers(brand="比亚迪", place_name="望京", area="北京")
  └─ → [{比亚迪海洋网(望京4S店), 评分4.2, 新车}]

Phase 4 (外呼确认)
  └─ POST /api/v1/chat → LLM 致电 4S 店
  └─ 确认: 现车/试驾/优惠/贷款/保险/提车周期 → completed

Phase 5 (结果整理)
  └─ 全部满足 ✓ → 用户 accept → 🎉 预约到店！
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
├── skills/                              # Hermes Skill 定义
│   ├── chinese-poi-search/              #   POI 搜索技能（高德 API）
│   │   ├── SKILL.md
│   │   ├── scripts/amap_poi_tool.py
│   │   └── references/ (x5)
│   ├── interactive-task-food/           #   餐馆预定领域技能（5 阶段流水线）
│   │   └── SKILL.md
│   ├── interactive-task-skill-generator/ #   元 Skill：领域 Skill 生成器
│   │   ├── SKILL.md
│   │   ├── templates/domain-skill-template.md
│   │   └── references/domain-heuristics.md
│   └── interactive-task-car-sales/      #   汽车销售领域技能（由生成器产出）
│       ├── SKILL.md
│       └── scripts/resolve_car_dealers.py
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
