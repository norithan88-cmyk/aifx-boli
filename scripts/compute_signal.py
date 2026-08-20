#!/usr/bin/env python3
"""
AI FX研究所（ボリンジャーバンド版、aifx.ink）- 本日のAIシグナル 自動計算スクリプト

やっていること（概要）:
  1. Yahoo Finance の非公式チャートAPI（無料・キー不要）からUSD/JPYの価格
     （5分・60分足）を取得する。4時間足は60分足を4本ずつ集計して自前で作る。
  2. 1時間・4時間足の2つでボリンジャーバンド（SMA±2σ）を計算し、2つとも
     「バンド中心から±1.3σ以上その方向に偏っている」状態(momentum_direction)が
     一致した時だけ、上位足の方向（押し目買い/戻り売りの候補方向）を確定する。
  3. その方向候補が確定している時だけ、5分足がその方向に逆行してバンド際まで
     達し、そこから3pips以上戻り始めたタイミング(detect_reversal_setup)を検出し、
     検出できた瞬間だけ実際のSELL/BUYシグナルとして確定する（それ以外はWAIT）。
  4. Entry = 直近5分足終値。TP/SLは、5分足の逆行の谷/山（測定値幅）を基準に算出する。
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

時間足構成の変更履歴（2026-08-20）:
  - 当初は「5分・15分・1時間足の3つ一致＋1分足の反発」（LOOKBACK=15）を採用して
    いたが、3つの時間足を同時に一致させる条件が厳しすぎて、実運用でSELL/BUYが
    ほとんど出ない状態が続いた。
  - 「上位足の一致条件を増やすほどシグナル頻度は下がる」という一般的な傾向を踏まえ、
    一致条件を1時間・4時間足の2つに緩め、反発トリガーを1分足→5分足に変更した
    「1時間・4時間足一致＋5分足の反発」パターンで再検証したところ、19ヶ月・
    USD/JPY実データで343件・月平均17.9件・勝率77.8%・PF2.58・合計+2,501pips
    （BUY220件PF3.10／SELL123件PF2.13、両方向とも安定してプラス）という、
    十分な頻度と良好な成績を確認できたため、本番ロジックをこちらに切り替えた。
  - 反発判定・損切り幅の閾値(REVERT_MIN_PIPS/SL_BUFFER_PIPS)は3〜20pipsで比較し、
    3pips版が総獲得pips・頻度のバランスで最良だったため採用（詳細は
    experiment_h1h4_trigger.py の閾値別フォワード検証ログを参照）。
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
SL_BUFFER_PIPS = 3.0
WATCH_MIN_PIPS = 1.0  # 「もうすぐ来るかも」の事前警告(pre_alert)を出す反発量のしきい値


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


def aggregate_to_4h(hourly_bars):
    """60分足を4本ずつ(UTC基準、4時間境界)まとめて4時間足を作る。"""
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


def detect_watch_setup(bars, ch, direction):
    """
    正式シグナル(3pips反発)にはまだ届いていないが、バンド際まで達して反発し始めている
    (WATCH_MIN_PIPS以上戻っている)状態を検出する。「もうすぐ来るかも」の事前警告用。
    正式シグナルが出ている時にはこちらは呼ばない想定(bias=="WAIT"の時だけ使う)。
    """
    if len(bars) < REVERT_WINDOW:
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
        reverted_pips = (latest - trough) * 100
        if reverted_pips < WATCH_MIN_PIPS:
            return None
        return {"extreme": trough, "reverted_pips": round(reverted_pips, 1)}
    else:
        peak_idx = max(range(len(closes)), key=lambda i: closes[i])
        peak = closes[peak_idx]
        if peak_idx == len(closes) - 1:
            return None
        if (peak - mid) / sigma < EDGE_THRESHOLD:
            return None
        reverted_pips = (peak - latest) * 100
        if reverted_pips < WATCH_MIN_PIPS:
            return None
        return {"extreme": peak, "reverted_pips": round(reverted_pips, 1)}


def build_confidence_breakdown(bias, candidate, timeframes, confidence):
    tf_line = " / ".join(f"{tf['label']}:{MOMENTUM_LABEL_JA[tf['momentum']]}" for tf in timeframes)
    if bias in ("SELL", "BUY"):
        align_note = tf_line + " → 2時間足とも一致、5分足の反発シグナルも確認済み"
        calc_note = f"基本50% + 2時間足一致30% + バンド際からの乖離度ボーナス = {confidence}%（上限95%）"
    elif candidate is not None:
        align_note = tf_line + " → 2時間足は一致していますが、5分足の反発シグナルはまだ点灯していません"
        calc_note = "2時間足の方向一致のみでは確信度は上がらず、5分足の反発確認まで基本値50%のままです。"
    else:
        align_note = tf_line + " → 2時間足の方向が一致していません"
        calc_note = "2時間足の方向が揃っていないため、基本値50%のままです。"
    return {"timeframes_note": align_note, "calc_note": calc_note}


def build_market_context(bias, candidate, latest_price, day_change_pct):
    change_txt = f"{day_change_pct:+.2f}%"
    if bias == "SELL":
        stance = "1時間・4時間足が揃って上値の重さを示す中、5分足が短期的な戻りから反落したタイミング"
        outlook = "目先は上値の重い展開が想定され、高値を追わず戻りを待つスタンスが機能しやすい局面。"
    elif bias == "BUY":
        stance = "1時間・4時間足が揃って下値の堅さを示す中、5分足が短期的な押し目から反発したタイミング"
        outlook = "目先は下値の堅い展開が想定され、押し目を焦らず拾うスタンスが機能しやすい局面。"
    elif candidate == "SELL":
        stance = "1時間・4時間足は戻り売り方向で揃っているが、5分足の反落シグナルはまだ点灯していない"
        outlook = "上位足の方向感は出ているため、5分足が戻り高値から反落するタイミングを待ちたい局面。"
    elif candidate == "BUY":
        stance = "1時間・4時間足は押し目買い方向で揃っているが、5分足の反発シグナルはまだ点灯していない"
        outlook = "上位足の方向感は出ているため、5分足が押し目安値から反発するタイミングを待ちたい局面。"
    else:
        stance = "1時間・4時間足の方向が揃っておらず、方向感に乏しいレンジ地合い"
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

    m5 = fetch_fx_intraday(SYMBOL, "5m", "60d")
    h1 = fetch_fx_intraday(SYMBOL, "60m", "2y")
    h4 = aggregate_to_4h(h1)

    ch_5m = bollinger_channel([b["c"] for b in m5])
    ch_1h = bollinger_channel([b["c"] for b in h1])
    ch_4h = bollinger_channel([b["c"] for b in h4])

    timeframes = [
        {"label": "1時間足", "key": "h1", "channel": ch_1h},
        {"label": "4時間足", "key": "h4", "channel": ch_4h},
    ]
    for tf in timeframes:
        tf["momentum"] = momentum_direction(tf["channel"])

    dirs = [tf["momentum"] for tf in timeframes]
    if dirs[0] == "UP" and dirs[1] == "UP":
        candidate = "BUY"
    elif dirs[0] == "DOWN" and dirs[1] == "DOWN":
        candidate = "SELL"
    else:
        candidate = None

    extreme = detect_reversal_setup(m5, ch_5m, candidate) if candidate else None
    bias = candidate if (candidate and extreme is not None) else "WAIT"

    pre_alert = None
    if bias == "WAIT" and candidate is not None:
        watch = detect_watch_setup(m5, ch_5m, candidate)
        if watch:
            pre_alert = {
                "direction": candidate,
                "reverted_pips": watch["reverted_pips"],
                "needed_pips": REVERT_MIN_PIPS,
                "note": (
                    f"{'押し目買い' if candidate == 'BUY' else '戻り売り'}方向の反発が始まっています"
                    f"（{watch['reverted_pips']}pips反発／確定には{REVERT_MIN_PIPS:.0f}pips必要）。"
                    "このまま反発が続けば、まもなくシグナルが確定する可能性があります。"
                ),
            }

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
        market_mode_note = "1時間・4時間足の方向が揃っており、方向感のある地合い。"
    else:
        market_mode = "RANGE"
        market_mode_note = "時間足ごとに方向が割れており、方向感に乏しいレンジ地合い。"

    latest_price = m5[-1]["c"] if m5 else ch_1h["latest"]
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
        trade_lead = "戻り売り ― 上位足の下降方向一致＋5分足の戻りからの反落"
    elif bias == "BUY":
        entry = latest_price
        move = abs(entry - extreme)
        sl = extreme - SL_BUFFER_PIPS / 100
        tp = entry + move
        trade_lead = "押し目買い ― 上位足の上昇方向一致＋5分足の押し目からの反発"
    else:
        entry = tp = sl = None
        if candidate == "SELL":
            trade_lead = "様子見 ― 上位足は戻り売り方向で一致、5分足の反落シグナル待ち"
        elif candidate == "BUY":
            trade_lead = "様子見 ― 上位足は押し目買い方向で一致、5分足の反発シグナル待ち"
        else:
            trade_lead = "様子見 ― 1時間・4時間足の方向が一致していない"

    reversal_setup = None
    if bias in ("SELL", "BUY"):
        reverted = round((entry - extreme) * 100, 1) if bias == "BUY" else round((extreme - entry) * 100, 1)
        reversal_setup = {"extreme": round(extreme, 3), "reverted_pips": reverted}

    comments = {
        "SELL": ["強い相場ほど、飛び乗らない。戻りを丁寧に売る一日に。", "上値は重い。高値づかみを避け、戻り待ちに徹する。"],
        "BUY": ["押し目は焦らず拾う。飛び乗りより、待つ勇気を。", "下値は堅い。押し目待ちで、無理な高値追いはしない。"],
        "WAIT": ["方向感のない日は、休むも相場。無理に取りにいかない。", "5分足のタイミングを待つのが賢明。"],
    }
    if bias in ("SELL", "BUY"):
        commentary = comments[bias][0]
    elif candidate == "BUY":
        commentary = "上位足は上向き。焦らず、5分足の押し目からの反発を待つ。"
    elif candidate == "SELL":
        commentary = "上位足は下向き。焦らず、5分足の戻りからの反落を待つ。"
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
        "pre_alert": pre_alert,
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
            "現在のロジック「1時間・4時間足の方向一致＋5分足の反発」（LOOKBACK=15）は、"
            "19ヶ月分のUSD/JPY実データ（2025年1月〜2026年7月）で343件・勝率77.8%・"
            "プロフィットファクター2.58・合計+2,501pips（BUY220件PF3.10／SELL123件PF2.13、"
            "両方向とも安定してプラス）という結果でしたが、実際の運用成績はこれと異なる"
            "可能性があります。"
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
