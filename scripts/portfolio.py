import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class Portfolio:
    def __init__(self, json_path: str):
        self._path = Path(json_path)
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            with open(self._path, encoding='utf-8') as f:
                self._state = json.load(f)
        else:
            self._state = {
                'cash': 500000,
                'positions': {},
                'short_positions': {},
                'last_updated': None,
                'initial_cash': 500000,
            }

    def save(self) -> None:
        tmp = self._path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self._path)

    @property
    def cash(self) -> float:
        return float(self._state['cash'])

    @property
    def positions(self) -> dict:
        return self._state['positions']

    @property
    def short_positions(self) -> dict:
        return self._state.setdefault('short_positions', {})

    @property
    def initial_cash(self) -> float:
        return float(self._state.get('initial_cash', 500000))

    @property
    def last_updated(self) -> str | None:
        return self._state.get('last_updated')

    @last_updated.setter
    def last_updated(self, value: str) -> None:
        self._state['last_updated'] = value

    def _total_value(self, current_prices: dict[str, float]) -> float:
        total = self.cash
        for sym, pos in self.positions.items():
            price = current_prices.get(sym, pos['avg_price'])
            total += pos['shares'] * price
        for sym, pos in self.short_positions.items():
            price = current_prices.get(sym, pos['avg_short_price'])
            total -= pos['shares'] * price
        return total

    def _short_exposure(self, current_prices: dict[str, float]) -> float:
        total = 0.0
        for sym, pos in self.short_positions.items():
            price = current_prices.get(sym, pos['avg_short_price'])
            total += pos['shares'] * price
        return total

    def buy(
        self,
        symbol: str,
        shares: int,
        price: float,
        max_long_pct: float = 0.30,
        current_prices: dict[str, float] | None = None,
    ) -> tuple[bool, str]:
        cost = shares * price
        if cost > self.cash:
            return False, f"現金不足（必要{cost:.0f}＞残高{self.cash:.0f}）"

        cp = current_prices or {symbol: price}
        total = self._total_value(cp)
        existing = self.positions.get(symbol, {'shares': 0, 'avg_price': price})
        new_val = (existing['shares'] + shares) * price
        if total > 0 and (new_val / total) > max_long_pct:
            return False, f"集中度上限超過（取得後{new_val/total*100:.1f}%＞上限{max_long_pct*100:.0f}%）"

        self._state['cash'] = round(self.cash - cost, 2)
        pos = self.positions.get(symbol, {'shares': 0, 'avg_price': 0.0})
        total_shares = pos['shares'] + shares
        pos['avg_price'] = round((pos['shares'] * pos['avg_price'] + cost) / total_shares, 2)
        pos['shares'] = total_shares
        self._state['positions'][symbol] = pos
        return True, ''

    def sell(self, symbol: str, shares: int, price: float) -> tuple[bool, str]:
        pos = self.positions.get(symbol)
        if pos is None or pos['shares'] < shares:
            held = pos['shares'] if pos else 0
            return False, f"保有株数不足（保有{held}株＜売却{shares}株）"

        self._state['cash'] = round(self.cash + shares * price, 2)
        pos['shares'] -= shares
        if pos['shares'] == 0:
            del self._state['positions'][symbol]
        return True, ''

    def short(
        self,
        symbol: str,
        shares: int,
        price: float,
        max_short_exposure: float = 250000,
        current_prices: dict[str, float] | None = None,
    ) -> tuple[bool, str]:
        cp = current_prices or {symbol: price}
        current_exp = self._short_exposure(cp)
        new_exp = current_exp + shares * price
        if new_exp > max_short_exposure:
            return False, f"ショート建玉上限超過（合計{new_exp:.0f}＞上限{max_short_exposure:.0f}）"

        self._state['cash'] = round(self.cash + shares * price, 2)
        pos = self.short_positions.get(symbol, {'shares': 0, 'avg_short_price': 0.0})
        total_shares = pos['shares'] + shares
        pos['avg_short_price'] = round(
            (pos['shares'] * pos['avg_short_price'] + shares * price) / total_shares, 2
        )
        pos['shares'] = total_shares
        self._state.setdefault('short_positions', {})[symbol] = pos
        return True, ''

    def cover(self, symbol: str, shares: int, price: float) -> tuple[bool, str]:
        pos = self.short_positions.get(symbol)
        if pos is None or pos['shares'] < shares:
            held = pos['shares'] if pos else 0
            return False, f"ショート建玉不足（建玉{held}株＜買戻{shares}株）"

        cost = shares * price
        if cost > self.cash:
            return False, f"現金不足（必要{cost:.0f}＞残高{self.cash:.0f}）"

        self._state['cash'] = round(self.cash - cost, 2)
        pos['shares'] -= shares
        if pos['shares'] == 0:
            del self._state['short_positions'][symbol]
        return True, ''

    def force_close_all(self, current_prices: dict[str, float]) -> list[dict]:
        trades = []
        for symbol, pos in list(self.positions.items()):
            price = current_prices.get(symbol, pos['avg_price'])
            ok, msg = self.sell(symbol, pos['shares'], price)
            if ok:
                trades.append({'symbol': symbol, 'action': 'SELL', 'shares': pos['shares'], 'price': price, 'status': 'FILLED'})
            else:
                logger.error(f"Force-close long {symbol}: {msg}")

        for symbol, pos in list(self.short_positions.items()):
            price = current_prices.get(symbol, pos['avg_short_price'])
            ok, msg = self.cover(symbol, pos['shares'], price)
            if ok:
                trades.append({'symbol': symbol, 'action': 'COVER', 'shares': pos['shares'], 'price': price, 'status': 'FILLED'})
            else:
                logger.error(f"Force-close short {symbol}: {msg}")
        return trades

    def get_summary(self, current_prices: dict[str, float] | None = None) -> dict:
        cp = current_prices or {}
        long_value = sum(
            pos['shares'] * cp.get(sym, pos['avg_price'])
            for sym, pos in self.positions.items()
        )
        short_exposure = sum(
            pos['shares'] * cp.get(sym, pos['avg_short_price'])
            for sym, pos in self.short_positions.items()
        )
        total_value = self.cash + long_value - short_exposure
        pnl = total_value - self.initial_cash
        pnl_pct = (pnl / self.initial_cash * 100) if self.initial_cash > 0 else 0.0

        long_detail = {
            sym: {
                'shares': pos['shares'],
                'avg_price': pos['avg_price'],
                'current_price': cp.get(sym, pos['avg_price']),
                'unrealized_pnl': round((cp.get(sym, pos['avg_price']) - pos['avg_price']) * pos['shares'], 2),
            }
            for sym, pos in self.positions.items()
        }
        short_detail = {
            sym: {
                'shares': pos['shares'],
                'avg_short_price': pos['avg_short_price'],
                'current_price': cp.get(sym, pos['avg_short_price']),
                'unrealized_pnl': round((pos['avg_short_price'] - cp.get(sym, pos['avg_short_price'])) * pos['shares'], 2),
            }
            for sym, pos in self.short_positions.items()
        }

        return {
            'cash': self.cash,
            'long_positions': long_detail,
            'short_positions': short_detail,
            'long_value': round(long_value, 2),
            'short_exposure': round(short_exposure, 2),
            'total_value': round(total_value, 2),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl_pct, 2),
        }
