# LLM Wiki 项目研究文档

> 研究对象：`llmwiki-master`（本地部署版）
> 研究日期：2026-09-17
> 研究方式：源码精读 + 本地全链路运行验证（上传 246 页 PDF → 解析 → 索引 → MCP 构建 wiki）

---

## 摘要

LLM Wiki 是一个受 Karpathy "LLM Wiki" 概念启发的**个人知识工作区**：用户丢入原始资料（PDF、网页剪藏、笔记、图片），LLM 通过 MCP 协议读取这些资料，**增量地**撰写并维护一个持久的、互相链接的 Markdown wiki。它与 RAG 共享同一套检索底座（切块 + 全文索引），但产出物截然不同——RAG 给的是一次性答案，LLM Wiki 沉淀的是可累积、可引用、可演化的知识资产。

系统的核心设计哲学可以概括为三句话：

1. **LLM 是大脑，系统是躯体**——模型外置、可插拔（Claude / Kimi / 任何 MCP 客户端），仓库内没有任何模型调用代码
2. **文件系统是真相，索引是衍生**——本地模式下 `.md` 文件是权威数据，SQLite 只是可删可重建的索引
3. **提示词即工具**——不写死 system prompt，500+ 行写作规范通过 `guide` 工具按需下发

---

## 1. 项目背景与概念渊源

### 1.1 Karpathy 的 LLM Wiki 概念

传统知识工具（Notion、Obsidian）假设"人写、机器存"；RAG 类方案假设"机器检索、即时回答、用完即弃"。LLM Wiki 提出第三种范式：**LLM 持续阅读原始资料，把精华蒸馏进一个结构化的 wiki，wiki 随时间复利生长**。

关键区别在于"沉淀"：

| 维度 | RAG 问答 | LLM Wiki |
|---|---|---|
| 产出 | 一次性回答 | 持久 wiki 页面 |
| 知识形态 | 散落在语料里，每次重新检索 | 被蒸馏、互链、可引用 |
| 第二次问同样的问题 | 重新检索推理 | 直接读已写好的页面 |
| 新资料进来 | 只是索引变多 | 触发相关页面更新，知识重组 |
| 可审计性 | 答案无出处保证 | 每个事实带脚注页码 |

### 1.2 本项目的定位

`llmwiki-master` 是这一概念的完整工程实现，三种客户端、两个入口服务、一个存储抽象：

```
Claude（任何 MCP 客户端）──MCP──►  mcp/ ──┐
Web 应用 / Chrome 扩展 ────HTTP──►  api/ ──┼──► VaultFS ──┬─ local:  SQLite + 文件系统
                                    │       │              └─ hosted: Postgres(Supabase) + S3
                     api/ ──► converter/  （PDF/Office 重度解析微服务）
```

---

## 2. 总体架构

### 2.1 双模式设计（MODE 环境变量）

| | local（默认） | hosted |
|---|---|---|
| 数据真相 | 工作区文件夹里的真实 `.md` / 原始文件 | Postgres 行 |
| 索引 | `.llmwiki/index.db`（SQLite + FTS5） | Postgres（pgroonga 类全文索引） |
| 文件存储 | 文件系统 | S3 |
| 网络 | 仅 127.0.0.1 回环，设计如此 | 多用户，Supabase JWT 认证 |
| PDF 解析 | 本机 opendataloader（JVM） | converter 微服务（强制，禁止降级本地解析） |
| 启动方式 | `./llmwiki open <workspace>` | docker-compose / 云服务 |

**设计亮点**：两种模式下 MCP 工具行为完全一致，因为所有工具只面向 `VaultFS` 抽象接口编程。

### 2.2 核心接缝：VaultFS 抽象

`mcp/vaultfs/base.py` 定义抽象基类，`sqlite.py`（约 731 行）与 `postgres.py`（约 679 行）是两个实现。这是整个系统最重要的架构决策：

- 所有 9 个 MCP 工具（guide / list / search / read / write / edit / delete / lint / comments / reply）只调用 VaultFS 接口
- 任何 vault 语义变更（比如 wiki 页面规则、引用图）只需要改这一层
- 测试可以用同一套契约测试（`tests/integration/mcp/test_vaultfs_contract.py`）同时验证两个实现

### 2.3 源码即真相，索引可重建

本地模式的数据流严格遵守单向派生：

```
工作区文件（.md / .pdf / ...）  ← 权威，可被任何编辑器直接修改
        │  watcher（api/domain/watcher.py）监听带外修改
        ▼
.llmwiki/index.db（documents / pages / chunks / FTS5）
        ▲
        └─ `./llmwiki reindex <workspace>` 可随时全量重建
```

这意味着：用户可以直接用 VS Code 改 wiki 页面，watcher 会自动重新索引；`.llmwiki/` 目录整个删掉也不丢任何知识——它只是缓存和索引。

### 2.4 工作区布局

```
workspace/
├── Datawhale FDE案例100.pdf   ← 源文档（用户上传的原始资料，只读）
├── wiki/                      ← LLM 生成/维护的 wiki 页面
│   ├── overview.md            ← 枢纽页（受删除保护）
│   ├── concepts/              ← 抽象概念页
│   └── entities/              ← 具体实体页
└── .llmwiki/
    ├── index.db               ← SQLite 索引（可删可重建）
    └── cache/                 ← 处理缓存
```

---

## 3. 存储层：SQLite Schema 深读

`shared/sqlite_schema.sql` 共 271 行，设计相当精炼：

### 3.1 表结构

| 表 | 作用 | 关键设计 |
|---|---|---|
| `workspace` | 知识库 | `kind` 字段区分 wiki / course |
| `documents` | 所有文件（源 + wiki 页 + 资产） | `content` 列直接存文本全文；`source_kind` 区分 wiki/source/asset；`UNIQUE(relative_path)` |
| `document_pages` | PDF/Office 分页文本 | 保留页码，支撑脚注级引用 |
| `document_chunks` | 切块 | `content`（FTS 用）与 `source_content`（原始）分离；`annotations_text` 存放用户划线评论的物化文本 |
| `chunks_fts` | FTS5 虚拟表 | porter + unicode61 分词 |
| `document_references` | 引用图 | cites / links_to 边 |
| `knowledge_base_events` | 活动流 | 全部由触发器自动写入，驱动"最近变更"视图和增量更新判断 |

### 3.2 值得注意的设计

- **FTS 同步靠触发器**：`document_chunks` 的 INSERT/DELETE/UPDATE 触发器自动维护 `chunks_fts`，应用层不可能忘记同步
- **活动流也靠触发器**：documents 表上的三个触发器自动记录 page.created / source.added / page.updated 等事件，且精心排除了 asset、隐藏文件和 overview 初始化噪音。LLM 做"增量更新 wiki"时查的就是这个事件表
- **已知缺陷**：FTS5 的 porter/unicode61 分词器对中文按整段处理，**中文短语查询需要连续子串匹配**——搜 "FDE 案例" 无结果，搜连续词可以。这是索引层限制，不是数据问题

---

## 4. 上传与解析流水线

### 4.1 本地模式全流程：入库的 8 个步骤

以实际上传的 246 页 PDF 为例，从上传到可检索共 8 步，全部自动完成：

**步骤 1 · 上传落盘（api/routes 上传端点）**
multipart 接收 → 先写隐藏临时文件 → 原子 rename 进 workspace。**源文件落盘即真相写入**——这一步之后文件已经持久化，后续任何一步失败都不会丢文件。

**步骤 2 · 登记**
`documents` 表插入一行，`status='pending'`。此时网页端已能看到文件。

**步骤 3 · 后台认领（local_processor.py:173）**
后台任务执行 `UPDATE ... SET status='processing' WHERE status='pending'`，靠 SQL 原子性防并发重复处理；信号量限制最多 4 个文档同时解析（local_processor.py:40）。处理中断（如服务重启）的文档由启动时的 `reconcile_workspace`（local_processor.py:310）重置回 pending 补跑。

**步骤 4 · 类型路由（local_processor.py:217-232）**
按扩展名分 5 条路：

| 类型 | 处理器 | parser 标记 |
|---|---|---|
| PDF | opendataloader（默认）或 mistral | `opendataloader` / `mistral` |
| Office（docx/pptx 等） | LibreOffice 转 PDF → opendataloader | `libreoffice+opendataloader` |
| 表格（xlsx 等） | openpyxl 按 sheet 提取 | `openpyxl` |
| 图片 | 不处理，直接 ready | `native` |
| HTML | webmd 解析器提取正文 | `webmd` |
| 简单文本（md/txt/csv…） | 上传时全文已入库，只补切块 | `text` |

**步骤 5 · 解析提取（OCR 决策点）**
- PDF 走 `extract_pdf`（services/pdf_extract.py），调本机 opendataloader 的 JVM 子进程，输出每页 Markdown + 嵌入图片。**本地默认链路没有 OCR**——扫描件提取结果为空或接近空，想处理扫描件只能配 `PDF_BACKEND=mistral` + `MISTRAL_API_KEY` 走云端 Mistral OCR API（local_processor.py:523）
- 图片文件**永不 OCR**：原样存储，`page_count=1` 直接 ready，靠多模态 LLM 在 MCP read 时直接看图
- Office 转换产物缓存到 `.llmwiki/cache/local/<doc_id>/converted.pdf` 供前端预览

**步骤 6 · 分页入库（local_processor.py:419-447）**
每页一行写入 `document_pages`（保留页码，这是脚注 `p.3` 引用的数据基础）；PDF 抽出的嵌入图存为隐藏资产文件并登记 `source_kind='asset'` 行；全文按 `\n\n---\n\n` 拼接后存入 `documents.content`。

**步骤 7 · 切块（见 4.2）**
`chunk_pages` 把分页文本切成 `document_chunks`：512 token / 128 重叠 / 最小 32，带 `header_breadcrumb`。

**步骤 8 · 建索引 + 完成**
chunks 落库后 **FTS5 触发器自动同步** `chunks_fts`（schema 层保证，应用层不会漏）；`documents` 更新为 `status='ready'` 并记录 `parser`。

### 4.1.1 关于清洗：没有这一步，是刻意的

整条流水线**不存在清洗/去水印/去噪环节**——提取出的文本原样进 pages 和 chunks。唯一的例外是 opendataloader 解析时按元素类型跳过页眉页脚，那是解析器行为而非清洗规则。

设计判断：清洗规则永远追不上噪声的花样，而下游消费者不是人而是 LLM——它读完带噪文本后在**写作 wiki 页面时蒸馏**，水印、页码、乱码在这一步被自然忽略。代价是真实存在的：重度污染的文档会拉低生成页面的质量（见 8.2 局限表）。也没有给用户暴露清洗规则的配置入口。

### 4.2 切块策略（api/services/chunker.py）

- `CHUNK_SIZE = 512` token、`CHUNK_OVERLAP = 128`、`MIN_CHUNK_TOKENS = 32`
- token 估算为 `len(text) // 4`（粗估，换取零依赖）
- 句子边界识别包含中文标点 `。！？`
- **头部面包屑**：每个 chunk 携带 `header_breadcrumb`（如 "第3章 > 3.2 方法论"），检索结果自带章节上下文

### 4.3 PDF 解析后端

| 后端 | 配置 | 能力 | 限制 |
|---|---|---|---|
| opendataloader（默认） | 无 | 本地 JVM，提取文字/表格/嵌入图 | **扫描件无 OCR**；JSON 输出有 `MAX_EXTRACT_JSON_BYTES`（默认 64 MiB）上限 |
| mistral | `PDF_BACKEND=mistral` + `MISTRAL_API_KEY` | 云端 OCR，能处理扫描件 | 依赖外部 API、成本 |

实践踩过的坑：opendataloader 用 `image_output='embedded'` 时，图片 base64 内嵌导致 JSON 急剧膨胀——一份 13 MB / 246 页的 PDF 产出 77.4 MiB JSON，超过默认 64 MiB 上限直接失败。处理方式：调大上限或换后端。

### 4.4 converter 微服务（hosted 模式专用）

`converter/main.py`（454 行）是独立认证微服务，设计目标是**安全地处理不可信文档**：

- 仅支持 Office + PDF；Office 先经 LibreOffice 转 PDF 再走 opendataloader
- **拒绝无 `CONVERTER_SECRET` 启动**——不存在"公开模式"
- S3 URL 严格白名单：只允许指向配置的 bucket（虚拟主机/路径两种风格都校验）
- 层层资源上限：源文件 200 MB、JSON 64 MiB、元素 10 万、单元素文本 1 MiB、总文本 32 MiB、响应 40 MiB、页数 1 万、并发 2
- 子进程组隔离 + 超时 SIGKILL 整组（防 LibreOffice/JVM 僵尸）
- 所有 JSON 尺寸检查**增量流式**完成，不物化大对象

这是仓库里安全工程密度最高的文件，体现了"解析器输出即不可信输入"的假设。

---

## 5. MCP 层：LLM 的唯一入口

### 5.1 双服务器共享工具

- `mcp/local_server.py`：stdio，一个 workspace = 一个服务器进程
- `mcp/hosted.py`：HTTP + Supabase JWT
- 两者都调用 `tools.register(mcp, get_user_id, fs_factory)`

### 5.2 "提示词即工具"的提示工程架构

仓库中**没有任何面向 wiki 写作的 system prompt**，规范通过三层机制下发：

1. **FastMCP instructions**（`local_server.py:94`）：连接建立时注入一句话——"Call the `guide` tool first"
2. **GUIDE_TEXT**（`mcp/tools/guide.py:5`）：约 530 行的写作手册，是事实上的系统提示词。内容涵盖：
   - wiki 结构契约（overview 枢纽 / concepts 抽象 / entities 具体）
   - frontmatter 四字段必填（title/description/date/tags≥2）
   - **每页至少一个视觉元素**（Mermaid/表格/SVG/KaTeX），含 Mermaid 语法陷阱清单
   - 事实必须带脚注引用（`[^1]: file.pdf, p.3`），UI 渲染为悬停徽章
   - 完整工作流：摄取新源 6 步 / 回答问题 4 步 / lint 维护
   - 课程模式：教学设计的完整规范（锚定案例、机制叙事、迁移练习、quiz 组件契约）
3. **工具 docstring**：每个 MCP 工具自带的描述（如 delete 保护 overview.md）随工具列表注入

**为什么这样设计**：

- 客户端无关——system prompt 归客户端管，工具返回值任何 MCP 客户端都能收到
- 按需加载——500 行规范不常驻上下文
- 版本随代码演进——规范更新，下次会话自动生效

### 5.3 引用图

每次写入自动解析脚注和页面间链接，写入 `document_references`，形成双向图：

- 写入响应即返回"哪些页面引用了本页"，提示 LLM 联动更新
- `search(mode="references", query="uncited")` 找从未被引用的源文档
- `search(mode="references", query="stale")` 找因链接对象更新而可能过时的页面

这让"增量维护"从口号变成可执行的 lint 项。

---

## 6. 前端与扩展

### 6.1 Web（Next.js 16）

- App Router（`web/src/app`），渲染层只负责展示，"Claude 驱动，UI 渲染"
- wiki 页面树按路径层级渲染为可展开树；脚注渲染为悬停 popover；quiz 块渲染为交互组件
- 课程模式是知识库的一个 `kind` 标记，可逆切换，内容不动
- 已知的第三方兼容问题：
  - Next 16 内置 webpack 5.98 + pdfjs-dist 5.4.x 在 eval sourcemap 下崩溃（vercel/next.js#89177），且 dev 模式强制回退用户 devtool 覆盖——解法是用 `dev:turbo`（Turbopack）
  - 必须 `npm ci` 安装依赖——pnpm 会解析出与 lock 文件不一致的传递依赖（实测 tiptap 版本错位导致 500）
- 实测发现并修复的 bug：`WikiContent.tsx` 的 `slugify` 用 `[^\w\s-]` 过滤字符，中文标题全部 slug 成空串，导致目录组件 React key 重复、锚点跳转失效。修复为 `[^\p{L}\p{N}\w\s-]`（Unicode 感知）

### 6.2 Chrome 扩展（WXT）

`extension/src/entrypoints`，网页剪藏入口，`npm run verify:package` 校验发布包。

---

## 7. 实测：一次完整的 wiki 构建

2026-09-17 用本系统对《Datawhale FDE案例100.pdf》（246 页，24 个访谈案例）做了完整验证：

1. 上传 → 解析 → 246 页 / 212 chunks / FTS 就绪（自动，无需干预）
2. 通过 MCP 让 LLM"读取该 PDF，构建 wiki"
3. LLM 自主完成：调 guide 读规范 → list 盘点 → 分四批读完全部 246 页 → 创建 6 个新页面 + 重写 overview → 运行 lint → 修复全部 error 级问题
4. 产出：4 个概念页（FDE 角色 / 方法论 / 人机分工 / 落地障碍）、2 个实体页（案例集索引 / Datawhale）、1 个枢纽 overview，共 80+ 条带页码脚注，页面间交叉链接成网

**观察到的关键行为**：LLM 完全按 GUIDE_TEXT 契约执行——frontmatter、视觉元素、脚注页码、lint 修复，全程只需一句自然语言触发。这验证了"提示词即工具"架构的有效性。

---

## 8. 设计权衡与局限

### 8.1 优点

- **模型可插拔**：LLM 完全外置，换模型（Claude→Kimi→任何 MCP 客户端）零代码改动
- **数据可携带**：本地模式就是普通文件夹，git 可以版本化整个知识库
- **安全边界清晰**：converter 的资源上限、URL 白名单、进程组隔离是教科书级的不可信输入处理
- **可维护性机制内建**：引用图 + lint + 事件流，让"长期维护 wiki"有工具支撑而非靠自觉

### 8.2 局限与风险

| 问题 | 现状 | 影响 |
|---|---|---|
| FTS5 中文分词 | porter/unicode61，短语查询差 | 中文检索召回受限；根本解是换分词器（如 jieba 插件）或引入向量检索 |
| 扫描件 PDF | 默认后端无 OCR | 需配 `PDF_BACKEND=mistral` 或接受失败 |
| 图片内容 | 不 OCR，依赖多模态 LLM 读图 | 文本型模型无法利用图片资料 |
| 无清洗/去水印规则 | 刻意不管 | 噪声靠 LLM 写作时蒸馏容忍；重度污染文档会影响页面质量 |
| 嵌入式检索 | 仅全文索引，无向量 | 语义相似但措辞不同的内容召回弱 |
| 单 workspace/服务器 | local 模式一进程一库 | 多 workspace 需多 MCP 配置 |

### 8.3 与 RAG 的关系

检索层确实是同一套技术（切块、全文索引、按页引用），说"底层是 RAG"没错。差异在**写路径**：RAG 没有写路径，LLM Wiki 的写路径受 guide 契约约束、被引用图连接、被 lint 审计。一句话：**RAG 是只读的知识消费，LLM Wiki 是可累积的知识生产**。

---

## 9. 可能的演进方向

1. **检索增强**：FTS5 + 向量混合检索（schema 已预留 EMBEDDING_MODEL/DIM 配置但未在 local 启用）；中文分词插件
2. **多模态深化**：图片 OCR/描述预生成，让纯文本模型也能利用图像资料
3. **自动化 Routine**：README 已提的定时任务模式（每晚增量更新 wiki）产品化
4. **协作**：hosted 模式的多人共同维护同一 wiki 的冲突语义
5. **评测**：wiki 质量（引用准确率、覆盖率、陈旧率）的量化指标

---

## 10. 结论

LLM Wiki 的最大价值不在于某个单点技术，而在于**架构决策的一致性**：

- 存储上"文件即真相、索引可重建"消除了状态同步类 bug 的整个类别
- 接口上"VaultFS 唯一接缝"让双模式实现成本减半、行为一致
- 提示工程上"规范即工具"让任何模型任何客户端拿到同一份写作契约
- 安全上"解析器输出不可信"贯穿 converter 的每一层上限

它回答了"LLM 应用如何沉淀长期价值"这个问题：不靠更长的上下文，不靠更大的索引，而靠**让 LLM 以受约束的方式持续写作，并把写作成果当作一等公民来存储、链接和审计**。

---

*本文档基于 2026-09-17 的代码版本与实测结果撰写。*
