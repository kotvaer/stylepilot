# StylePilot 产品与技术设计

状态：M0 / M1 complete，M2 safety slice complete，M3 Style Profile slice in progress
更新时间：2026-08-17

## 1. 产品定义

StylePilot 是一个运行在 Lightroom Classic 工作流内的场景感知调色
Agent。它从一组已调色参考作品中拆解摄影风格，判断当前原片是否具备
迁移条件，在技术安全约束下生成非破坏性 Lightroom Develop 参数，并以
Lightroom 的实际渲染结果完成“执行—观察—评价—修正”闭环。

它不是：

- 独立 RAW 编辑器；
- 图片生成或像素重绘模型；
- 固定参数 Preset 复制器；
- 自动 Photoshop、人像精修或构图工具；
- 用 LLM 随机猜测 Lightroom 滑块的聊天外壳。

## 2. 目标用户与核心价值

### 2.1 目标用户

- 希望理解摄影风格构成的摄影新手；
- 有参考作品，但不知道原片是否适合该风格的 Lightroom 用户；
- 希望快速获得可解释起点、再进行人工精修的摄影爱好者；
- 希望逐步沉淀个人调色偏好的摄影师。

### 2.2 核心价值

1. **理解风格**：把“日系、电影感、通透”等模糊描述拆成影调、色彩、
   语义区域和质感特征。
2. **判断条件**：在套用前解释曝光、光线、动态范围、肤色和噪声风险。
3. **安全执行**：只在虚拟副本上应用经过范围校验的全局 Develop 参数。
4. **闭环评价**：以 Lightroom 的真实输出而非模型臆测作为最终测量对象。
5. **保留主导权**：用户始终在 Lightroom 中观察、接受、撤销或继续修改。

## 3. 产品形态

产品只有两个运行时组件：

```text
Lightroom Classic
└── StylePilot Lua plugin
    ├── 浮动面板
    ├── 当前选片与元数据
    ├── 虚拟副本与 Develop 操作
    └── Lightroom 渲染预览
          ↕ localhost authenticated socket
Python StylePilot runtime
├── LangGraph workflow
├── 色彩与图像分析
├── VLM provider adapters
├── 适配度与安全规则
├── 参数预测与优化
└── SQLite state and feedback
```

不引入独立桌面端和 Web 后端。Lightroom Classic 是唯一照片浏览、A/B
对比、人工调色和 RAW 渲染界面。Python Runtime 是常驻本地 Sidecar，
不是远程服务。

## 4. 用户流程

### 4.1 创建 Style Profile

1. 用户在 Lightroom 选择 5～20 张已经调色的参考作品。
2. 在浮动面板点击“创建风格画像”并命名。
3. 插件请求 Lightroom 生成统一规格的分析预览。
4. Runtime 计算全局及语义区域特征，并由 VLM 生成摄影语言解释。
5. 保存风格中心、方差、适用场景、风险条件和模型版本。

单张图片只能产生低置信度的 `SingleImageStyleAnalysis`，不能声称代表一名
摄影师的稳定风格。

### 4.2 分析原片适配度

1. 用户选择一张 RAW 和一个 Style Profile。
2. Runtime 获取 Lightroom 当前渲染预览、EXIF 和 Develop Settings。
3. 计算影调、色彩、区域、噪声及裁切风险。
4. 输出 0～100 适配度、置信度、原因和建议风格强度。
5. 不可安全迁移时停止；边界案例由用户选择降低强度或继续。

### 4.3 创建预览副本

1. Agent 将用户意图转换为目标风格约束，而非直接生成参数。
2. 参数规划器和优化器产生候选 Develop Settings。
3. 双重 Guardrail 校验参数名、范围、Process Version 和操作目标。
4. 插件创建虚拟副本并应用候选参数。
5. Lightroom 重新渲染分析预览。
6. Runtime 测量技术安全性和风格距离；最多自动修正两轮。

### 4.4 反馈

用户可接受、降低强度、重新规划或直接在 Develop 面板继续修改。第一版
仅记录 Agent 参数与最终参数差值；拥有足够样本后再训练个性化偏好模型。

## 5. MVP 范围

### 5.1 支持

- 参考作品的影调、色彩、语义区域和适用条件拆解；
- 当前 RAW 的风格适配度与风险解释；
- Temperature、Tint、Exposure、Contrast、Highlights、Shadows、Whites、
  Blacks、Tone Curve、HSL、Vibrance、Saturation、Color Grading、Texture、
  Clarity、Dehaze、Grain 和暗角等全局调整；
- 虚拟副本、Snapshot、回滚和执行历史；
- Lightroom 渲染后的技术评价与最多两轮修正；
- GPT 与国内 Qwen Provider；
- 用户接受、拒绝和人工修正差值记录。

### 5.2 不支持

- 自动裁切、构图和镜头选择；
- 生成式移除、扩图或像素重绘；
- 液化、磨皮和商业人像精修；
- 第一版中的复杂 AI Mask；
- “任意原片精确复刻任意风格”的承诺；
- 以摄影师滑块参数作为唯一 Ground Truth。

## 6. Agent 工作流

LangGraph 管理显式、有边界的状态图：

```text
START
  → load_selection
  → load_metadata
  → render_source_preview
  → analyze_image
  → evaluate_suitability
      ├─ unsuitable → explain_and_stop
      └─ suitable
          → plan_target
          → optimize_settings
          → validate_settings
          → require_write_approval
          → create_virtual_copy
          → create_recovery_snapshot
          → apply_settings
          → render_result_preview
          → evaluate_result
              ├─ success → END
              ├─ safety failure → restore_snapshot → END
              ├─ refinable and iteration < 2 → refine (future)
              └─ needs_user → interrupt → resume/END
```

LLM/VLM 负责场景语义、用户意图、风格解释和边界决策；确定性代码负责
色彩数值、参数范围、目标函数、执行与停止条件。不可逆或批量动作必须经过
Human-in-the-loop。

## 7. 代码模块

```text
src/stylepilot/
├── domain/          # 不依赖基础设施的业务模型与约束
├── application/     # LangGraph、用例和端口协议
├── services/        # 图像分析、适配度、规划与后续优化
├── adapters/        # Lightroom、模型、存储等外部适配器
└── cli.py           # 本地开发与诊断入口
```

依赖方向：

```text
adapters ─┐
services ─┼→ application → domain
cli ──────┘
```

Domain 不依赖 LangGraph、Lightroom SDK、模型 SDK 或数据库。

## 8. Lightroom Bridge

插件基于 `Automaat/lightroom-mcp` 的 MIT 授权代码 fork，保留许可证和
第三方声明。复用 Socket 生命周期、Token、日志、照片解析、Catalog 锁和
已有 handler 测试；新增：

- `render_analysis_preview`
- `create_virtual_copy`
- `create_develop_snapshot`
- `restore_develop_snapshot`
- 参数级范围与 Process Version 校验
- StylePilot 浮动面板
- 插件到 Runtime 的事件消息

Python 端首先可通过现有 MCP Server 完成 POC；正式运行时实现相同的双
Socket 协议并直接连接 Lua 插件，避免 Node 成为必要的第三个进程。Python
Runtime 可在后期额外暴露 MCP Server，但内部工作流不依赖 MCP。

## 9. 色彩与图像分析

分析以 Lightroom 输出并附带色彩配置的预览为准，不在 Python 中重新解码
RAW。快速闭环使用 2048px sRGB JPEG；离线评测可以使用 16-bit TIFF。

基础特征：

- L* 分位数、动态范围和黑白场裁切；
- Lab/LCh 色度、色相分布、冷暖比例和中性色偏；
- 主色聚类与基于感知距离的分布距离；
- 肤色、天空、植被等语义区域独立统计；
- 暗部占比、ISO、提亮幅度和噪声风险；
- 清晰度、局部对比度和颗粒特征。

ICC 转换由 LittleCMS 完成；`colour-science`、NumPy、OpenCV 和
scikit-image 提供计算能力。任何跨图片比较必须先进入统一颜色表示。

## 10. 模型策略

- 海外默认：GPT-5.6 Terra；复杂离线 Style Profile 可升级到 Sol。
- 高频场景门禁默认使用支持图像输入和 Structured Outputs 的 GPT-5.6 Luna；
  复杂风格语言解释再升级到 Terra。
- 国内默认：Qwen3.7 Plus；稳定后用 Qwen3.7 Flash 降低成本。
- Provider 通过端口协议隔离，业务模型不出现厂商响应对象。
- 云端只接收移除 EXIF 的缩小预览和必要指标，RAW 不上传。
- 精确 Lightroom 参数不由 VLM 单独决定。

## 11. 数据与安全

- localhost Socket 只绑定 `127.0.0.1`；
- 每条请求携带随机 Token、请求 ID 和超时；
- Lua 与 Python 双端执行参数 allowlist 和范围校验；
- 原片永不直接修改，默认目标必须是虚拟副本；
- 写入前生成原生 Develop Snapshot，写入后以 Lightroom 重渲染结果执行
  裁切率和客观风格距离后置验证，失败自动恢复；
- `apply` 意图与显式写入授权分离；
- API Key 存储在系统凭据管理或受限配置中，不写入日志；
- SQLite 保存 Style Profile、指标、模型/Prompt 版本、执行轨迹和反馈；
- 图片只保存受生命周期管理的临时路径。

## 12. 测试策略

### 12.1 单元测试

- Domain 验证与参数边界；
- 色彩转换和指标不变量；
- 适配度评分及原因；
- 参数规划与强度缩放；
- LangGraph 的未授权停止、验证通过和验证失败回滚分支。

### 12.2 契约测试

- Python Bridge 与 Lua handler 的 JSON Schema；
- 请求 ID、认证、超时、重连与幂等；
- Lightroom Process Version 参数映射。

### 12.3 Golden Image 测试

固定测试图的特征结果保存在版本库中，仅在明确算法变更时更新。允许浮点
容差，但不允许指标无解释漂移。

### 12.4 集成与 E2E

- Fake Bridge 覆盖 CI 中的完整工作流；
- Lightroom E2E 使用独立测试 Catalog 和可恢复虚拟副本；
- 云模型测试使用录制响应，定期运行小规模在线回归集。

质量门禁：

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest --cov=stylepilot --cov-branch
```

## 13. 评价指标

不以 Agent 参数和摄影师参数的距离为主要指标。评价分三层：

1. **技术安全**：裁切、色域、肤色和噪声风险违规率；
2. **风格接近**：全局及语义区域风格距离的下降比例；
3. **使用价值**：适配度校准、A/B 接受率、人工修正幅度和总耗时。

每次实验固定模型版本、Prompt 版本、算法版本和 Lightroom 版本。

## 14. 里程碑

### M0：可测试骨架

- uv/ruff/ty 工程；
- Domain、Bridge 端口、Fake Bridge；
- LangGraph 分支：分析、拒绝、规划、虚拟副本应用；
- 基础图片统计和自动化测试。

### M1：真实 Lightroom POC

- [x] 官方 Python MCP Client 与固定版本 `lightroom-mcp 0.9.0`；
- [x] 读取选片、元数据并导出 Lightroom 实际渲染预览；
- [x] 连接诊断和只读单图分析命令；
- [x] 在本机 Lightroom 完成“选片—导出—分析—规划”真实只读 E2E；
- [x] 明确安全应用链路移至 M2：上游 0.9.0 缺少创建虚拟副本工具，因此不允许回退到原片写入。

### M2：StylePilot 插件 fork

- [x] 建立带上游归属和 MIT 许可证的 `kotvaer/lightroom-mcp` fork；
- [x] 新增 `create_virtual_copy`，返回新副本 catalog ID；
- [x] Python Bridge 只授权本会话创建的虚拟副本作为写入目标；
- [x] 完成真实 Lightroom E2E：分析原片、创建副本、仅对副本应用参数；
- [x] 新增原生 Develop Snapshot、异常自动回滚和跨进程显式恢复命令；
- [x] Lua 二次确认 `isVirtualCopy`，并对 StylePilot 参数执行严格类型与范围校验；
- [x] 真实 Lightroom 回滚 E2E：写入副本后按 Snapshot 恢复并核对参数；
- [x] 写入后由 Lightroom 重新渲染，并验证裁切率与客观风格距离；
- [x] 后置条件失败自动恢复 Snapshot，并完成真实 pass / rollback 双路径 E2E；
- [x] `apply` 与显式领域写入授权分离，防止库调用者意外落盘；
- [x] 在 Lightroom 原生浮动面板中展示照片、风格、适配度、参数与风险；
- [x] 以 request ID 绑定批准/拒绝决定，CLI 申请应用不再等同于写入授权；
- [ ] 评估从 MCP stdio 迁移为 Python 直接 Socket 协议的收益。

### M3：Style Profile 与模型

- [x] 5～20 张参考图的多参考图风格画像；
- [x] 统一降采样后的 L* 中位数、P90–P10 对比度、Lab 色度与 a*/b*、
  黑白场裁切占比；
- [x] 中位数 / MAD 稳健中心、离散度和仅供人工复核的离群候选；
- [x] Style Profile JSON 构建、保存与 Lightroom 只读分析加载；
- [x] 富特征参与适配度与 Lightroom 重渲染后风格距离；
- [x] SceneAnalyzer Provider 端口、受限场景 taxonomy 与 Pydantic 结构化输出；
- [x] OpenAI Responses 图片输入 adapter，上传前移除 EXIF 并缩小预览；
- [x] Provider 失败、低置信度和场景不匹配时在规划前 fail closed；
- [x] 显式 preferred scene 与只用于评测的 source scene override；
- [x] Qwen OpenAI-compatible Chat Completions 配置与结构化输出 adapter；
- [ ] Qwen / DashScope 可选真实 Key 契约回归测试；
- [ ] 使用真实 API Key 的小规模在线 VLM 回归集；
- [ ] 人像、天空、植被、水面等语义区域分析；
- [x] 参考集共享场景聚合与场景不匹配拒绝；
- [ ] 混合参考集的场景子风格自动聚类；
- [ ] 评测集和模型路由。

首轮真实实验使用三组 Instagram 摄影作品构建本地、Git 忽略的参考集。
全局特征能够区分高调低色度人像与风光风格，并检测拼贴画布、暖色子风格
等候选异常；但 Dearie Studio 人像画像仍对一张西湖暮色风光给出较高适配
分。这验证了全局色彩距离不能代替场景语义，语义区域与场景拒绝是下一
个阻塞正式自动应用的 M3 任务。加入语义门禁后，同一张西湖照片对 Dearie
Studio 人像画像得到 30.70 分并在规划前拒绝；对 Jannik Obenhoff 风光画像
得到 83.64 分并正常进入只读规划。该 A/B 使用显式 scene override 验证本地
门禁。后续使用真实千问 Key 完成 Dearie Studio 六张参考图在线构建，并在
同一张西湖照片上得到 landscape 主场景与 portrait 风格不匹配的拒绝结果；
正式、可重复的小规模在线回归集仍未完成。

### M3.5：Lightroom 执行器与评测基准

- [x] 分离执行正确性、语义适配、安全门禁、风格移动和产品效率指标；
- [x] 单参数探针领域报告与 11 个现有安全参数 CLI 入口；
- [x] 原生批准、虚拟副本、Snapshot、写入读回、三次渲染测量与自动恢复；
- [x] Contrast、Whites、Blacks 仅在存在可测目标时进入规则规划；
- [ ] 在真实 Lightroom 上测量导出噪声与各参数响应；
- [ ] 多照片、多参数采样清单和聚合报告；
- [ ] 白平衡与 HSL 的类型化 Python / TypeScript / Lua 安全协议；
- [ ] 留一参考图风格距离评测。

### M4：参数代理与个性化

- 自动采样 Lightroom 黑盒响应数据；
- LightGBM 代理模型与受约束优化；
- 用户反馈和个人偏好学习。
