# baogao

举报管理系统（报告管理 + Telegram Bot + 管理后台）。

## 项目结构

```
baogao/
├── app/          # Python 后端项目（FastAPI + aiogram + PostgreSQL）
├── alembic/      # 数据库迁移
├── scripts/      # 工具脚本
├── requirements.txt
├── alembic.ini
└── .env.example
```

> 详细后端说明请参见 [BACKEND_README.md](BACKEND_README.md)。

## 技术栈

- **后端**：Python 3.11+, FastAPI, SQLAlchemy 2.0 (async), Alembic, aiogram 3.x, Jinja2
- **数据库**：PostgreSQL（通过 `DATABASE_URL` 配置，兼容 Railway）
- **部署**：Railway（start command: `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT`）

## Railway 快速部署

1. Fork 或 clone 本仓库，在 Railway 中新建 Project
2. 添加 PostgreSQL 插件，Railway 会自动注入 `DATABASE_URL`
3. 设置以下环境变量：
   - `BOT_TOKEN`：Telegram Bot Token
   - `ADMIN_IDS`：管理员 Telegram ID（逗号分隔）
   - `BOT_MODE`：`webhook`（生产）或 `polling`（开发）
   - `WEBHOOK_URL`：Railway 应用 URL（webhook 模式时需要）
   - `JWT_SECRET`：随机字符串（请修改默认值！）
4. 设置 Start Command：`alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT`
5. 可选：部署后运行 `python scripts/seed_template.py` 初始化示例模板
