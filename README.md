# aigc-humanizer-skill

[![License: CC BY-NC-SA 4.0](https://img.shields.io/badge/License-CC%20BY--NC--SA%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by-nc-sa/4.0/)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)

把 AI 写的文字改成"人写的"：逐段改写 + 双遍去痕，降低 GPTZero 等 AIGC 检测率，同时守住学术和技术文档的语体。

这是一个 agent skill，适用于 Claude Code、Codex、WorkBuddy 等支持 `SKILL.md` 的 agent。

规则与流水线移植自 [chi111i/BypassAIGC](https://github.com/chi111i/BypassAIGC)。原项目是一个 FastAPI + React 的 Web 服务，本仓库只保留其中的去痕规则和分段逻辑，改写由 agent 自己完成——不需要起服务，也不需要配 API Key。

## 它解决什么问题

AI 生成的文字有一套固定的写作习惯，读起来"太顺"：

- 每段都以"第一、第二、第三"或"其一、其二、其三"开头
- "通过……实现……""依托……推动……"——有动词，没内容
- "具有重要意义""取得了显著突破""可广泛应用于"——拔高但没有信息
- 同一个东西在三个段落里被叫了三个不同的名字
- 每句话长度都差不多，节奏像节拍器
- "值得注意的是""综上所述""归根结底"——删掉不影响任何意思

这些东西人工改起来很累，因为要逐句判断。这个 skill 把判断标准写成了可执行的规则表，agent 按表逐条比对。

## 直接处理你手上的文件

论文不是纯文本。它有标题层级、参考文献编号、公式、图表交叉引用。把这些剥掉再粘进一个网页表单，改完再粘回来重新排版——这是原版 Web 形态逼出来的限制，不是设计选择，也是它只能改改摘要这类片段的原因。

这个 skill 直接在原文件上工作：

| 格式 | 流程 | 保住什么 | 代价 |
|---|---|---|---|
| `.md` / `.txt` | `split` → 改 → `check` | 全部 | — |
| `.docx` | `split` → 改 → `writeback` → `check` | 标题级别、字体、字号、缩进、行距 | 段内加粗斜体会塌成段首格式 |
| `.tex` | 直接改散文 → `check_tex` | 导言区、公式、表格、`\label`、`\ref`、`\cite`、图注数字 | 无 |

两种格式各有各的坑，值得说明。

**docx 的坑在于文本被切碎。** Word 里一句话可能被拆成五六个 run（格式相同也照样拆），朴素地替换段落文本会把格式全冲掉。这里的做法是把整段文本塞进第一个 run、删掉其余 run，段落级样式因此保住，代价是句中的加粗和斜体统一成段首格式。表格、目录、页眉页脚、脚注、批注不在 `doc.paragraphs` 里，天然不会被碰——这是好事，那些地方本来也不该改。

**tex 的坑在于散文和代码混在一起。** 一个 `\ref` 改坏，整篇编译不过。所以改完必须跑 `check_tex`：环境配对、`\ref` 都有对应 `\label`、`\cite` 键都在 `thebibliography` 里、花括号平衡、`$` 配对，外加把所有两位小数的数字列出来供人工核对。图注建议整体保留，数据密集，改动风险大于收益。

## 怎么用

四步：分段 → 逐段改 → 第二遍 → 校验。

```bash
# 1. 分段（标出标题、参考文献、关键词，这些不改）
python scripts/aigc_pipeline.py split paper.md --out segs.json

# 2. agent 逐段改写 segs.json 里 skip=false 的段落

# 3. 第二遍只打三个问题：句长太均匀、同义词还在轮换、句尾假分析

# 4. 校验，不过不交付
python scripts/aigc_pipeline.py check paper.md paper.humanized.md
```

`check` 会逐段对齐，报四件事：段落数有没有变（硬失败）、原文的数据和引用有没有丢（硬失败）、字数偏差、套话残留。还会算句长变异系数——如果改完比改前更均匀，说明把句子都砍短了，这本身也是一种 AI 特征。

## 规则表长什么样

`references/humanizer-rules.md` 把 AI 写作习惯拆成 18 类，每类给"为什么像 AI"和"怎么改"。举两条：

| 禁用 | 问题 | 改法 |
|---|---|---|
| 通过…实现… | 语法完整，信息为零 | 把机制写出来，写不出就降级成陈述 |
| 该方案体现了较强的可扩展性 | 该用"是"的时候不用 | 这个方案容易扩展 |

还有一条总纲压在所有细则之上：**改写，不删信息；原文没有的日期、数字、来源、因果、评价，不补。**

这条比什么都重要。改 AI 文本最常见的翻车不是"没改干净"，而是"改得太起劲"——把事实改没了，或者为了让句子完整编了一个数字。补事实是硬错误，比残留套话严重得多。

## 安装

```bash
git clone https://github.com/<你的用户名>/aigc-humanizer-skill.git

# Claude Code
cp -r aigc-humanizer-skill ~/.claude/skills/

# WorkBuddy
cp -r aigc-humanizer-skill ~/.workbuddy-ai/skills/
```

其他 agent 放进各自的 skills 目录即可，只要它能读到 `SKILL.md`。

**依赖**：处理 `.docx` 需要 `python-docx`，其余功能只用标准库。

```bash
pip install python-docx
```

## 它做不到什么

- **不能替代人工判断。** 规则表能抓大部分模板句，但"这句话算不算拔高"仍然要看上下文。
- **不能保证过检测。** 检测器在迭代，任何方法都有保质期。这套规则对 GPTZero 这类基于困惑度和突发性的检测器更对路；对知网、万方这类中文查重，LLM 改写有概率反而升率。
- **不处理"降重"。** 降查重率和降 AI 率是两件事。前者靠确定性字符串替换凑修改率，后者靠改写写作习惯，混用会把文字改坏。

## 目录

```
SKILL.md                    agent 读的操作手册
references/
  humanizer-rules.md        18 类去痕规则 + 改写示例
  prompts.md                三段提示词原文 + 参数建议
scripts/
  aigc_pipeline.py          split / scan / check / writeback
  check_tex.py              LaTeX 结构校验
evals/
  cases.md                  改写对照样例（恰当 / 过度）
```

## 相对原版的改动

| | 原版 | 本仓库 |
|---|---|---|
| 形态 | FastAPI + React Web 服务 | agent skill，无服务 |
| 配置 | `.env` + 4 组 API Key + 代理地址 | 零配置 |
| 输入 | 网页粘贴纯文本 | md / txt / **docx** / **tex** |
| 校验 | 无 | 3 类硬失败 + 4 类警告 |
| 规则 | 一段密集字符串 | 18 类分类表，带示例 |
| 未保留 | 卡密、并发队列、流式输出、历史压缩 | 单用户场景用不上 |

去痕规则、三段提示词、分段参数（500 字上限 / 15 字跳过）原样继承，未作改动。

## 许可

[CC BY-NC-SA 4.0](LICENSE)——署名、非商业、相同方式共享。

规则与提示词来自 [chi111i/BypassAIGC](https://github.com/chi111i/BypassAIGC)，原作者以同一许可发布。详见 [NOTICE](NOTICE)。

如果你要商用，需要先获得原作者许可。

> 仓库侧边栏显示的许可是 "Other"，这是正常的。GitHub 的自动识别只覆盖 13 种 OSI 许可，不含 CC 的非商业系列，所以 CC BY-NC-SA 4.0 一律被判为 Other。以 `LICENSE` 文件的实际内容为准。
