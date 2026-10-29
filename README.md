# aigc-humanizer-skill

[![License: CC BY-NC-SA 4.0](https://img.shields.io/badge/License-CC%20BY--NC--SA%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by-nc-sa/4.0/)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-blue.svg)](https://www.python.org/)

AI 写的中文有个通病：句子都对，读完却不知道说了什么。这个 skill 把这类文字逐段改一遍，改完再检查有没有改坏。

论文、技术文档、个人陈述都能用。跑在你的 agent 里，不用起服务，不用配 API Key。

规则和提示词来自 [chi111i/BypassAIGC](https://github.com/chi111i/BypassAIGC)。原项目是个 Web 服务，本仓库只留了它的去痕规则和分段逻辑，改写交给 agent 直接做。

## 装一下

**最省事的办法**：把下面这段话连同仓库地址丢给你的 agent，让它自己装。

```
把这个仓库装成我的 skill：
https://github.com/shandaiqiecheng-commits/aigc-humanizer-skill

clone 下来放到你的 skills 目录，Claude Code 是 ~/.claude/skills/，
WorkBuddy 是 ~/.workbuddy-ai/skills/。装好告诉我一声。
```

**自己动手**：

```bash
git clone https://github.com/shandaiqiecheng-commits/aigc-humanizer-skill.git

cp -r aigc-humanizer-skill ~/.claude/skills/        # Claude Code
cp -r aigc-humanizer-skill ~/.workbuddy-ai/skills/  # WorkBuddy
```

处理 `.docx` 需要装一下 `python-docx`，其余功能只用标准库：

```bash
pip install python-docx
```

## 哪些 agent 能用

支持 `SKILL.md` 的都能装，常见的有 Claude Code、WorkBuddy、CodeBuddy、Cline、Trae、Gemini CLI。

如果你的 agent 没有 skill 机制，把 `SKILL.md` 当规则文件放进项目目录也能生效，只是脚本要自己手动跑。

## 能处理什么文件

| 格式 | 怎么处理 |
|---|---|
| `.md` / `.txt` | 直接改 |
| `.docx` | 改完写回原文档，标题级别、字体、缩进、行距都留着 |
| `.tex` | 只动正文，命令、公式、表格、参考文献一概不碰 |

docx 有一个代价要说清楚：句中的加粗和斜体会统一成段首格式。中文论文正文一般不靠行内加粗强调，影响不大，但交付前最好抽查几段。

tex 改完会跑一遍结构校验：环境有没有配对、`\ref` 有没有对应的 `\label`、`\cite` 的键在不在文献表里、括号和公式定界符平不平衡。一个 `\ref` 写错整篇就编译不过，这步不能省。

## 它做什么

逐段改写，每段改完只交这一段，不整篇重写。整篇重写会让全文风格变得整齐，而整齐本身就是机器味。人写长文，前后段的用词习惯是有波动的。

改两遍。第一遍删套话、拆模板句、把"通过……实现……"这种空架子换成具体机制。第二遍专门修第一遍留下的后遗症：句子被删短之后长度容易趋同，得重新拉开。

改完自动和原文比对，查三件事：段落数有没有变、原文的引用编号和数字有没有丢、标题和参考文献有没有被误改。任一条不过就不让交付。

## 做不到什么

- 它改的是句子和用词，改不了文章结构。整篇三段式、每段一个论点加一个例子，光改句子救不回来。
- 检测器在迭代，任何方法都有保质期。这套规则更适合看困惑度和突发性的检测器。
- 不处理"降重"。降查重率和去 AI 腔是两件事，混着做会把文字改坏。

## 相对原版的改动

| | 原版 | 本仓库 |
|---|---|---|
| 形态 | FastAPI + React Web 服务 | agent skill，无服务 |
| 配置 | `.env` + 4 组 API Key + 代理地址 | 零配置 |
| 输入 | 网页粘贴纯文本 | md / txt / **docx** / **tex** |
| 校验 | 无 | 3 类硬失败 + 4 类警告 |
| 规则 | 一段密集字符串 | 18 类分类表，带示例 |
| 未保留 | 卡密、并发队列、流式输出、历史压缩 | 单用户场景用不上 |

去痕规则、三段提示词、分段参数（500 字上限 / 15 字跳过）原样继承，没有改动。

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

## 许可

[CC BY-NC-SA 4.0](LICENSE)：署名、非商业、相同方式共享。要商用得先找原作者 [Yan Wenxin](https://github.com/chi111i) 拿授权。

侧边栏显示 Other 是正常的。GitHub 只认 13 种 OSI 许可，CC 的非商业系列不在里面，所以 CC BY-NC-SA 4.0 一律显示 Other，跟文件内容无关。以 `LICENSE` 为准。
