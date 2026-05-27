"""Minimal Blofin REST client with correct request signing."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://openapi.blofin.com"
DEMO_URL = "https://demo-trading-openapi.blofin.com"


def sign(secret: str, method: str, path: str, body: str = "") -> tuple[str, str, str]:
    timestamp = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    prehash = f"{path}{method.upper()}{timestamp}{nonce}{body}"
    hexdigest = hmac.new(
        secret.encode(),
        prehash.encode(),
        hashlib.sha256,
    ).hexdigest()
    signature = base64.b64encode(hexdigest.encode()).decode()
    return signature, timestamp, nonce


class BlofinHttp:
    def __init__(
        self,
        api_key: str,
        secret: str,
        passphrase: str,
        *,
        demo: bool = False,
        timeout: int = 20,
    ) -> None:
        self.api_key = api_key.strip()
        self.secret = secret.strip()
        self.passphrase = passphrase.strip()
        self.base_url = DEMO_URL if demo else BASE_URL
        self.timeout = timeout
        self.session = requests.Session()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        params = params or {}
        query = ""
        if params:
            query = "?" + urlencode(params)
        sign_path = path + query
        body_str = ""
        payload = None
        if body is not None:
            body_str = json.dumps(body, separators=(",", ":"))
            payload = body_str

        signature, timestamp, nonce = sign(self.secret, method, sign_path, body_str)
        headers = {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": signature,
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-NONCE": nonce,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        url = self.base_url + path + query
        response = self.session.request(
            method.upper(),
            url,
            headers=headers,
            data=payload,
            timeout=self.timeout,
        )
        data = response.json()
        code = str(data.get("code", ""))
        if response.status_code >= 400 or code not in {"0", "200"}:
            detail = data.get("data")
            raise RuntimeError(
                f"Blofin API error {response.status_code} code={code} "
                f"msg={data.get('msg')} data={detail}"
            )
        return data.get("data")

    def get_balance(self, account_type: str = "futures") -> dict[str, Any] | list[dict[str, Any]]:
        return (
            self.request(
                "GET",
                "/api/v1/account/balance",
                params={"accountType": account_type},
            )
            or {}
        )

    def list_currencies(self) -> list[dict[str, Any]]:
        rows = self.request("GET", "/api/v1/asset/currencies") or []
        return rows if isinstance(rows, list) else []

    def get_deposit_history(
        self,
        *,
        currency: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": str(limit)}
        if currency:
            params["currency"] = currency
        rows = self.request("GET", "/api/v1/asset/deposit-history", params=params) or []
        return rows if isinstance(rows, list) else []

    def asset_transfer(
        self,
        *,
        currency: str,
        amount: str,
        from_account: str,
        to_account: str,
        client_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "currency": currency,
            "amount": amount,
            "fromAccount": from_account,
            "toAccount": to_account,
        }
        if client_id:
            body["clientId"] = client_id
        result = self.request("POST", "/api/v1/asset/transfer", body=body)
        return result if isinstance(result, dict) else {"transferId": result}

    def get_positions(self, inst_id: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"accountType": "futures"}
        if inst_id:
            params["instId"] = inst_id
        rows = self.request("GET", "/api/v1/account/positions", params=params) or []
        return rows if isinstance(rows, list) else []

    def list_instruments(self) -> list[dict[str, Any]]:
        rows = self.request("GET", "/api/v1/market/instruments") or []
        return rows if isinstance(rows, list) else []

    def list_tickers(self) -> list[dict[str, Any]]:
        rows = self.request("GET", "/api/v1/market/tickers") or []
        return rows if isinstance(rows, list) else []

    def get_funding_rate(self, inst_id: str) -> float | None:
        rows = (
            self.request(
                "GET",
                "/api/v1/market/funding-rate",
                params={"instId": inst_id},
            )
            or []
        )
        if isinstance(rows, list) and rows:
            row = rows[0]
        elif isinstance(rows, dict):
            row = rows
        else:
            return None
        try:
            return float(row.get("fundingRate") or row.get("funding_rate") or 0)
        except (TypeError, ValueError):
            return None

    def get_candles(self, inst_id: str, bar: str = "1m", limit: int = 60) -> list[list[str]]:
        return (
            self.request(
                "GET",
                "/api/v1/market/candles",
                params={"instId": inst_id, "bar": bar, "limit": str(limit)},
            )
            or []
        )

    def get_instrument(self, inst_id: str) -> dict[str, Any]:
        rows = (
            self.request(
                "GET",
                "/api/v1/market/instruments",
                params={"instId": inst_id},
            )
            or []
        )
        if isinstance(rows, list) and rows:
            return rows[0]
        if isinstance(rows, dict):
            return rows
        raise RuntimeError(f"Instrument not found: {inst_id}")

    def get_ticker(self, inst_id: str) -> dict[str, Any]:
        rows = (
            self.request(
                "GET",
                "/api/v1/market/tickers",
                params={"instId": inst_id},
            )
            or []
        )
        if not rows:
            raise RuntimeError(f"No ticker for {inst_id}")
        return rows[0]

    def set_leverage(self, inst_id: str, leverage: int, position_side: str = "net") -> None:
        self.request(
            "POST",
            "/api/v1/account/set-leverage",
            body={
                "instId": inst_id,
                "leverage": str(leverage),
                "marginMode": "isolated",
                "positionSide": position_side,
            },
        )

    def place_order(self, body: dict[str, Any]) -> dict[str, Any]:
        result = self.request("POST", "/api/v1/trade/order", body=body)
        return result if isinstance(result, dict) else {"orderId": result}

    def close_position(
        self,
        inst_id: str,
        *,
        margin_mode: str = "isolated",
        position_side: str = "net",
        broker_id: str,
    ) -> Any:
        return self.request(
            "POST",
            "/api/v1/trade/close-position",
            body={
                "instId": inst_id,
                "marginMode": margin_mode,
                "positionSide": position_side,
                "brokerId": broker_id,
            },
        )

    def partial_close_position(
        self,
        inst_id: str,
        *,
        size: float,
        margin_mode: str = "isolated",
        position_side: str = "net",
        broker_id: str,
    ) -> Any:
        """Close a specified number of contracts (partial close)."""
        return self.request(
            "POST",
            "/api/v1/trade/close-position",
            body={
                "instId": inst_id,
                "marginMode": margin_mode,
                "positionSide": position_side,
                "brokerId": broker_id,
                "size": str(size),
            },
        )

    def place_order_tpsl(self, body: dict[str, Any]) -> dict[str, Any]:
        """Attach or update TP/SL on an open position."""
        result = self.request("POST", "/api/v1/trade/order-tpsl", body=body)
        return result if isinstance(result, dict) else {"tpslId": result}

    def cancel_tpsl(self, inst_id: str, tpsl_id: str) -> Any:
        return self.request(
            "POST",
            "/api/v1/trade/cancel-tpsl",
            body=[{"instId": inst_id, "tpslId": str(tpsl_id), "clientOrderId": ""}],
        )

    def get_position_tiers(self, inst_id: str, margin_mode: str = "isolated") -> dict[str, Any]:
        rows = (
            self.request(
                "GET",
                "/api/v1/market/position-tiers",
                params={"instId": inst_id, "marginMode": margin_mode},
            )
            or []
        )
        if isinstance(rows, list) and rows:
            return rows[0]
        if isinstance(rows, dict):
            return rows
        return {}

    def get_pending_tpsl(self, inst_id: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if inst_id:
            params["instId"] = inst_id
        rows = self.request("GET", "/api/v1/trade/orders-tpsl-pending", params=params) or []
        return rows if isinstance(rows, list) else []

    def place_algo_order(self, body: dict[str, Any]) -> dict[str, Any]:
        """Place an algo order (stop-loss, take-profit, trailing stop) on an existing position."""
        result = self.request("POST", "/api/v1/trade/order-algo", body=body)
        return result if isinstance(result, dict) else {"algoId": result}

    def amend_algo_order(self, body: dict[str, Any]) -> dict[str, Any]:
        """Amend an existing algo order (e.g. update stop-loss trigger price)."""
        result = self.request("POST", "/api/v1/trade/amend-algo", body=body)
        return result if isinstance(result, dict) else {"algoId": result}

    def get_order(self, inst_id: str, order_id: str) -> dict[str, Any]:
        """Get details of a specific order."""
        rows = self.request(
            "GET",
            "/api/v1/trade/order",
            params={"instId": inst_id, "orderId": order_id},
        ) or []
        if isinstance(rows, list) and rows:
            return rows[0]
        if isinstance(rows, dict):
            return rows
        raise RuntimeError(f"Order not found: {order_id}")
