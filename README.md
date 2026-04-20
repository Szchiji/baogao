# baogao Telegram 机器人

支持管理员后台配置、强制订阅、报告审核推送、可配置 `/start` 内容、可配置底部菜单、可配置审核反馈、可配置报告模板。

同时内置 **群组邀请链接管理**功能（来自 [Szchiji/sm](https://github.com/Szchiji/sm)），可为多个 Telegram 群组生成一次性限时邀请链接，支持邀请冷却和管理员审批流程。

## 功能概览

### 管理员
- `/admin`：打开管理后台（mini app URL）
- 管理后台可配置：
  - 强制订阅频道
  - 审核通过推送频道
  - `/start` 文本、多媒体、按钮
  - 底部键盘菜单按钮
  - 审核通过/驳回反馈模板
  - 报告模板（JSON）
  - 联系管理员/操作方式/查询提示文案
- `/pending` 查看待审核报告
- `/approve 报告ID` 通过
- `/reject 报告ID 原因` 驳回

### 用户
- `/start` 自动检测是否订阅（启用强制订阅时）
- 通过底部菜单执行"写报告/查阅报告/联系管理员/操作方式"
- 写报告时按模板逐项填写并预览，最后提交审核
- 查阅报告支持发送 `@用户名` 或 `#标签`

### 群组邀请链接管理

通过 `/start join_<admin_id>` 或 `/start joinall_<admin_id>` 参数进入邀请流程。

**管理员命令：**
- `/invpanel`：打开邀请管理面板（按钮菜单）
- `/bindgroup [群组ID] [名称]`：手动绑定群组
- `/addadmin [用户ID]`：添加邀请子管理员
- `/listgroups`：列出已绑定群组
- `/removegroup [群组ID]`：移除群组
- `/setapproval [群组ID]`：切换群组审批模式（开/关）
- `/invstats`：查看邀请统计
- `/invcleanup`：手动清理过期邀请记录
- `/invrevoke`：手动撤销失效邀请链接
- `/cancel`：取消当前输入状态

将机器人设为群组**管理员**后，机器人会自动绑定该群组；移除机器人时自动解绑。

## 运行模式

- `BOT_MODE=polling`：轮询模式
- `BOT_MODE=webhook`：Webhook 模式（FastAPI + Uvicorn）

## Railway 部署

项目根目录包含 `railpack.json`，启动命令：

`python -m app.main`

> 数据库使用 **PostgreSQL**，数据持久化存储，重新部署不会丢失。
> 在 Railway 中添加一个 PostgreSQL 插件后，`DATABASE_URL` 环境变量会自动注入，无需额外配置。
>
> 群组邀请功能使用 **Redis**（临时存储）。在 Railway 中添加 Redis 插件后 `REDIS_URL` 会自动注入。
> 若 Redis 不可用，邀请功能会自动禁用，报告机器人功能正常运行。

## 环境变量

### 报告机器人（必填）

- `BOT_TOKEN`（必填）Telegram Bot Token
- `DATABASE_URL`（必填）PostgreSQL 连接 URL，例如 `postgresql://user:pass@host:5432/dbname`

### 报告机器人（可选）

- `BOT_MODE`（可选，默认 `polling`）
- `ADMIN_USER_IDS`（可选）管理员 Telegram User ID，逗号分隔；这些用户同时也是邀请模块的管理员
- `ADMIN_PANEL_URL`（建议填写）管理后台外网 URL（建议 `https://xxx.up.railway.app/admin/login`）
- `ADMIN_PANEL_TOKEN`（建议填写）后台访问令牌（登录后写入 HttpOnly Cookie）

Webhook 额外变量：
- `WEBHOOK_URL`（必填）例如 `https://xxx.up.railway.app`
- `WEBHOOK_PATH`（可选，默认 `/webhook`）
- `WEBHOOK_SECRET`（建议填写）Telegram Webhook Secret Token
- `HOST`（可选，默认 `0.0.0.0`）
- `PORT`（可选，默认 `8000`）

报告链接配置：
- 在管理后台设置 `报告链接基地址`，例如 `https://xxx.up.railway.app`，用于给查询结果拼接报告详情链接。

### 群组邀请模块（可选）

- `REDIS_URL`（可选，默认 `redis://localhost:6379/0`）Redis 连接 URL；留空则邀请功能禁用
- `INVITE_EXPIRE_MINUTES`（可选，默认 `5`）邀请链接有效期（分钟）
- `INVITE_COOLDOWN_HOURS`（可选，默认 `24`）同一用户同一群组的邀请冷却时间（小时）
- `WELCOME_TEXT`（可选，默认 `👋 欢迎！请选择要加入的群组：`）邀请选择界面欢迎语

## 本地运行

```bash
pip install -r requirements.txt
export BOT_TOKEN=xxxx
export DATABASE_URL=postgresql://user:pass@localhost:5432/baogao
# 可选：启用群组邀请功能
export REDIS_URL=redis://localhost:6379/0
python -m app.main
```
