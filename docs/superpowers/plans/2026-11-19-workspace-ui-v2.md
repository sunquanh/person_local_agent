# 阅读栏与工作区持久化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把主界面做成已批准的阅读栏，并让选定工作目录成为会话与配置的默认存储位置。

**Architecture:** `server/storepath.py` 纯函数决定工作目录和配置文件，不读进程 cwd。界面偏好写入 `<工作目录>/.agent/ui.json`。视觉只改 token 和布局，沿用现有页面结构。

**Tech Stack:** Python 3 标准库、现有无构建前端。

## Global Constraints

- 不合并 main，不部署，不读用户本机真实数据
- 测试只用临时目录和伪造配置
- 密钥不进日志、截图、ui.json
- 旧 `config.json` 不删除、不覆盖工作区里已有的不同配置
- 相对路径相对程序目录，不随 cwd 变化

---

### Task 1: 路径决议

- [ ] 为指针、显式配置、旧文件导入、冲突、不可写、中文空格路径、不同 cwd 写失败测试
- [ ] 实现 `prepare_launch` 并接到 `app.py` 与配置保存
- [ ] 全量单测通过

### Task 2: 界面偏好

- [ ] `.agent/ui.json` 的读写测试，拒绝写入密钥字段
- [ ] `GET/POST /api/ui`，前端主题、上次会话、模型覆盖和栏宽以该文件为准

### Task 3: 差异预览

- [ ] 用新增、修改、无变化三个临时文件断言 diff 行和计数
- [ ] 后端已正确则不改算法；浏览器打开弹窗核对新增行

### Task 4: 阅读栏

- [ ] 灰白 token、蓝色强调、居中栏、浅色用户气泡、检查器默认关闭、状态层在输入框上方
- [ ] 深色和弹窗沿用同一 token
- [ ] 1280×800 实拍主会话、深色、设置、批处理、窄窗口，不用设计稿代替
