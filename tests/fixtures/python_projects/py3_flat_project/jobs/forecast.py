from __future__ import annotations


def normalize_sku(raw_sku: str) -> str:
    return raw_sku.strip().upper().replace(" ", "-")


def forecast_for_store(history: list[int], *, uplift: float = 1.0) -> int:
    if not history:
        return 0
    baseline = sum(history[-3:]) / min(len(history), 3)
    return max(0, int(round(baseline * uplift)))


class StoreForecast:
    def __init__(self, store_id: str):
        self.store_id = store_id

    def predict(self, sku: str, history: list[int]) -> dict:
        return {
            "store_id": self.store_id,
            "sku": normalize_sku(sku),
            "quantity": forecast_for_store(history, uplift=1.1),
        }
