# 上传到 GitHub · 日常速查手册

> 项目仓库：https://github.com/vzcsd1/chuanmu-10x-coin-screener
> 本机已配好：代理、提交身份、凭据。**你平时只需记住 3 条命令。**

---

## 一、最省事的方式（推荐你先用这个）

**双击脚本就行。** 桌面项目里已生成 `上传到GitHub.bat`，双击 = 自动提交 + 推送。
它会：
1. 自动检查代理是否开着（没开就提示你开 Clash）
2. 列出这次改了哪些文件，让你确认
3. 让你输入一句「这次改了什么」（不填就用默认）
4. 提交并推送

只想看状态不想上传？双击 `查看Git状态.bat`。

---

## 二、命令行的 3 条核心命令

打开 Git Bash（或任意终端），先 `cd` 到项目目录：

```bash
cd "C:/Users/Administrator/Desktop/川沐十倍币筛选"
```

| 场景 | 命令 | 大白话 |
|---|---|---|
| 看现在有什么改动 | `git status` | 体检报告：哪些文件动了、哪些还没提交 |
| 上传（一步到位） | `git add -A && git commit -m "说明" && git push` | 打包 + 贴标签 + 寄出去 |
| 拉取别人的改动 | `git pull` | 把云端最新版本取回来合并 |

**上面那条 `&&` 连起来的命令，就是你 90% 情况下要用的全部。**

---

## 三、常用命令逐个过一遍

### 1. 查看类（只看不动，随便用，安全）

```bash
git status              # 当前状态：哪些改了、哪些待提交（最常用）
git status --short      # 精简版，一行一个文件
git log --oneline -10   # 最近 10 次提交记录
git diff                # 还没 add 的改动具体改了哪几行
git diff --cached       # 已经 add 的改动具体内容
git remote -v           # 这个本地目录绑定了哪个 GitHub 仓库
```

### 2. 上传类（三步走）

```bash
git add -A                          # ① 把所有改动放进「待提交篮」
git commit -m "一句话说明改了什么"    # ② 打成一个包，贴上标签
git push                            # ③ 推到 GitHub
```

- `git add 文件名` 只挑一个文件提交；`git add -A` 是全部。
- **`commit -m` 后面的说明很重要**：以后翻历史就靠它，写「修了XX bug」比「update」有用一百倍。

### 3. 撤销类（改错了怎么办）

```bash
git restore 文件名            # 丢弃某个文件「未 add」的改动（⚠️ 不可恢复）
git restore --staged 文件名   # 把它从待提交篮里拿出来，但保留改动
git reset --soft HEAD~1       # 撤销上一次 commit，改动还在，重新提交即可
git commit --amend -m "新说明" # 只改上一次的说明文字（还没 push 时用）
```

**⚠️ 危险命令**（会永久丢改动，除非你清楚在干什么，否则别碰）：
```bash
git reset --hard     # 丢弃所有未提交改动
git clean -fd        # 删掉所有未跟踪的新文件
```

### 4. 分支类（现阶段可以先不管）

```bash
git branch              # 看有哪些分支，* 号是当前所在
git checkout -b 新分支名  # 新建并切到新分支（做实验用，不影响主线）
git checkout main       # 切回主线
```

单人开发项目，**一直待在 main 分支就够了**，不用折腾分支。

---

## 四、图形界面方式（你更习惯 GUI 的话）

本机已装的工具：

| 工具 | 怎么打开 | 适合 |
|---|---|---|
| **Git GUI** | 在项目目录右键 → 「Git GUI Here」 | 轻量，勾选文件 → 填写说明 → Commit → Push |
| **VS Code** | 命令行 `code .` 或右键用 VS Code 打开 | 左侧「源代码管理」图标，图形化提交/推送，**推荐** |
| **GitHub Desktop** | 未安装，需要可自行下载 | 最傻瓜，但要多装一个软件 |

**VS Code 用法**（最推荐给你）：
1. 用 VS Code 打开项目文件夹
2. 左侧栏点「源代码管理」（图标像分叉的树枝）
3. 改动文件列在下面，鼠标悬停文件右侧点 `+` 加入暂存
4. 上方输入框写说明 → 点「✓ 提交」
5. 点「同步更改」推送到 GitHub

---

## 五、本仓库已配好的设置（不用再动）

```bash
git config --get http.proxy      # http://127.0.0.1:7897  ← 走 Clash
git config --get core.autocrlf   # input  ← 换行符自动处理，避免全文件显示「已修改」
git config --get user.name       # vzcsd1
```

**排障：如果 push 报错「Connection was reset」**
→ 先确认 Clash 开着，再重试；大改动重试一次通常就成了。

**排障：如果 push 报 502 / CONNECT tunnel failed**
→ 代理没生效。检查 Clash 是否运行、7897 端口是否在监听。

---

## 六、什么东西不该传上去

项目 `.gitignore` 已经挡掉了这些（你**不用手动管**）：
- `data/` —— 200MB+ 行情缓存
- `reports/**` 里的大文件 —— 只保留 `.md` 文字报告
- `backup_*/*.before_onboard` —— 数百 MB 的状态快照
- `results/*.lock`、`__pycache__/` —— 运行时垃圾

**判断原则**：单个文件超过 50MB 就要警惕，超过 100MB **GitHub 会直接拒绝**。

临时发现不该传的文件，加进 `.gitignore` 即可；若已经 `add` 了，用：
```bash
git reset 文件名     # 从待提交篮里撤出（文件本身不动）
```
