# Baogao Backend

报告管理系统后端 — FastAPI + aiogram + PostgreSQL

## 概述

本项目为电报机器人举报系统的后端服务，包含：

- **FastAPI** REST API
- **aiogram 3.x** Telegram Bot（支持轮询和 Webhook 两种模式）
- **SQLAlchemy 2.0 async** ORM + **Alembic** 数据库迁移
- **Jinja2** 推送模板渲染
- **JSON Schema** 表单模板验证
- 管理员 Web 后台（OTP 登录 + JWT）

---

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env 填入真实配置
```

### 3. 运行数据库迁移

```bash
alembic upgrade head
```

### 4. 初始化示例模板（可选）

```bash
python scripts/seed_template.py
```

### 5. 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

---

## 环境变量

| 变量名 | 必填 | 默认值 | 说明 |
|--------|------|--------|------|
| `DATABASE_URL` | ✅ | — | PostgreSQL 连接串（asyncpg 格式）如 `postgresql+asyncpg://user:pass@host/db` |
| `BOT_TOKEN` | ✅ | — | Telegram Bot Token（从 @BotFather 获取） |
| `ADMIN_IDS` | ✅ | `""` | 管理员 Telegram 用户 ID，逗号分隔，如 `123456,789012` |
| `BOT_MODE` | — | `polling` | `polling`（本地开发）或 `webhook`（生产） |
| `WEBHOOK_URL` | — | `""` | Bot Webhook 的基础 URL，如 `https://your-app.railway.app` |
| `JWT_SECRET` | — | `change-me-in-production` | JWT 签名密钥，**生产环境必须修改** |
| `JWT_EXPIRE_MINUTES` | — | `1440` | JWT 有效期（分钟） |
| `OTP_EXPIRE_MINUTES` | — | `5` | OTP 验证码有效期（分钟） |
| `BASE_URL` | — | `http://localhost:8000` | 服务公开 URL，用于生成管理后台链接 |
| `PORT` | — | `8000` | 监听端口 |

---

## Railway 部署

### 部署步骤

1. 在 Railway 创建项目，添加 **PostgreSQL** 插件
2. 设置环境变量（参考上表）
3. 设置 `BOT_MODE=webhook`，`WEBHOOK_URL=https://your-app.up.railway.app`
4. 设置启动命令：

```
alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

### BOT_MODE 说明

| 模式 | 适用场景 | 说明 |
|------|----------|------|
| `polling` | 本地开发 | Bot 主动轮询 Telegram 服务器，无需公网 URL |
| `webhook` | 生产/Railway | Telegram 推送更新到 `/telegram/webhook`，需要 HTTPS 公网 URL |

---

## API 概览

### 报告

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/reports/` | 列出报告（支持分页和状态过滤） |
| `GET` | `/api/reports/{id}` | 获取单个报告详情 |

### 模板

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/templates/` | 列出所有模板 |
| `GET` | `/api/templates/{key}` | 获取单个模板 |
| `POST` | `/api/templates/` | 创建模板（自动验证 JSON Schema） |
| `PUT` | `/api/templates/{key}` | 更新模板 |
| `DELETE` | `/api/templates/{key}` | 删除模板 |
| `POST` | `/api/templates/preview` | 渲染推送模板预览 |

### 订阅

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/subscriptions/` | 列出订阅频道 |
| `POST` | `/api/subscriptions/` | 添加订阅 |
| `DELETE` | `/api/subscriptions/{id}` | 删除订阅 |

### 管理后台

| 路径 | 说明 |
|------|------|
| `/admin/login` | OTP 登录页 |
| `/admin/dashboard` | 仪表盘 |
| `/admin/templates` | 模板管理列表 |
| `/admin/templates/{key}/edit` | 模板编辑器 |
| `/admin/templates/{key}/preview` | 模板预览 |

### Telegram Webhook

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/telegram/webhook` | 接收 Telegram 更新（仅 webhook 模式使用） |

---

## 管理员 OTP 登录流程

1. 管理员访问 `/admin` → 跳转到 `/admin/login`
2. 页面显示 6 位验证码，并开始每 3 秒轮询一次验证状态
3. 管理员将验证码发送给 Bot 私聊
4. Bot 验证通过后，浏览器自动跳转到仪表盘并设置 JWT Cookie

---

## 项目结构

```
baogao/
├── app/
│   ├── main.py              # FastAPI 应用入口
│   ├── config.py            # pydantic-settings 配置
│   ├── database.py          # SQLAlchemy 异步引擎
│   ├── models/              # 数据库模型
│   ├── schemas/             # Pydantic 校验模式
│   ├── api/                 # REST API 路由
│   ├── admin/               # 管理后台（HTML + 路由）
│   ├── bot/                 # aiogram Bot 及处理器
│   └── services/            # 业务逻辑服务
├── alembic/                 # 数据库迁移
├── scripts/                 # 工具脚本
├── requirements.txt
├── .env.example
└── alembic.ini
```
