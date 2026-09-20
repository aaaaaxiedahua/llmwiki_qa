# LLM Wiki

**AI 自动维护的个人知识库 —— 丢进文档，wiki 自己长出来。**

[![License](https://img.shields.io/badge/license-Apache%202.0-green)](LICENSE)

把散落的 PDF、Word、笔记丢进一个文件夹，LLM Wiki 会自动把它们蒸馏成一座互相链接的 Markdown 维基：概念页、实体页、来源摘要页，外加自动维护的总览页。之后你可以直接在网页里向自己的知识库提问，也可以让 Claude 通过 MCP 读写这座 wiki。

灵感来自 [Andrej Karpathy 的 LLM Wiki 构想](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)：完全本地运行、上传即自动构建，不依赖任何云端服务。

## 特性

- **上传即自动构建** —— 新文档进入工作区后，后台队列自动执行两步 LLM 流水线（分析 → 生成），产出 wiki 页面并更新总览，无需任何对话干预
- **知识库问答** —— 网页内聊天面板，基于检索到的文档内容回答，支持 OpenAI 兼容和 Anthropic 两种 API 协议
- **本地优先** —— 文件就是真相：wiki 是普通的 Markdown 文件，索引是可重建的 SQLite；API 只监听 127.0.0.1，你的文档不出本机
- **MCP 工具** —— Claude（Desktop / Code / 任何 MCP 客户端）可以搜索、阅读、编辑你的 wiki
- **文件监视** —— 直接把文件拖进文件夹也会自动索引、自动摄入；在编辑器里手改 wiki 页也会同步
- **进度可见** —— 上传进度、解析状态、wiki 生成状态实时展示，失败会告诉你具体原因

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
LLM_BASE_URL=https://api.moonshot.cn/v1
LLM_API_KEY=sk-...
LLM_MODEL=kimi-k2
LLM_PROTOCOL=openai        # 或 anthropic
INGESTION_ENABLED=true     # 打开自动摄入
```

不配 LLM 也能用 —— 索引、搜索、浏览都正常，只是没有自动构建和问答。

**3. 指向你的文档文件夹**

```bash
./llmwiki open ~/research
```

这会初始化工作区、建立索引，并启动 API（127.0.0.1:8000）和网页（localhost:3000）。文件夹可以是已有的资料目录 —— 它**不会移动或修改你的文件**，只会新增 `wiki/`（生成的页面）和 `.llmwiki/`（索引缓存，删了可以重建）。

**4.（可选）接入 Claude**

```bash
./llmwiki mcp ~/research
```

把输出的 JSON 配进 Claude Desktop / Claude Code，Claude 就能直接搜索和编辑你的 wiki。

## 使用方式

**上传文档**：网页里拖文件上传，或直接把文件丢进工作区文件夹。右下角面板会显示完整进度：上传中 → 解析中 → 生成 wiki 中 → 完成，失败会标明原因。

**问答**：打开 wiki 页面右下角的聊天面板，直接对知识库提问，回答会引用来源文档。

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
  wiki/                      # LLM Wiki 生成的页面
    overview.md              # 自动维护的总览
    sources/                 # 每篇文档的摘要页
    concepts/                # 概念页
    entities/                # 实体页
  .llmwiki/                  # 索引 + 缓存（隐藏，可安全删除）
    index.db
    cache/
```

`wiki/` 就是普通 Markdown —— 可以用任何编辑器打开、可以提交到 git、可以手写修改，文件监视器会自动同步索引。

## 架构

```
 Claude ──MCP──► mcp/  ──┐
                         ├──► VaultFS ──► SQLite + 文件系统（本地单用户）
 网页 ──HTTP──► api/  ──┘        │
                                 ├──► 文件监视器（自动索引）
                                 └──► 摄入队列（两步 LLM 自动构建 wiki）
```

所有写入先落到文件系统（真相之源），搜索索引是派生状态。摄入队列持久化在 SQLite 里，重启自动续跑，失败自动重试。

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

Apache 2.0 — 见 [LICENSE](LICENSE)。
