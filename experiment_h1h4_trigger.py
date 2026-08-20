#!/usr/bin/env python3
"""
実験ログ: 「1時間・4時間足の方向一致＋5分足の反発」パターン（ボリンジャーバンド版）を、
反発判定・損切り幅の閾値(3/5/10/15/20pips)ごとに仮想トレードとして記録する。

本番シグナル(signal.json)とは完全に別ロジック・別ファイル(experiment_h1h4_log.json)で、
サイトの表示には一切使わない。過去データでのバックテスト
（19ヶ月・USD/JPY、LOOKBACK=15固定）:
  3pips : 343件 勝率77.8% PF2.58 +2,501pips (1回平均7.3pips)
  5pips : 281件 勝率76.9% PF2.34 +2,108pips (1回平均7.5pips)
  10pips: 185件 勝率81.1% PF2.56 +1,963pips (1回平均10.6pips)
  15pips: 113件 勝率77.9% PF2.02 +1,181pips (1回平均10.5pips)
  20pips:  66件 勝率75.8% PF2.14 +  919pips (1回平均13.9pips)
という、閾値を広げるほど1回あたりの獲得pipsは伸びるが総獲得pipsはむしろ減る
（頻度低下の影響の方が大きい）結果が出たため、実際の未来データでもこの傾向が
再現するかを検証する目的で開始。

本番のcompute_signal.py（5分/15分/1時間+1分トリガー）とは時間足構成自体が異なる
別ロジックである点に注意。失敗してもメインの更新処理は止めない設計。
"""

import json
import os
import statistics
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import compute_signal as cs  # noqa: E402

THRESHOLDS_PIPS = [3.0, 5.0, 10.0, 15.0, 20.0]
LOOKBACK = 15
EDGE_THRESHOLD = 1.3
REVERT_WINDOW = 10


def aggregate_to_4h(hourly_bars):
    buckets = {}
    order = []
    for b in hourly_bars:
        dt = datetime.fromtimestamp(b["t"], tz=timezone.utc)
        key = (dt.toordinal() * 24 + dt.hour) // 4
        if key not in buckets:
            buckets[key] = {"t": b["t"], "c": b["c"]}
            order.append(key)
        else:
            buckets[key]["c"] = b["c"]
    return [buckets[k] for k in order]


def bollinger_channel(closes, lookback):
    series = closes[-lookback:] if len(closes) > lookback else closes[:]
    if len(series) < 5:
        return None
    mid = statistics.fmean(series)
    sigma = statistics.pstdev(series)
    sigma = sigma if sigma > 1e-6 else 1e-6
    latest = series[-1]
    position = (latest - mid) / sigma
    return {"mid": mid, "upper": mid + 2 * sigma, "lower": mid - 2 * sigma, "sigma": sigma, "position": position}


def momentum_direction(ch):
    if ch is None:
        return "FLAT"
    if ch["position"] >= EDGE_THRESHOLD:
        return "UP"
    if ch["position"] <= -EDGE_THRESHOLD:
        return "DOWN"
    return "FLAT"


def detect_reversal_setup(bars, ch, direction, revert_min_pips):
    if ch is None or len(bars) < REVERT_WINDOW:
        return None
    recent = bars[-REVERT_WINDOW:]
    closes = [b["c"] for b in recent]
    latest = closes[-1]
    sigma, mid = ch["sigma"], ch["mid"]
    if direction == "BUY":
        trough_idx = min(range(len(closes)), key=lambda i: closes[i])
        trough = closes[trough_idx]
        if trough_idx == len(closes) - 1:
            return None
        if (trough - mid) / sigma > -EDGE_THRESHOLD:
            return None
        if (latest - trough) * 100 < revert_min_pips:
            return None
        return trough
    else:
        peak_idx = max(range(len(closes)), key=lambda i: closes[i])
        peak = closes[peak_idx]
        if peak_idx == len(closes) - 1:
            return None
        if (peak - mid) / sigma < EDGE_THRESHOLD:
            return None
        if (peak - latest) * 100 < revert_min_pips:
            return None
        return peak


def compute_bias_for_threshold(m5, h1, h4, threshold_pips):
    ch_h1 = bollinger_channel([b["c"] for b in h1], LOOKBACK)
    ch_h4 = bollinger_channel([b["c"] for b in h4], LOOKBACK)
    dir_h1 = momentum_direction(ch_h1)
    dir_h4 = momentum_direction(ch_h4)

    candidate = None
    if dir_h1 == "UP" and dir_h4 == "UP":
        candidate = "BUY"
    elif dir_h1 == "DOWN" and dir_h4 == "DOWN":
        candidate = "SELL"

    if not candidate:
        return "WAIT", None, None

    ch_m5 = bollinger_channel([b["c"] for b in m5], LOOKBACK)
    extreme = detect_reversal_setup(m5, ch_m5, candidate, threshold_pips)
    if extreme is None:
        return "WAIT", None, None

    latest_price = m5[-1]["c"]
    sl_buffer = threshold_pips / 100
    if candidate == "BUY":
        move = abs(latest_price - extreme)
        entry, tp, sl = latest_price, latest_price + move, extreme - sl_buffer
    else:
        move = abs(latest_price - extreme)
        entry, tp, sl = latest_price, latest_price - move, extreme + sl_buffer

    return candidate, latest_price, {"entry": round(entry, 3), "take_profit": round(tp, 3), "stop_loss": round(sl, 3)}


def pips_for(bias, entry, price):
    diff = (entry - price) if bias == "SELL" else (price - entry)
    return round(diff * 100, 1)


def load_log(base_dir):
    path = os.path.join(base_dir, "experiment_h1h4_log.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("by_threshold"), dict):
            raise ValueError("形式不正")
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, AttributeError):
        return {"by_threshold": {str(th): {"trades": []} for th in THRESHOLDS_PIPS}}


def compute_stats(trades):
    closed = [t for t in trades if t.get("status") in ("WIN", "LOSS")]
    wins = [t for t in closed if t["status"] == "WIN"]
    losses = [t for t in closed if t["status"] == "LOSS"]
    gross_win = sum(t["pips"] for t in wins)
    gross_loss = abs(sum(t["pips"] for t in losses))
    return {
        "total_closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "total_pips": round(sum(t["pips"] for t in closed), 1) if closed else 0.0,
        "avg_pips_per_trade": round(sum(t["pips"] for t in closed) / len(closed), 1) if closed else None,
    }


def update_one_threshold(bucket, bias, latest_price, order_plan, now_iso):
    trades = bucket.get("trades", [])
    open_trade = trades[-1] if trades and trades[-1].get("status") == "OPEN" else None

    if open_trade is not None:
        ob = open_trade["bias"]
        tp, sl = open_trade["take_profit"], open_trade["stop_loss"]
        hit_tp = (latest_price <= tp) if ob == "SELL" else (latest_price >= tp)
        hit_sl = (latest_price >= sl) if ob == "SELL" else (latest_price <= sl)
        if hit_tp or hit_sl:
            open_trade["status"] = "WIN" if hit_tp else "LOSS"
            open_trade["closed_at_utc"] = now_iso
            open_trade["pips"] = pips_for(ob, open_trade["entry"], latest_price)
            open_trade = None

    if open_trade is None and bias in ("SELL", "BUY") and order_plan:
        trades.append({
            "opened_at_utc": now_iso, "bias": bias,
            "entry": order_plan["entry"], "take_profit": order_plan["take_profit"], "stop_loss": order_plan["stop_loss"],
            "status": "OPEN", "closed_at_utc": None, "pips": None,
        })

    bucket["trades"] = trades
    bucket["stats"] = compute_stats(trades)
    return bucket


def main():
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    now = datetime.now(timezone.utc)

    try:
        m5 = cs.fetch_fx_intraday(cs.SYMBOL, "5m", "5d")
        h1 = cs.fetch_fx_intraday(cs.SYMBOL, "60m", "60d")
        h4 = aggregate_to_4h(h1)

        log = load_log(base_dir)
        by_threshold = log.setdefault("by_threshold", {})

        for th in THRESHOLDS_PIPS:
            key = str(th)
            bucket = by_threshold.setdefault(key, {"trades": []})
            bias, latest_price, order_plan = compute_bias_for_threshold(m5, h1, h4, th)
            if latest_price is None:
                latest_price = m5[-1]["c"] if m5 else None
            by_threshold[key] = update_one_threshold(bucket, bias, latest_price, order_plan, now.isoformat())

        log["updated_at_utc"] = now.isoformat()
        with open(os.path.join(base_dir, "experiment_h1h4_log.json"), "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
        print("experiment_h1h4_log.json 更新完了")
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] 実験ログの更新に失敗しました（本体には影響しません）: {e}", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
