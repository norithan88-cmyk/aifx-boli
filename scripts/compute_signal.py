#!/usr/bin/env python3
"""
AI FX研究所（ボリンジャーバンド版、aifx.ink）- 本日のAIシグナル 自動計算スクリプト

やっていること（概要）:
  1. Yahoo Finance の非公式チャートAPI（無料・キー不要）からUSD/JPYの価格
     （1分・5分・15分・1時間足）を取得する。
  2. 5分・15分・1時間足の3つでボリンジャーバンド（SMA±2σ）を計算し、3つとも
     「バンド中心から±1.3σ以上その方向に偏っている」状態(momentum_direction)が
     一致した時だけ、上位足の方向（押し目買い/戻り売りの候補方向）を確定する。
  3. その方向候補が確定している時だけ、1分足がその方向に逆行してバンド際まで
     達し、そこから戻り始めたタイミング(detect_reversal_setup)を検出し、
     検出できた瞬間だけ実際のSELL/BUYシグナルとして確定する（それ以外はWAIT）。
  4. Entry = 直近1分足終値。TP/SLは、1分足の逆行の谷/山（測定値幅）を基準に算出する。
  5. 結果を signal.json として書き出す。

姉妹サイト aifxlabo.com（線形回帰チャネル版）との違い・検証結果:
  - aifxlabo.comは「線形回帰チャネル」（回帰直線±2σ）を使っているが、この方式は
    LOOKBACKを短くするほど回帰直線の傾きが直近の反発点自体に引っ張られる
    自己参照的な問題があり、短くするほど際限なく成績が良くなり続ける
    （過去データへの過剰適合が疑われる）不自然な挙動が確認された。
  - 本サイトは「ボリンジャーバンド」（単純移動平均±2σ、傾きを持たない）を使うことで
    この問題を回避できるか検証する目的の実験サイト。実際、19ヶ月分の実データ
    （2025-01〜2026-07、USD/JPY）でLOOKBACK値を100〜10まで比較したところ、
    ボリンジャーバンド版はLOOKBACK=15付近で勝率・PFがピークを打ち、10では
    むしろ悪化する、素直な（過剰適合を疑わせない）挙動を示した。
    LOOKBACK=15: 2,986件・勝率78.2%・PF2.48（19ヶ月合計+11,226pips）。
    同条件のLOOKBACK=100でも勝率74.8%・PF2.86と、線形回帰版(LOOKBACK=100時点で
    勝率70.3%・PF1.88)を上回っている。
  - このためLOOKBACK=15を採用している。
"""

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

SYMBOL = "JPY=X"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart"

LOOKBACK = 15
EDGE_THRESHOLD = 1.3
REVERT_WINDOW = 10
REVERT_MIN_PIPS = 3.0
SL_BUFFER_PIPS = 2.0


def http_get_json(url, retries=3, wait_sec=5):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_err = None
    for _ in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.loads(res.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(wait_sec)
    raise RuntimeError(f"取得に失敗しました: {url} ({last_err})")


def fetch_fx_intraday(symbol, interval, range_):
    url = f"{YAHOO_CHART_URL}/{symbol}?interval={interval}&range={range_}"
    data = http_get_json(url)
    result = data.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"為替データが取得できませんでした: {data}")
    ts = result[0].get("timestamp") or []
    quote = result[0]["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i]
        if o is None or h is None or l is None or c is None:
            continue
        bars.append({"t": t, "o": float(o), "h": float(h), "l": float(l), "c": float(c)})
    if not bars:
        raise RuntimeError(f"為替データが空でした（interval={interval}, range={range_}）")
    return bars


def bollinger_channel(closes, lookback=LOOKBACK):
    """
    単純移動平均(SMA)±2σでバンドを作る。線形回帰チャネルと違い「傾き」を持たないため、
    直近の反発点そのものにバンドの向きが引っ張られる自己参照現象が起きにくい。
    """
    series = closes[-lookback:] if len(closes) > lookback else closes[:]
    n = len(series)
    if n < 5:
        raise RuntimeError("ボリンジャーバンド計算に必要なデータ本数が不足しています")
    mid = sum(series) / n
    sigma = statistics.pstdev(series)
    sigma = sigma if sigma > 1e-6 else 1e-6
    latest = series[-1]
    position = (latest - mid) / sigma
    return {
        "mid": mid, "upper": mid + 2 * sigma, "lower": mid - 2 * sigma,
        "sigma": sigma, "position": position, "n": n, "latest": latest,
    }


MOMENTUM_LABEL_JA = {"UP": "上方向", "DOWN": "下方向", "FLAT": "中央"}


def momentum_direction(ch):
    pos = ch["position"]
    if pos >= EDGE_THRESHOLD:
        return "UP"
    if pos <= -EDGE_THRESHOLD:
        return "DOWN"
    return "FLAT"


def detect_reversal_setup(bars, ch, direction):
    if len(bars) < REVERT_WINDOW:
        return None
    recent = bars[-REVERT_WINDOW:]
    closes = [b["c"] for b in recent]
    latest = closes[-1]
    sigma = ch["sigma"]
    mid = ch["mid"]
    if direction == "BUY":
        trough_idx = min(range(len(closes)), key=lambda i: closes[i])
        trough = closes[trough_idx]
        if trough_idx == len(closes) - 1:
            return None
        if (trough - mid) / sigma > -EDGE_THRESHOLD:
            return None
        if (latest - trough) * 100 < REVERT_MIN_PIPS:
            return None
        return trough
    else:
        peak_idx = max(range(len(closes)), key=lambda i: closes[i])
        peak = closes[peak_idx]
        if peak_idx == len(closes) - 1:
            return None
        if (peak - mid) / sigma < EDGE_THRESHOLD:
            return None
        if (peak - latest) * 100 < REVERT_MIN_PIPS:
            return None
        return peak


def build_confidence_breakdown(bias, candidate, timeframes, confidence):
    tf_line = " / ".join(f"{tf['label']}:{MOMENTUM_LABEL_JA[tf['momentum']]}" for tf in timeframes)
    if bias in ("SELL", "BUY"):
        align_note = tf_line + " → 3時間足すべて一致、1分足の反発シグナルも確認済み"
        calc_note = f"基本50% + 3時間足一致30% + バンド際からの乖離度ボーナス = {confidence}%（上限95%）"
    elif candidate is not None:
        align_note = tf_line + " → 3時間足は一致していますが、1分足の反発シグナルはまだ点灯していません"
        calc_note = "3時間足の方向一致のみでは確信度は上がらず、1分足の反発確認まで基本値50%のままです。"
    else:
        align_note = tf_line + " → 3時間足の方向が一致していません"
        calc_note = "3時間足の方向が揃っていないため、基本値50%のままです。"
    return {"timeframes_note": align_note, "calc_note": calc_note}


def build_market_context(bias, candidate, latest_price, day_change_pct):
    change_txt = f"{day_change_pct:+.2f}%"
    if bias == "SELL":
        stance = "5分・15分・1時間足が揃って上値の重さを示す中、1分足が短期的な戻りから反落したタイミング"
        outlook = "目先は上値の重い展開が想定され、高値を追わず戻りを待つスタンスが機能しやすい局面。"
    elif bias == "BUY":
        stance = "5分・15分・1時間足が揃って下値の堅さを示す中、1分足が短期的な押し目から反発したタイミング"
        outlook = "目先は下値の堅い展開が想定され、押し目を焦らず拾うスタンスが機能しやすい局面。"
    elif candidate == "SELL":
        stance = "5分・15分・1時間足は戻り売り方向で揃っているが、1分足の反落シグナルはまだ点灯していない"
        outlook = "上位足の方向感は出ているため、1分足が戻り高値から反落するタイミングを待ちたい局面。"
    elif candidate == "BUY":
        stance = "5分・15分・1時間足は押し目買い方向で揃っているが、1分足の反発シグナルはまだ点灯していない"
        outlook = "上位足の方向感は出ているため、1分足が押し目安値から反発するタイミングを待ちたい局面。"
    else:
        stance = "5分・15分・1時間足の方向が揃っておらず、方向感に乏しいレンジ地合い"
        outlook = "明確な方向一致が出るまでは、無理に取りにいかず様子見が無難な局面。"
    return (
        f"USD/JPYは現在{latest_price:.3f}円付近で推移（直近1時間比{change_txt}）。{stance}。"
        f"{outlook}"
        "※このまとめは実データから自動生成された定型解説です（ボリンジャーバンド版）。"
    )


def load_trade_log(base_dir):
    path = os.path.join(base_dir, "trade_log.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("trades"), list):
            raise ValueError("trade_log.jsonの形式が不正です")
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, AttributeError):
        return {"trades": []}


def pips_for(bias, entry, price):
    diff = (entry - price) if bias == "SELL" else (price - entry)
    return round(diff * 100, 1)


def update_trade_log(trade_log, bias, priority_trade, latest_price, confidence, now_iso):
    trades = trade_log.get("trades", [])
    open_trade = trades[-1] if trades and trades[-1].get("status") == "OPEN" else None

    if open_trade is not None:
        ob = open_trade["bias"]
        tp, sl = open_trade["take_profit"], open_trade["stop_loss"]
        hit_tp = (latest_price <= tp) if ob == "SELL" else (latest_price >= tp)
        hit_sl = (latest_price >= sl) if ob == "SELL" else (latest_price <= sl)
        if hit_tp or hit_sl:
            open_trade["status"] = "WIN" if hit_tp else "LOSS"
            open_trade["closed_at_utc"] = now_iso
            open_trade["closed_price"] = round(latest_price, 3)
            open_trade["pips"] = pips_for(ob, open_trade["entry"], latest_price)
            open_trade = None

    newly_opened = False
    if open_trade is None and bias in ("SELL", "BUY"):
        entry, tp, sl = priority_trade.get("entry"), priority_trade.get("take_profit"), priority_trade.get("stop_loss")
        if entry is not None and tp is not None and sl is not None:
            trades.append({
                "id": now_iso, "opened_at_utc": now_iso, "bias": bias,
                "entry": entry, "take_profit": tp, "stop_loss": sl, "confidence": confidence,
                "status": "OPEN", "closed_at_utc": None, "closed_price": None, "pips": None,
            })
            newly_opened = True

    trade_log["trades"] = trades
    return trade_log, newly_opened


def compute_trade_stats(trades):
    closed = [t for t in trades if t.get("status") in ("WIN", "LOSS")]
    wins = [t for t in closed if t["status"] == "WIN"]
    losses = [t for t in closed if t["status"] == "LOSS"]
    total_closed = len(closed)
    gross_win = sum(t["pips"] for t in wins)
    gross_loss = abs(sum(t["pips"] for t in losses))
    return {
        "total_closed": total_closed,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / total_closed * 100, 1) if total_closed else None,
        "avg_win_pips": round(gross_win / len(wins), 1) if wins else None,
        "avg_loss_pips": round(-gross_loss / len(losses), 1) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "total_pips": round(sum(t["pips"] for t in closed), 1) if closed else 0.0,
    }


def build_signal(out_path=None):
    now = datetime.now(timezone.utc)

    m1 = fetch_fx_intraday(SYMBOL, "1m", "5d")
    m5 = fetch_fx_intraday(SYMBOL, "5m", "5d")
    m15 = fetch_fx_intraday(SYMBOL, "15m", "5d")
    h1 = fetch_fx_intraday(SYMBOL, "60m", "60d")

    ch_1m = bollinger_channel([b["c"] for b in m1])
    ch_5m = bollinger_channel([b["c"] for b in m5])
    ch_15m = bollinger_channel([b["c"] for b in m15])
    ch_1h = bollinger_channel([b["c"] for b in h1])

    timeframes = [
        {"label": "5分足", "key": "m5", "channel": ch_5m},
        {"label": "15分足", "key": "m15", "channel": ch_15m},
        {"label": "1時間足", "key": "h1", "channel": ch_1h},
    ]
    for tf in timeframes:
        tf["momentum"] = momentum_direction(tf["channel"])

    dirs = [tf["momentum"] for tf in timeframes]
    if dirs[0] == "UP" and dirs[1] == "UP" and dirs[2] == "UP":
        candidate = "BUY"
    elif dirs[0] == "DOWN" and dirs[1] == "DOWN" and dirs[2] == "DOWN":
        candidate = "SELL"
    else:
        candidate = None

    extreme = detect_reversal_setup(m1, ch_1m, candidate) if candidate else None
    bias = candidate if (candidate and extreme is not None) else "WAIT"

    if bias in ("SELL", "BUY"):
        avg_abs_pos = sum(abs(tf["channel"]["position"]) for tf in timeframes) / len(timeframes)
        confidence = 50 + 30 + min(avg_abs_pos, 3.0) * 5
        confidence = max(50, min(95, round(confidence)))
        stars = max(1, min(5, round(confidence / 20)))
    else:
        confidence = 50
        stars = 2

    if candidate is not None:
        market_mode = "TREND"
        market_mode_note = "5分・15分・1時間足の方向が揃っており、方向感のある地合い。"
    else:
        market_mode = "RANGE"
        market_mode_note = "時間足ごとに方向が割れており、方向感に乏しいレンジ地合い。"

    latest_price = m1[-1]["c"] if m1 else ch_1h["latest"]
    day_change_pct = 0.0
    if len(h1) >= 1 and h1[-1]["o"]:
        day_change_pct = (latest_price - h1[-1]["o"]) / h1[-1]["o"] * 100

    abs_change = abs(day_change_pct)
    volatility_risk = "HIGH" if abs_change >= 0.7 else ("MID" if abs_change >= 0.3 else "LOW")

    if bias == "SELL":
        entry = latest_price
        move = abs(entry - extreme)
        sl = extreme + SL_BUFFER_PIPS / 100
        tp = entry - move
        trade_lead = "戻り売り ― 上位足の下降方向一致＋1分足の戻りからの反落"
    elif bias == "BUY":
        entry = latest_price
        move = abs(entry - extreme)
        sl = extreme - SL_BUFFER_PIPS / 100
        tp = entry + move
        trade_lead = "押し目買い ― 上位足の上昇方向一致＋1分足の押し目からの反発"
    else:
        entry = tp = sl = None
        if candidate == "SELL":
            trade_lead = "様子見 ― 上位足は戻り売り方向で一致、1分足の反落シグナル待ち"
        elif candidate == "BUY":
            trade_lead = "様子見 ― 上位足は押し目買い方向で一致、1分足の反発シグナル待ち"
        else:
            trade_lead = "様子見 ― 5分・15分・1時間足の方向が一致していない"

    reversal_setup = None
    if bias in ("SELL", "BUY"):
        reverted = round((entry - extreme) * 100, 1) if bias == "BUY" else round((extreme - entry) * 100, 1)
        reversal_setup = {"extreme": round(extreme, 3), "reverted_pips": reverted}

    comments = {
        "SELL": ["強い相場ほど、飛び乗らない。戻りを丁寧に売る一日に。", "上値は重い。高値づかみを避け、戻り待ちに徹する。"],
        "BUY": ["押し目は焦らず拾う。飛び乗りより、待つ勇気を。", "下値は堅い。押し目待ちで、無理な高値追いはしない。"],
        "WAIT": ["方向感のない日は、休むも相場。無理に取りにいかない。", "1分足のタイミングを待つのが賢明。"],
    }
    if bias in ("SELL", "BUY"):
        commentary = comments[bias][0]
    elif candidate == "BUY":
        commentary = "上位足は上向き。焦らず、1分足の押し目からの反発を待つ。"
    elif candidate == "SELL":
        commentary = "上位足は下向き。焦らず、1分足の戻りからの反落を待つ。"
    else:
        commentary = comments["WAIT"][0]

    market_context = build_market_context(bias, candidate, latest_price, day_change_pct)

    result = {
        "generated_at_utc": now.isoformat(),
        "pair": "USD/JPY（ボリンジャーバンド版）",
        "latest_price": round(latest_price, 3),
        "day_change_pct": round(day_change_pct, 2),
        "signal": {
            "bias": bias,
            "bias_label": {"SELL": "戻り売り優勢", "BUY": "押し目買い優勢", "WAIT": "方向感なし"}[bias],
            "stars": stars,
            "confidence": confidence,
            "confidence_breakdown": build_confidence_breakdown(bias, candidate, timeframes, confidence),
        },
        "volatility_risk": volatility_risk,
        "market_mode": market_mode,
        "market_mode_note": market_mode_note,
        "priority_trade": {
            "lead": trade_lead,
            "entry": round(entry, 3) if entry is not None else None,
            "take_profit": round(tp, 3) if tp is not None else None,
            "stop_loss": round(sl, 3) if sl is not None else None,
        },
        "reversal_setup": reversal_setup,
        "regression_channels": [
            {
                "key": tf["key"], "label": tf["label"],
                "position_sigma": round(tf["channel"]["position"], 2),
                "momentum": tf["momentum"],
                "mid": round(tf["channel"]["mid"], 3),
                "upper": round(tf["channel"]["upper"], 3),
                "lower": round(tf["channel"]["lower"], 3),
            }
            for tf in timeframes
        ],
        "commentary": commentary,
        "market_context": market_context,
        "disclaimer": (
            "本データはルールベースの参考情報であり、投資成果を保証するものではありません。"
            "本サイトは線形回帰チャネル版（aifxlabo.com）に対するボリンジャーバンド版の比較実験サイトです。"
            "バックテスト（2025年1月〜2026年7月、USD/JPY実データ）ではLOOKBACK=15で"
            "勝率78.2%・プロフィットファクター2.48という結果でしたが、実際の運用成績は"
            "これと異なる可能性があります。"
        ),
    }

    if out_path:
        base_dir = os.path.dirname(out_path)
        try:
            trade_log = load_trade_log(base_dir)
            trade_log, _newly_opened = update_trade_log(
                trade_log, bias, result["priority_trade"], latest_price, confidence, now.isoformat(),
            )
            trade_log["stats"] = compute_trade_stats(trade_log["trades"])
            trade_log["updated_at_utc"] = now.isoformat()
            with open(os.path.join(base_dir, "trade_log.json"), "w", encoding="utf-8") as f:
                json.dump(trade_log, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] trade_log.jsonの更新に失敗しました（シグナル本体は継続します）: {e}", file=sys.stderr)

    return result


def main():
    out_path = os.path.join(os.path.dirname(__file__), "..", "signal.json")
    out_path = os.path.abspath(out_path)
    try:
        signal = build_signal(out_path=out_path)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] シグナル計算に失敗しました: {e}", file=sys.stderr)
        sys.exit(1)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(signal, f, ensure_ascii=False, indent=2)
    print(f"書き出し完了: {out_path}")
    print(json.dumps(signal, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
