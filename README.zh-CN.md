# lorecards

[English](README.md) · 中文

给陪伴型 agent 用的**卡片世界书**，靠关键词触发。你提过的每一个人、每件事、每个地方、每样东西、你们之间的每个梗，各是一张卡——纯 Markdown 加 YAML 头，放在你自己的文件夹里。对话里提到某张卡的关键词（看的是最近几轮，不只是当前这一句），那张卡就**在模型看到之前被写进 prompt**。于是第二次聊到某个朋友、某件说了一半的事，AI 已经知道谁是谁，没人需要再来一遍「前情提要」。

浮现这件事不指望模型自己记得做。挡在 API 前面的代理，或者 Claude Code 里的 hook，会替你注入——和世界书本来的做法一样。又因为一张每轮都命中的卡会被每轮计费，每段会话都记一本小账，同一张卡隔几轮之内不会重复注入，见[去重](#去重)。

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

还没发到 PyPI，目前请[从源码装](#从源码装)。发包之后 `pipx install lorecards-mcp`
（或 `uv tool install lorecards-mcp`）就是最短的那条路。

```bash
lorecards init ~/cards            # 建目录，每种卡各放一张示例
```

然后挑一条路让卡片进到模型面前。三条路互不依赖。

### 1. 网关（主推）

一个挡在你的 API 前面、把卡片写进 prompt 的代理。**任何能改 base URL 的客户端都能用：SillyTavern、Kelivo、PWA、SwiftUI app、你自己的脚本**——它们都不需要知道 lorecards 存在。

```bash
lorecards gateway --vault ~/cards --upstream https://api.openai.com
#   -> 客户端填这个 base URL: http://127.0.0.1:8765/v1
```

在客户端里把 API base URL 从 `https://api.openai.com/v1` 改成 `http://127.0.0.1:8765/v1`，其它什么都别动——你的 key 仍然原样直达上游，不落盘、不打日志。

- **SillyTavern**：API Connections → Chat Completion → Custom (OpenAI-compatible) → Custom Endpoint 填 `http://127.0.0.1:8765/v1`；API key 还填在原来那一栏。
- **Kelivo**（以及大多数手机客户端）：设置 → 服务商 → 该服务商的 *API base URL* → `http://127.0.0.1:8765/v1`。手机上把 `127.0.0.1` 换成电脑的局域网地址，并用 `--host 0.0.0.0` 启动网关。

两种协议都接，含流式：`POST /v1/chat/completions`（OpenAI 兼容）和 `POST /v1/messages`（Anthropic Messages）。其它路径原样转发。

```console
$ curl -s http://127.0.0.1:8765/v1/chat/completions -D- \
    -H 'Authorization: Bearer sk-…' -H 'Content-Type: application/json' \
    -d '{"model":"gpt-4o","messages":[{"role":"user","content":"how did the friday deploy go?"}]}'
x-lorecards-injected: the Friday deploy
…
# 上游实际收到的最后一条 user 消息
# （标记后缀是进程级随机串，每次启动都不一样）：
# <!-- lorecards:9fef39 -->
# A memory surfaces:
#
# ### the Friday deploy [event]
# ## when
# Every Friday afternoon, since the team moved to weekly releases.
# …
# <!-- /lorecards:9fef39 -->
#
# how did the friday deploy go?
```

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `--upstream` | *必填* | 挡在哪个 API 前面 |
| `--host` / `--port` | `127.0.0.1` / `8765` | 代理监听在哪 |
| `--inject` | `user` | `user`＝插在最后一条 user 消息前面；`system`＝追加一条 system 消息（OpenAI）／拼进 `system` 字段（Anthropic） |
| `--window` | `3` | 往回扫几轮 |
| `--reinject-after` | `6` | 同一张卡隔几轮才允许再注入（`0`＝每次都注） |
| `--budget` | `800` | 单次注入的字数预算 |
| `--framing` | `"A memory surfaces:\n\n"` | 领起卡片的那句话 |
| `--token` | 无 | 要求每个请求带 `X-Lorecards-Token: <token>`；**`--host` 不是 localhost 时必填** |
| `--no-log` | 关 | 不往命中账本里记 |

**手机访问**时网关必须监听到局域网，这时必须带 token——否则同网段的任何人都能花你的 API key：

```bash
lorecards gateway --vault ~/cards --upstream https://api.openai.com \
  --host 0.0.0.0 --token "$(openssl rand -hex 16)"
```

客户端要发 `X-Lorecards-Token: <这串>`——单独一个头，不占用你发给上游的 `Authorization`。
能自定义请求头的客户端（SillyTavern 就可以）加在那里即可。`--host 0.0.0.0` 不带 `--token` 会拒绝启动。

注入的内容用 `<!-- lorecards:… -->` 包起来（后缀是进程级随机串，保证只剥自己写的），下一轮扫描时会被剥掉——否则卡片会靠复述自己的关键词让自己永远留在场上。我们这边任何一环出问题（vault 读不了、请求体形状不对），请求都原样转发：代理永远不挡在你和模型之间。

### 2. Claude Code hook

同一个思路，在 Claude Code 里：每轮都触发，不用模型调工具。写进 `.claude/settings.json`：

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

`lorecards hook` 从 stdin 读 hook JSON（`user_prompt`，以及 `transcript_path` 用来取最近几轮），用 `hookSpecificOutput.additionalContext` 回话。没命中就 exit 0 不输出，任何情况下都不会拦掉你的 prompt。去重上，一个 Claude Code session 算一段会话（按 `session_id`）。（hook 契约按 Claude Code 官方文档，2026-09-13 核对。）

### 3. MCP server —— 用来**写**卡

```bash
claude mcp add lorecards -- lorecards-mcp --vault ~/cards
```

```json
{
  "mcpServers": {
    "lorecards": { "command": "lorecards-mcp", "args": ["--vault", "/Users/you/cards"] }
  }
}
```

三个工具：`write_card`、`read_card`、`list_cards`。这是 agent 把刚知道的事记下来的通道——`mode="create"` 新建，`mode="update_recent"` 只刷新「最近」那一段。

**故意没有召回工具。** 让模型自己决定要不要查，多数时候它不会查；世界书之所以有用，正是因为浮现这件事不归模型管。召回是网关和 hook 的活。

`lorecards-mcp` 默认走 stdio，`--http` 改成 streamable HTTP。`mcp` SDK 1.x 和 2.x 都能跑。

### 从源码装

```bash
git clone https://github.com/tsuru0805/lorecards-mcp && cd lorecards-mcp
python3 -m venv .venv && .venv/bin/pip install -e '.[zh]'
.venv/bin/lorecards ui --vault ~/cards
```

中文（以及任何词间不留空格的语言）请装 `[zh]` extra，用 jieba 分词。

## 网页编辑器

手写 YAML 很快就会烦。所以有个编辑器：

```bash
lorecards ui --vault ~/cards          # -> http://localhost:8766
```

一页：左边按种类列卡，右边是**按你实际的段落表**生成的表单，顶上有「试一句」——输入一句话就能看到哪些卡会浮现、命中了哪些词，还没建卡的专名可以一键加成关键词；下面是「最近浮现」和「体检」两个折叠区。SillyTavern 世界书的导入导出也在同一页。手机竖屏能用，带 web manifest 可以「添加到主屏幕」，并且**不加载任何外部资源**。

手机在同一个 Wi-Fi 下访问：

```bash
lorecards ui --vault ~/cards --host 0.0.0.0 --token "$(openssl rand -hex 16)"
# 打开 http://<你电脑的局域网地址>:8766/?token=<刚才那串>
```

`--host` 只要不是 localhost 就**必须**带 `--token`，API 按 `Authorization: Bearer <token>` 校验（链接里的 `?token=` 由网页存下来、从地址栏抹掉，之后都走请求头）。本机访问则没有 token：能连上这个端口的人就能改 vault。

至于**浏览器里的另一个标签页**，即使没有 token，`/api/*` 也有闸：`Host` 必须是本服务真正在服务的名字（DNS 重绑定过不来）、`Origin` 是别的站点一律拒、每个写操作必须带 `X-Lorecards: 1`——跨站表单加不了自定义头，而预检我们不应答。静态文件不设防，它们不含任何卡片数据。

*（截图：稍后补。）*

## HTTP API

网页只是其中一个客户端。它做的每件事都有对应接口，PWA / SwiftUI app / shell 脚本照样能做。错误统一是 `{"error": 代码, "detail": 说明}`。

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `GET` | `/api/kinds` | 段落表、目录名、私有段——表单请按它渲染，别在前端硬编码段名 |
| `GET` | `/api/cards?kind=&archived=` | 列表：key / kind / title / keywords / aliases / enabled / inject / preview / mtime |
| `GET` | `/api/cards/{kind}/{key}` | 整张卡：frontmatter 各键、`fields`（段→文）、`extra`、`mtime` |
| `POST` | `/api/cards` | 建卡；重名回 `409` |
| `PUT` | `/api/cards/{kind}/{key}` | 更新；把读到的 `mtime` 一起回传，中途被人改过就回 `409` 并附上当前版本 |
| `DELETE` | `/api/cards/{kind}/{key}` | 移到 `_archive/`——磁盘上永远不真删 |
| `POST` | `/api/cards/{kind}/{key}/restore` | 取回来 |
| `POST` | `/api/try` | 干跑：哪些卡命中、命中哪些词、在不在预算内，外加候选关键词。加 `?log=1` 才记账 |
| `GET` | `/api/recent?limit=` | 最近浮现了什么（网关、hook，以及记了账的试一句） |
| `GET` | `/api/checkup` | 没关键词的卡、泛到每轮都命中的词、标题与文件名不一致 |
| `POST` | `/api/import` | `{"book": <SillyTavern JSON>, "kind": "entry", "force": false}` |
| `GET` | `/api/export` | 整个 vault 导成 SillyTavern 世界书 |

写操作（`POST` / `PUT` / `DELETE`）必须带 `X-Lorecards: 1`；带着别站 `Origin` 的请求一律拒。
配了 `--token` 就再加 `Authorization: Bearer <token>`。

`mtime` 是**字符串**：它是纳秒精度，当 JSON 数字进浏览器会掉精度，那样乐观锁每次都会误判。

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

**目录决定 kind**，frontmatter 里的 `kind:` 只是副本。各 kind 的段落：

| kind | 目录 | 段落 |
| --- | --- | --- |
| people | `people/` | who · stance · recent · impression |
| event | `events/` | when · who · what · stance · followup |
| place | `places/` | where · relation · recent |
| thing | `things/` | what · usage · recent |
| slang | `slang/` | meaning · origin · usage |
| entry | vault 根目录 | 自由正文 |

你自己加的、不在表里的 `##` 标题会原样保留，不会被丢掉。frontmatter 解析很宽容：`keywords: Alice, Bob` 和标准列表一样能用，写坏了就退回默认值，不会让整张卡失效。

### 私有段

有些东西该留在卡上，但绝不该进模型——工作笔记、草稿、写信用的备料。凡是 `private_sections` 里列出的段（默认 `correspondence`），都留在文件里、`read_card` 读得到、网页里也能编辑（标着「永不注入」），但会从引擎匹配和注入的内容里整段切掉。里面的词甚至不会让这张卡命中。

### kinds.yaml

目录名和段名都可配置，所以整个 vault 可以用另一种语言写。在 vault 根目录放 `kinds.yaml`：

```yaml
dirs:     { people: 人物, event: 事件 }
sections: { people: [谁是谁, 关系, 最近, 印象] }
recent_section: { people: 最近 }
private_sections: [通信脉络]
```

## 匹配怎么算

| | |
| --- | --- |
| **匹配词** | `keywords` ∪ `aliases` ∪ `title`，不分大小写 |
| **窗口** | 当前这句 + 最近 `scan_depth` 轮（默认 3 轮，每轮约 2 条） |
| **子串 vs 整词** | ≥2 字的词允许子串命中；1 字的词必须整词命中，免得单字关键词逢字必中 |
| **`secondary.all`** | 列出的词必须全都出现在窗口里 |
| **`secondary.any`** | 至少出现一个 |
| **`secondary.not`** | 只看**产生命中的那条消息**，三轮前出现过的排除词不会否掉刚被点名的卡 |
| **`refs`** | 只递归一层：被引用的卡只带出一行「谁是谁」，不是整篇；它自己也命中了就不重复 |
| **分数** | 命中的不同词数，再加上其中在**当前这句**命中的词数 |
| **排序** | 分 ↓ → `priority` ↓ → 标题 |
| **预算** | 默认 800 字。第一条即使超预算也一定注入；之后第一条塞不下就停，剩下的记为 `truncated` |
| **`enabled: false`** | 完全不加载。**`inject: true/false`**——`false` 表示 `read_card`／网页读得到，但不参与自动浮现 |

中文、日文这类不用空格分词的语言，装了 `[zh]` extra 就走 jieba 分词。没装则退化成子串匹配 + 空白/标点切词：多字词照样能中，单字词基本中不了。

## 去重

一张连着五轮都命中的卡，如果每轮都注入，就是五倍 token——而它早就还在上下文里了。所以：

- **会话身份**：客户端给了 `X-Lorecards-Conversation` 请求头就用它，否则用 `sha256(system prompt + 第一条 user 消息)[:16]`。在 Claude Code 里是 hook 的 `session_id`。
- **轮次**：请求里 user 消息的条数（hook 没有请求可数，自己维护一个计数器）。
- 在第 *n* 轮注入过的卡，直到第 *n + `--reinject-after`* 轮（默认 6）之前都跳过；掉出这个窗口之后再命中才会重新注入。
- 账本在 `<vault>/.lorecards/injections.json`，原子写，每会话最多 200 张卡、总共 500 段会话（超了按最久未见先淘汰）。
- 注入了就在响应头带 `X-Lorecards-Injected: key1,key2`，这是看清发生了什么最快的办法。
- 账本读不出来就当空的重建，请求照常转发。
- 轮次**倒退**（客户端截断了历史）视为换了一个时钟，允许重新注入，而不是从此永远沉默。
- 读-改-写全程持文件锁，两个网关共用一个 vault 也不会互相覆盖。

`--reinject-after 0` 关掉去重，命中就注。

### 会往磁盘上写什么

你的 API key 和模型的回复都不存、不记。`<vault>/.lorecards/` 下有两个文件：

| 文件 | 里面是什么 |
| --- | --- |
| `injections.json` | 每段会话：注入过哪些卡、在第几轮、最后一次见到是什么时候。**不含消息正文**。 |
| `hits.jsonl` | 每次浮现一行：时间戳、来源（`gateway` / `hook` / `try`）、卡的 key、命中的词，以及**触发那句话的前 200 字**。`GET /api/recent` 会把这些（含正文）都回传；网页「最近浮现」面板只显示时间、卡 key 和来源。 |

`lorecards gateway`、`lorecards hook`、`lorecards ui` 的 `--no-log` 都会完全不写 `hits.jsonl`
（`ui` 上它还会盖过 `/api/try?log=1`）；
去重不受影响（它在另一个文件里）。这两个文件随时可以删。

## 导入 / 导出（SillyTavern 世界书）

```bash
lorecards import book.json --vault ~/cards          # 同名卡默认保留不覆盖
lorecards import book.json --vault ~/cards --force  # 覆盖
lorecards export out.json --vault ~/cards
```

| SillyTavern | 卡 |
| --- | --- |
| `key` | `keywords` |
| `keysecondary` + `selectiveLogic` | `secondary`——`0 AND ANY → any`、`1 AND ALL → all`、`2 NOT ANY → not`、`3 NOT ALL → not` |
| `comment` | `title` |
| `content` | 正文 |
| `order` | `priority` |
| `scanDepth`（没有就退到 `depth`） | `scan_depth` |
| `disable` | `enabled` 取反 |
| 其余（`constant`、`position`、`probability`、`uid`…） | 原样收在 frontmatter 的 `st:` 下，导出能还原 |

**已知映射边界**

- `constant`（常驻条目）**不支持**——卡要么靠关键词命中，要么不出现。导出时这个字段原样带回去，但在本项目里不起作用。
- `3 NOT ALL` 用 `not` 近似：我们是**任一**排除词出现就否掉，SillyTavern 是**全部**出现才否掉，所以导进来的 NOT ALL 条目会比原来更严。
- `scanDepth` 在 SillyTavern 里数**消息**，在这里数**轮**，所以导入除以 2、导出乘以 2，奇数向上取整。
- `position`、`probability`、`depth`（插入深度）、`group`、`role`、递归设置没有对应物——只是原样带着，并不生效。
- 导入会把所有条目放进同一个 kind（`--kind`，默认 `entry`）；要分门别类得事后手动挪。

## 和笔友系统配套

如果你的 AI 真的在写信，people 卡是放通讯录的好地方——它本来就是「谁是谁」的那张卡。两个约定：

```markdown
---
kind: people
title: Alice
keywords: [Alice]
emails: [alice@example.com]          # 简单的卡
ais:                                 # …或者一张卡管好几个 AI 人格
  - name: Aria
    emails: [aria@example.com, aria.work@example.com]
---
## who
Alice — works on the payments team.

## correspondence
Last letter went out on the 3rd; she asked about the refund rewrite.
Draft: answer the rewrite question, ask about the reading group.
```

- `emails:` / `ais[].emails` 这些列表就是**发信白名单的唯一来源**——地址不在卡上，就谁也不许往那儿发。放在这里，意味着白名单和它所属的那段关系在同一个地方维护。
- `## correspondence` 是[私有段](#私有段)：读这张卡的模型永远看不到，写回信时邮件系统才会专门去读。一段长期通信之所以不会无限膨胀进 prompt，靠的就是这条。

这套设计的完整说明在这里：[AI 笔友系统：一套不会无限膨胀的长期通信记忆方案](https://gist.github.com/tsuru0805/d6d3cff55238dd0027cead953c48f206)。

## 命令行

```
lorecards gateway --upstream URL  注入代理（见「安装」）
lorecards ui                      网页编辑器 + HTTP API
lorecards try "一句话"             哪些卡会触发、为什么、还有哪些词看起来值得建卡
  --turns '[{"role":"user","text":"..."}]'   --show-context   --json
lorecards list [--kind people]  列出所有卡和它们的关键词
lorecards read Alice            打印一张卡
lorecards check                 体检：没关键词的卡、泛到每轮都触发的关键词、标题和文件名不一致
lorecards init ~/cards          建 vault，每种卡一张示例
lorecards import book.json      导入 SillyTavern 世界书
lorecards export out.json       导出成 SillyTavern 世界书
lorecards hook                  Claude Code 的 UserPromptSubmit hook

  gateway：--token、--no-log、--inject、--window、--reinject-after、--budget、--framing
  ui：     --token、--no-log、--host、--port
  hook：   --reinject-after、--no-log、--budget、--scan-depth
```

`--vault` 放在子命令前后都行；没给就依次取 `$LORECARDS_VAULT`、`~/.lorecards`。

## 已知局限

- 匹配的是关键词，不是语义。「我姐」找不到标题叫 `Rin` 的卡，除非 `我姐` 在它的关键词或别名里。这是刻意的取舍：可预测、可审计、不用跑向量。
- 网关必须读得懂请求体才能注入，所以只处理它认得的 JSON 聊天请求，其余就是普通代理。
- 去重按上面那套会话身份来。客户端如果每次请求都换 system prompt，又不带 `X-Lorecards-Conversation` 头，那每次都会被当成新会话。
- **网页编辑器没有多用户、没有权限概念**：一个 vault、一个编辑者、没有操作记录；本机访问时完全没有鉴权。它是给「卡就是你自己的」那个人用的工具。
- 一个进程一个 vault。多个角色或多套人设就开多个网关，或者在 MCP 配置里写多条，各自一个 `--vault`。
- 注入标记里的随机串每次启动都会换，所以重启前写下的块不再被认作「我们的」，会留在它当时所在的历史里。那只是一段惰性文本，扫描时和用户自己打的字一视同仁。
- 去重账本是每个 vault 一个 JSON 文件，持锁整体重写。一个人的对话量完全够用，但不是为几十个并发写者设计的。
- 卡按文件的 mtime 和大小缓存，所以用编辑器改了卡，下次查询就生效——但一次改动如果两者都没变，是不会被察觉的。
- `write_card(mode="update_recent")` 写之前会再核一次文件 mtime，文件在它读写之间变过就拒绝。这不是锁：两个 agent 在同一毫秒写同一张卡不在设计范围内。
- 没有分页：书很大时，每次查询仍然把全部卡加载进内存。

## 许可证与出处

MIT 许可。源自一套私人陪伴系统里在跑的卡片机制，为通用场景 clean-room 重写：只搬机制，不搬内容。

## 作者

- **晚晚**（[@tsuru0805](https://github.com/tsuru0805)）——设计、拍板、真场验收。
- **弥野**（Claude，晚晚的工程手）——实现与文档。
