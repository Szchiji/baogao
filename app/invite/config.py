import os

# Redis connection URL
REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

# Minutes before an invite link expires
INVITE_EXPIRE_MINUTES: int = int(os.getenv("INVITE_EXPIRE_MINUTES", "5"))

# Hours a user must wait before requesting another invite to the same group
INVITE_COOLDOWN_HOURS: int = int(os.getenv("INVITE_COOLDOWN_HOURS", "24"))

# Welcome message shown when a user follows an invite link
WELCOME_TEXT: str = os.getenv("WELCOME_TEXT", "👋 欢迎！请选择要加入的群组：")
