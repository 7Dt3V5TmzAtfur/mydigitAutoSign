# 项目记忆

## 约定

- 修改代码前先用 git 保存当前状态：无仓库则 `git init`，并提交基线。

## 结构

- `main.py`：原始本地版，账号硬编码在文件内，使用 pickle 会话缓存。
- `mydigit_sign_ql.py`：青龙单文件版，配置全部来自环境变量，不依赖 `pyproject.toml`。
- 目标站点为 Discuz 论坛 `www.mydigit.cn`，签到插件 `k_misign`。

## 注意

- 游客访问签到页 `k_misign-sign.html` 同样返回 200 且带 `formhash`，判断登录态必须依据页头 `action=logout` 或「欢迎您回来」。
- 青龙任务命令：`task mydigit_sign_ql.py`；默认 cron `17 7 * * *`。
