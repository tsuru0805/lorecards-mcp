# lorecards

[English](README.md) · 中文

给陪伴型 agent 用的**卡片世界书**，靠关键词触发。你提过的每一个人、每件事、每个地方、每样东西、你们之间的每个梗，各是一张卡——纯 Markdown 加 YAML 头，放在你自己的文件夹里。对话里提到某张卡的关键词（看的是最近几轮，不只是当前这一句），那张卡就浮现出来。于是第二次聊到某个朋友、某件说了一半的事，AI 已经知道谁是谁，没人需要再来一遍「前情提要」。

它不是 RAG，也不是记忆库。没有向量、没有相似度打分、没有背着你偷偷写的东西。卡片书是**正典**：你（或者 agent 通过 `write_card` 工具）决定值得记住的东西，放在你看得见、改得了、删得掉的文件里。

```
~/cards/
  people/Alice.md
  events/the Friday deploy.md
  places/the corner cafe.md
  things/the blue notebook.md
  slang/ship it Friday.md
```

## 安装

两条接法，互不依赖——挑一条，或者两条都用。

### 1. 当 MCP server 用（任何 MCP 客户端）

```bash
# Claude Code
claude mcp add lorecards -- uvx lorecards-mcp --vault ~/cards

# 或者直接跑
uvx lorecards-mcp --vault ~/cards
pipx run lorecards-mcp --vault ~/cards      # 不用 uv 的同款
```

Claude Desktop——`claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "lorecards": {
      "command": "uvx",
      "args": ["lorecards-mcp", "--vault", "/Users/you/cards"]
    }
  }
}
```

中文（或任何词与词之间没有空格的语言）要装分词扩展：
`uvx --from 'lorecards-mcp[zh]' lorecards-mcp --vault ~/cards`。`mcp` Python SDK 1.x 和 2.x 都能跑。

Claude Code 里新加的 MCP server 要审批一次——在那个项目里起一次 `claude`，批准它。

**这条路的局限：** 卡要浮现，得靠模型自己去调 `recall_cards`。它不调，什么都不会发生。给它一条常驻指令——放 system prompt 或 `CLAUDE.md`：

> 回答之前，如果这条消息提到了某个人、某个地方、某件事或某个说法，可能已经在卡片书里，就把用户这条消息和最近几轮一起交给 `recall_cards`。它很便宜，没命中就返回空。

### 2. 当 Claude Code 的 hook 用（每轮都触发，不用模型调工具）

这条路会自动把命中的卡注入进去，等价于系统侧的世界书注入。写进 `.claude/settings.json`：

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          { "type": "command", "command": "lorecards hook --vault ~/cards", "timeout": 10 }
        ]
      }
    ]
  }
}
```

`lorecards hook` 从 stdin 读 hook 的 JSON（`user_prompt`，还有 `transcript_path` 用来取最近几轮），用 `hookSpecificOutput.additionalContext` 回答。没有卡命中时退出码 0、什么都不打印；它永远不会拦住一条 prompt——vault 坏了你损失的是卡，不是这一轮对话。（hook 契约按 Claude Code 官方文档，2026-09-13 核过。）

这条命令需要 `lorecards` 在 PATH 上：`uv tool install lorecards-mcp`，或 `pipx install lorecards-mcp`，或者把 hook 指到某个 virtualenv：
`command: "/path/to/.venv/bin/lorecards hook --vault ~/cards"`。

### 从源码装

```bash
git clone https://github.com/tsuru0805/lorecards-mcp && cd lorecards-mcp
python3 -m venv .venv && .venv/bin/pip install -e '.[zh]'
.venv/bin/lorecards-mcp --vault ~/cards        # MCP server
.venv/bin/lorecards try "hello" --vault ~/cards
```

MCP 配置或 hook 命令里写 `.venv/bin/lorecards-mcp` / `.venv/bin/lorecards` 的绝对路径。

### 建一个 vault

```bash
lorecards init ~/cards      # 建目录，每种卡放一张示例
lorecards try "how did the friday deploy go" --vault ~/cards
```

## 卡的格式

`~/cards/people/Alice.md`：

```markdown
---
kind: people
title: Alice
keywords: [Alice, payments team]
aliases: [Al]
secondary:
  not: [Alice Cooper]
scan_depth: 3
refs: [Bob]
priority: 1
---
## who
Alice — works on the payments team, sits two desks away.

## stance
Friendly. She reviews my pull requests and I review hers.

## recent
Took over the refund flow rewrite in September.

## impression
Direct, allergic to meetings that could have been a message.
```

**目录**决定卡的种类；头里的 `kind:` 只是副本。各种卡的段落：

| 种类 | 目录 | 段落 |
| --- | --- | --- |
| people 人物 | `people/` | who 谁是谁 · stance 态度 · recent 最近 · impression 印象 |
| event 事件 | `events/` | when 时间 · who 涉及谁 · what 经过 · stance 态度 · followup 后续 |
| place 地点 | `places/` | where 在哪 · relation 关系 · recent 最近 |
| thing 物件 | `things/` | what 是什么 · usage 怎么用 · recent 最近 |
| slang 梗 | `slang/` | meaning 意思 · origin 出处 · usage 用法 |
| entry 普通条目 | vault 根目录 | 自由正文 |

你自己加的、不在表里的标题原样保留，永远不会被丢掉。YAML 头是宽容解析的：`keywords: Alice, Bob` 和正规的列表一样认，值写坏了就退回默认，不会把整张卡搞坏。

目录名和段名都可以改，所以 vault 可以整个用另一种语言写——在 vault 根目录放一个 `kinds.yaml`：

```yaml
dirs:     { people: 人物, event: 事件 }
sections: { people: [谁是谁, 态度, 最近, 印象] }
recent_section: { people: 最近 }
```

## 匹配怎么算

| | |
| --- | --- |
| **匹配词** | `keywords` + `aliases` + `title`，不分大小写 |
| **窗口** | 当前这条消息 + 最近 `scan_depth` 轮（默认 3 轮，每轮约两条消息） |
| **子串还是整词** | 两个字及以上的词允许在词内部命中；单字词必须整个 token 匹配，所以单字关键词不会见字就触发 |
| **`secondary.all`** | 列出的词必须全部在窗口里出现 |
| **`secondary.any`** | 至少一个在窗口里出现 |
| **`secondary.not`** | 只查产生命中的那几条消息，所以三轮前随口提过的排除词不会否掉用户刚点名的卡 |
| **`refs`** | 只向下一层：被引用的卡贡献一行（「这是谁」），不带整张正文；它自己已经命中时就不重复 |
| **分数** | 命中的不同词的个数，*当前消息*里命中的每个词再加一分 |
| **顺序** | 分数降序，再按 `priority` 降序，再按标题 |
| **预算** | 默认 800 字符。第一张卡永远进，哪怕超预算；第一张放不下的卡就停，后面的在 `truncated` 里报个数 |
| **`enabled: false`** | 根本不加载。**`inject: true/false`**——`false` 时 `read_card` / `list_cards` 还读得到，只是不进自动召回 |

中文、日文这类不用空格分词的文字，装了 `[zh]` 扩展就用 [jieba](https://github.com/fxsjy/jieba) 分词。没装的话退化成子串匹配加空白/标点切词：多字词照样能用，单字词基本不行。

## 导入 / 导出（SillyTavern 世界书）

```bash
lorecards import book.json --vault ~/cards          # 已有的卡保留
lorecards import book.json --vault ~/cards --force  # 覆盖它们
lorecards export out.json --vault ~/cards
```

| SillyTavern | 卡 |
| --- | --- |
| `key` | `keywords` |
| `keysecondary` + `selectiveLogic` | `secondary`——`0 AND ANY → any`、`1 AND ALL → all`、`2 NOT ANY → not`、`3 NOT ALL → not` |
| `comment` | `title` |
| `content` | 卡的正文 |
| `order` | `priority` |
| `scanDepth`（没有就用 `depth`） | `scan_depth` |
| `disable` | `enabled`，取反 |
| 其它一切（`constant`、`position`、`probability`、`uid`……） | 原样存在头里的 `st:` 下面，导出时能原样带回去 |

**已知的映射边界**

- `constant`（常驻条目）**不支持**——卡要么靠关键词触发，要么不触发。这个标记导出时保留，但在本书里不起作用。
- `3 NOT ALL` 用 `not` 近似，而 `not` 是**任一**词出现就否决。SillyTavern 要**全部**出现才否决，所以导进来的 NOT ALL 条目在这里更严。
- `scanDepth` 在 SillyTavern 数的是**消息条数**，这里数的是**轮数**，所以导入时减半、导出时加倍；奇数向上取整。
- `position`、`probability`、`depth`（插入深度）、`group`、`role` 和递归设置没有对应物——只搬运，不生效。
- 导入把所有条目放进同一种类（`--kind`，默认 `entry`）；分到 people / events / places 里要事后手动整理。

## 命令行

```
lorecards try "sentence"        哪些卡会触发、为什么、还有哪些词看起来值得建卡
  --turns '[{"role":"user","text":"..."}]'   --show-context   --json
lorecards list [--kind people]  列出所有卡和它们的关键词
lorecards read Alice            打印一张卡
lorecards check                 体检：没关键词的卡、泛到每轮都触发的关键词、标题和文件名不一致
lorecards init ~/cards          建 vault，每种卡一张示例
lorecards import book.json      导入 SillyTavern 世界书
lorecards export out.json       导出成 SillyTavern 世界书
lorecards hook                  Claude Code 的 UserPromptSubmit hook
```

## 已知局限

- **MCP 那条路取决于模型愿不愿意调 `recall_cards`。** 只有 hook 那条路每轮必触发。
- 匹配的是关键词，不是语义。「我姐」找不到标题叫 `Rin` 的卡，除非 `我姐` 在它的关键词或别名里。这是刻意的取舍：可预测、可审计、不用跑向量。
- 一个 server 进程一个 vault。多个角色或多套人设就在 MCP 配置里写多条，各自一个 `--vault`。
- 卡按文件的 mtime 和大小缓存，所以用编辑器改了卡，下次查询就生效——但一次改动如果两者都没变，是不会被察觉的。
- `write_card(mode="update_recent")` 写之前会再核一次文件 mtime，文件在它读写之间变过就拒绝。这不是锁：两个 agent 在同一毫秒写同一张卡不在设计范围内。
- 没有分页：书很大时，每次查询仍然把全部卡加载进内存。

## 作者

- **晚晚**（[@tsuru0805](https://github.com/tsuru0805)）——设计、拍板、真场验收。
- **弥野**（Claude，晚晚的工程手）——实现与文档。

从 tilldusk 家庭系统里跑着的那套卡片世界书 clean-room 抽出。MIT 许可。
