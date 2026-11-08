# daytrade-bot

日本株デイトレード自動化の準備段階：戦略のバックテスト。

## 使い方

```bash
pip install yfinance pandas numpy
python3 data.py                       # 20銘柄の5分足を取得（直近60日、data/ に保存）
python3 backtest.py                   # 3戦略 × 買いのみ / 買い+売り
python3 backtest.py --slippage 0.001  # コストを厳しめにして再検証
python3 backtest.py --long-only       # 現物口座（空売りなし）想定
python3 backtest.py --save            # 取引明細を results/ に保存
```

`data.py` を定期的に実行すると、60日より古いデータも data/ に貯まっていく。

## バックテストの前提
- シグナルは足の確定後に判断し、次の足の始値で約定
- 同じ足で損切りと利確の両方に届いた場合は損切り扱い（保守的）
- 15:20 に必ず決済（持ち越しなし）
- コスト: スリッページ 片道0.05%、手数料0%（信用の手数料無料コースを想定）

## 戦略
| キー | 内容 |
|---|---|
| orb  | 9:00〜9:30 の高値・安値のブレイク。損切りはレンジの反対側、利確は損切り幅の2倍 |
| vwap | 上昇中の VWAP への押し目で買い（下降中は戻り売り）。損切り0.6%、利確1.2% |
| ema  | 5分足 EMA9/26 のクロス。損切り0.7%、反対のクロスで決済 |

## 改善版（trend / trend_novwap）
EMA9/26 のクロスで1日1回だけ入り、損切り0.8%、トレーリング1.0%、利確の上限なし、15:20 に決済。

## 結果ページ
```bash
python3 backtest.py --save && python3 make_report.py   # report/ を更新
```
report/index.html: 成績表、資産推移、1日ごとの売買チャート（data.json と bars/ を読み込む）

## TradingView
pine/daytrade_trend.pine を Pine エディタに貼り付けて、日本株の5分足チャートに追加する。

## 自動売買ボット（bot/）
設定は `config.toml`。お金に関わる上限（1回の損失、1日の損失、取引回数、同時保有数）は全てここで決める。

```bash
python3 -m bot.run replay --date 2026-11-06   # 過去の1日を再生
python3 -m bot.run replay --all               # 全日を再生
python3 -m bot.review --mode replay --days 60 # 成績の要約
python3 -m bot.run live                       # 当日、5分ごとに動く（mode=paper なら発注しない）
python3 tests/test_equivalence.py trend       # ボットとバックテストの売買が一致するか
```

- 日中は Claude を呼ばない（ルールだけで動く）。Claude は `/jp-backtest` と `/bot-review` のときだけ使う
- 損切りは本番では証券会社側の逆指値に置く（ボットが止まっても損切りは実行される）
- 時計でも 15:20 の決済を確認し、引け後に建玉が残っていたら警告する
- 通知: 環境変数 `DISCORD_WEBHOOK_URL` に Discord の Webhook URL を入れる
- 記録: `logs/<mode>/trades.csv`（取引）、`logs/<mode>/events-<日付>.jsonl`（全ての判断）
- 本番の発注（`TachibanaBroker`）は、立花証券 e支店の口座開設後に実装する

サーバーでの起動例（平日 8:55、cron）:
```
55 8 * * 1-5 cd /path/to/daytrade-bot && python3 -m bot.run live >> logs/cron.log 2>&1
```

## GitHub での自動実行
- `.github/workflows/bot.yml`: 平日 8:35 と 12:05（日本時間）に起動し、仮想の売買を前場・後場に分けて実行。状態と記録はリポジトリに保存される
- `.github/workflows/experiment.yml`: Claude を使う実験を手動で実行（Actions → experiment → Run workflow）
- 秘密の値（Settings → Secrets and variables → Actions）:
  - `NTFY_TOPIC`: 通知のトピック名
  - `ANTHROPIC_API_KEY`: 実験で使う Claude API のキー

## 実験（experiments/）
- `exp1_signals.py`: ボットのシグナルに乗るか見送るかを Claude が判断すると成績は上がるか
- `exp2_disclosures.py`: 前日引け後〜朝の開示を Claude が読んで銘柄を選ぶと勝てるか
- どちらも合格基準をファイルの先頭に、結果を見る前に書いてある
