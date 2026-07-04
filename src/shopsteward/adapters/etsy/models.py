"""Pydantic models mirroring the Etsy Open API v3 shapes we consume."""

from pydantic import BaseModel, Field


class Money(BaseModel):
    amount: int
    divisor: int
    currency_code: str

    @property
    def as_float(self) -> float:
        return self.amount / self.divisor


class EtsyShop(BaseModel):
    shop_id: int
    shop_name: str
    listing_active_count: int = 0
    transaction_sold_count: int = 0


class EtsyListing(BaseModel):
    listing_id: int
    title: str
    state: str
    quantity: int
    views: int = 0
    num_favorers: int = 0
    price: Money
    tags: list[str] = Field(default_factory=list)

    @property
    def price_usd(self) -> float:
        return self.price.as_float


class EtsyTransaction(BaseModel):
    transaction_id: int
    listing_id: int
    quantity: int
    price: Money


class EtsyReceipt(BaseModel):
    receipt_id: int
    created_timestamp: int
    grandtotal: Money
    transactions: list[EtsyTransaction] = Field(default_factory=list)

    @property
    def total_usd(self) -> float:
        return self.grandtotal.as_float
