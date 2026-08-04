# models/

Smart Turn(意味的な終話検出, `config.SMART_TURN_ENABLED=True` のとき使用)の ONNX
モデルを置く場所。モデル本体はサイズが大きい(約8.7MB)ため **Git 管理外**
(`.gitignore` で `models/*.onnx` を除外)。使う場合は下記の手順でここに配置する。

`SMART_TURN_ENABLED=False`(既定)なら、このモデルは無くても通話は動く。

## 必要ファイル

| ファイル | 用途 | サイズ | sha256 |
|---|---|---|---|
| `smart-turn-v3.2-cpu.onnx` | Smart Turn v3.2(CPU向け) | 8,679,182 bytes | `2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f` |

配置先: `models/smart-turn-v3.2-cpu.onnx`
(別の場所に置く場合は `config.SMART_TURN_MODEL_PATH` を変更)

## 入手方法

このモデルは **pipecat**(BSD-2-Clause, Daily)に同梱されているものを使う。
特徴量抽出 `pipeline/_whisper_features.py` も同じ pipecat 由来(BSD-2)。

### 方法A(推奨): pip の pipecat パッケージから取り出す

pipecat 本体を常用しないなら、モデルの取り出しだけに使って後で消してよい。

```bash
python -m pip install --no-deps pipecat-ai
python - <<'PY'
import shutil
from importlib import resources
src = resources.files("pipecat.audio.turn.smart_turn.data").joinpath("smart-turn-v3.2-cpu.onnx")
shutil.copy(str(src), "models/smart-turn-v3.2-cpu.onnx")
print("copied ->", "models/smart-turn-v3.2-cpu.onnx")
PY
```

### 方法B: pipecat リポジトリから直接コピー

```bash
git clone --depth 1 https://github.com/pipecat-ai/pipecat.git
cp pipecat/src/pipecat/audio/turn/smart_turn/data/smart-turn-v3.2-cpu.onnx models/
```

(この実装は pipecat commit `972f570` の同モデルで検証済み)

### 参考: HuggingFace

上流モデルは HuggingFace の `pipecat-ai/smart-turn-v3` で公開されている。
ただし配布ファイル名/版が変わることがあるので、確実なのは上記A/B。

## 配置後の確認

```bash
shasum -a 256 models/smart-turn-v3.2-cpu.onnx
# => 2bb026316b14a660486a75b1733cd3fbab8c2fd0314dc9af7be49f8cca967e4f
```

その後 `config.py` で `SMART_TURN_ENABLED = True` にすると有効になる。
