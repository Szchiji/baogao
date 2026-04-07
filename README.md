# baogao Telegram 机器人

支持管理员后台配置、强制订阅、报告审核推送、可配置 `/start` 内容、可配置底部菜单、可配置审核反馈、可配置报告模板。

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
- 通过底部菜单执行“写报告/查阅报告/联系管理员/操作方式”
- 写报告时按模板逐项填写并预览，最后提交审核
- 查阅报告支持发送 `@用户名` 或 `#标签`

## 运行模式

- `BOT_MODE=polling`：轮询模式
- `BOT_MODE=webhook`：Webhook 模式（FastAPI + Uvicorn）

## Railway 部署

项目根目录包含 `railpack.json`，启动命令：

`python -m app.main`

## 环境变量

- `BOT_TOKEN`（必填）Telegram Bot Token
- `BOT_MODE`（可选，默认 `polling`）
- `ADMIN_USER_IDS`（可选）管理员 Telegram User ID，逗号分隔
- `DB_PATH`（可选，默认 `baogao.db`）
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

## 本地运行

```bash
pip install -r requirements.txt
export BOT_TOKEN=xxxx
python -m app.main
```
