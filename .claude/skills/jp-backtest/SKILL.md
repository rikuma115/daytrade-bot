---
name: jp-backtest
description: 日本株の5分足でデイトレード戦略をバックテストし、ボットと同じ売買になるかを確認する。「この戦略を試して」「バックテストして」「ルールを変えたらどうなる」「確率を見て」と言われたときに使う。
argument-hint: "[戦略名] [変更したいルール]"
---

# jp-backtest

戦略の追加や変更は、必ずこの手順で検証してからボットに入れる。

## 手順
1. データを更新する（直近60日分。古い分は data/ に貯まっていく）。
   ```bash
   python3 data.py
   ```
2. 新しい戦略は `backtest.py` に、既存の戦略と同じ形（`prepare` / `entry_signal` / `exit_signal`、必要なら `trail`）のクラスとして追加し、`STRATEGIES` に登録する。ボットはこのクラスをそのまま使う。
3. 検証する。コストは標準と厳しめの両方で見る。
   ```bash
   python3 backtest.py --strategy <名前> --long-only
   python3 backtest.py --strategy <名前> --long-only --slippage 0.001
   ```
4. ボットが同じ売買をするか確かめる（数分かかる）。
   ```bash
   python3 tests/test_equivalence.py <名前>
   ```
5. 実際の設定（資金、損失の上限）で全日を再生し、要約を見る。
   ```bash
   python3 -m bot.run replay --all > /dev/null && python3 -m bot.review --mode replay --days 60
   ```
6. 1つの注文のリスクを見たいときはタイムマシンを使う。
   ```bash
   python3 -m bot.timemachine 9432.T --date 2026-11-06 --time 09:35 --entry 163.4 --stop 0.008 --trail 0.01
   ```

## 判断の基準
- 採用の目安: コスト控除後の期間リターンがプラス、かつ「前半」と「後半」の両方でプラス、かつ取引30回以上。
- 結果を見てから条件を足すと、たまたま合っただけの戦略になる。条件やパラメータは検証の前に決め、何通り試したかを記録して伝える。
- タイムマシンの確率は、値動きがランダムだと仮定した見積もり。勝てる根拠としては使わない。
- 出力は表の要約だけを読み、取引明細の CSV 全体は読まない（トークンの節約）。
