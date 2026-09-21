# LLM Wiki-qa

**AI 自动维护的个人知识库 —— 丢进文档，wiki 自己长出来。**


把散落的 PDF、Word、笔记丢进一个文件夹，LLM Wiki-qa 会自动把它们蒸馏成一座互相链接的 Markdown 维基：概念页、实体页、来源摘要页，外加自动维护的总览页。之后你可以直接在网页里向自己的知识库提问，也可以使用智能体通过 MCP 读写这座 wiki。

灵感来自 [Andrej Karpathy 的 LLM Wiki 构想](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)：完全本地运行、上传即自动构建，不依赖任何云端服务。

## 特性

- **上传即自动构建** —— 把 PDF、Word、Markdown 等文档丢进工作区就不用管了：后台自动分析内容，蒸馏出概念页、实体页、来源摘要页，并持续维护一份总览
- **会话式问答** —— 在网页里直接向自己的知识库提问，支持多会话管理和上下文记忆，回答附带来源引用
- **语义检索** —— 除了关键词匹配，还能按意思找到相关内容；可完全离线运行，不配置就自动用关键词检索
- **原生交叉链接** —— wiki 页面之间、页面与来源文档之间自动互相链接，并配有图表查看器浏览概念与实体之间的关系
- **Chrome 扩展** —— 阅读网页和 PDF 时剪藏内容、高亮关键段落、留下评论，智能体可以通过 MCP 看到这些标注
- **MCP 连接** —— 通过 MCP 接入 Claude、Codex 或任何兼容 MCP 的智能体，让它们直接搜索、阅读和编辑你的 wiki
- **本地优先** —— wiki 就是普通的 Markdown 文件，可以用任何编辑器打开、可以提交到 git；服务只监听本机，文档不出你的电脑
- **文件监视** —— 直接把文件拖进文件夹就会自动索引、自动构建；在编辑器里手改 wiki 页面也会实时同步
- **进度可见** —— 上传、解析、生成 wiki 的每一步都有实时进度，失败会告诉你具体原因

## 快速开始

**环境要求：** Python 3.11+，Node.js 20+。可选：安装 [LibreOffice](https://www.libreoffice.org/) 用于解析 Word / PowerPoint 文件。

**1. 安装依赖**

```bash
git clone <你的仓库地址>
cd llmwiki
python -m venv .venv && source .venv/bin/activate
pip install -r api/requirements.txt -r mcp/requirements.txt
cd web && npm install && cd ..
```

**2. 配置 LLM**

复制 `.env.example` 为 `.env`，填入你的模型服务（任选兼容 OpenAI 或 Anthropic 协议的端点）：

```bash
LLM_BASE_URL=https://your-llm-endpoint.example.com/v1
LLM_API_KEY=sk-your-api-key
LLM_MODEL=your-model-name
LLM_PROTOCOL=openai        # 或 anthropic
INGESTION_ENABLED=true     # 打开自动摄入
```

不配 LLM 也能用 —— 索引、搜索、浏览都正常，只是没有自动构建和问答。

**（可选）向量语义检索**：在 `.env` 追加 embeddings 配置，已有工作区再跑一次 `./llmwiki embed <工作区>` 回填向量即可。嵌入后端二选一：

```bash
# 方式一：API（独立于 LLM 配置；SiliconFlow 的 BGE 模型有免费额度）
EMBEDDING_BACKEND=api
EMBEDDING_BASE_URL=https://api.siliconflow.cn/v1
EMBEDDING_API_KEY=sk-your-api-key
EMBEDDING_MODEL=your-embedding_model

# 方式二：本地模型（pip install fastembed，完全离线，无需任何 API 配置）
EMBEDDING_BACKEND=local

VECTOR_BACKEND=qdrant   # 本地嵌入模式（零服务）；换 qdrant-server + QDRANT_URL 用 Docker 服务
```

向量库需要 `pip install qdrant-client`。

**3. 指向你的文档文件夹**

```bash
./llmwiki open ~/research
```

这会初始化工作区、建立索引，并启动 API（127.0.0.1:8000）和网页（localhost:3000）。文件夹可以是已有的资料目录 —— 它**不会移动或修改你的文件**，只会新增 `wiki/`（生成的页面）和 `.llmwiki/`（索引缓存，删了可以重建）。

**4.（可选）接入智能体（MCP）**

```bash
./llmwiki mcp ~/research
```

这会输出一段 MCP 服务器配置 JSON，把它配进任何支持 MCP 的客户端（Claude Desktop / Claude Code、Cursor、Cherry Studio 等），智能体就能直接搜索和编辑你的 wiki。

## 使用方式

**上传文档**：网页里拖文件上传，或直接把文件丢进工作区文件夹。右下角面板会显示完整进度：上传中 → 解析中 → 生成 wiki 中 → 完成，失败会标明原因。

**问答**：侧边栏进入「问答」页，左侧管理多个会话（新建 / 重命名 / 删除），右侧对话；回答附来源引用，支持快速 / 深度两种模式，刷新后会话不丢。

**支持的格式**：

| 类型 | 格式 | 处理方式 |
|------|------|----------|
| PDF | `.pdf` | 本地提取文本和图表 |
| Office | `.docx` `.doc` `.pptx` `.ppt` | 经 LibreOffice 转换后提取（需安装） |
| 表格 | `.xlsx` `.xls` | 逐 sheet 提取 |
| 网页 | `.html` `.htm` | 清洗为可读 Markdown |
| 文本数据 | `.md` `.txt` `.csv` `.json` `.xml` `.yaml` 等 | 直接索引 |
| 图片 | `.png` `.jpg` `.webp` `.gif` | 存储并可在页面中查看 |

## 目录结构

```
~/research/                  # 你的文件，原封不动
  papers/paper.pdf
  notes.md
  wiki/                      # LLM Wiki-qa 生成的页面
    overview.md              # 自动维护的总览
    sources/                 # 每篇文档的摘要页
    concepts/                # 概念页
    entities/                # 实体页
  .llmwiki/                  # 索引 + 缓存（隐藏，可安全删除）
    index.db
    cache/
```

`wiki/` 就是普通 Markdown —— 可以用任何编辑器打开、可以提交到 git、可以手写修改，文件监视器会自动同步索引。

## 开发

```bash
# 后端测试
PYTHONPATH=api pytest tests/unit/ -v

# MCP 测试
cd mcp && pytest ../tests/unit/mcp/ -v

# 前端
cd web && npm run check && npm test

# Lint
ruff check .
```

## License

本项目基于 Apache License 2.0 开源，详见 [LICENSE](LICENSE)。