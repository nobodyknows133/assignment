import os
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg://checkout:checkout@localhost:5432/checkout",
    )
    reward_order_interval: int = int(os.getenv("REWARD_ORDER_INTERVAL", "5"))
    reward_discount_percent: Decimal = Decimal(
        os.getenv("REWARD_DISCOUNT_PERCENT", "10")
    )


settings = Settings()

if settings.reward_order_interval < 1:
    raise ValueError("REWARD_ORDER_INTERVAL must be at least 1")
if not Decimal("0") < settings.reward_discount_percent <= Decimal("100"):
    raise ValueError("REWARD_DISCOUNT_PERCENT must be greater than 0 and at most 100")
