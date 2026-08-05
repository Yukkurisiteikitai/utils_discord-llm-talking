# Next-thread handoff prompt

Paste the block below into a fresh Claude Code session to continue the work.

---

```
Continuation of work on the Discord "phone call" bot (/Users/yuuto/learn_lab/discord_caller).
Goal of THIS thread: run a REAL call, collect logs/turns.jsonl that actually exercises Smart Turn
(i.e. probs in the mid-range, not just 0.98), and tune SMART_TURN_THRESHOLD against that data.

# Read first (in this order)
1. HANDOFF.md — full history. Especially the LAST section
   "追記 (2026-08-05 その2): 初の実通話・計装追加・コールドスタート起因の遅延を特定/修正".
2. config.py ("Smart Turn v3" block + VAD block), pipeline/turn_detector.py,
   and the _should_continue / awaiting_continuation logic in audio/sink.py.
3. pipeline/metrics.py (TurnRecord now has smart_turn_prob / continuation_count).

# Current state (done in the previous thread)
- INSTRUMENTATION DONE: turns.jsonl now logs `smart_turn_prob` and `continuation_count` per turn
  (null / 0 when Smart Turn is OFF). This is the basis for quantitative threshold tuning.
- COLD-START BUG FOUND & FIXED: the first turn of a session had 7–14s STT because the models sit
  idle between bot-load and the first /call-me, and on this 8GB Mac the weights get evicted (measured:
  30s idle→0.47s, 60s→1.28s, 90s→2.21s). That slow first response arrived out-of-order and felt like
  "delayed similar audio". Fix: _begin_call now re-warms STT/LLM/SmartTurn IN PARALLEL with the greeting
  TTS (call_flow._warm_up_models). Verified: post-warmup first transcribe ~0.26s. Smart Turn was NOT the
  cause of the delay (its probs were all 0.97–0.99, continuation_count=0).
- Smart Turn v3.2 is implemented & unit-verified but STILL default-OFF in config.py, and has NOT yet
  produced a clean tuning dataset (every observed prob was 0.97+, so threshold 0.5 never triggered a
  continuation — early-cut prevention never actually fired).
- Model models/smart-turn-v3.2-cpu.onnx is present locally (git-ignored; see models/README.md).
- WARP/SSL already handled in bot.py via truststore. If a new SSL error appears, suspect the WARP
  pattern ("curl works but Python fails").

# Setup before the call
1. Launch VOICEVOX (headless is fastest):
   nohup "/Applications/VOICEVOX.app/Contents/Resources/vv-engine/run" --host 127.0.0.1 --port 50021 &
   then confirm: curl -s http://127.0.0.1:50021/version
2. .venv/bin/python scripts/voice_doctor.py  (opus / davey / VOICEVOX / bot perms)
3. Set SMART_TURN_ENABLED=True in config.py (keep METRICS_ENABLED / LOG_LATENCY = True).
4. Run UNBUFFERED so [sink]/[latency] logs are visible live (stdout is block-buffered otherwise):
   PYTHONUNBUFFERED=1 python3 bot.py    (wait for "モデルのロード完了")

# The actual task (was NOT completed last thread)
1. Trigger /call-me and have a real conversation. To get useful tuning data you MUST produce turns where
   Smart Turn prob lands in the middle (0.3–0.7), i.e. genuine mid-sentence endings:
   - Speak a clause, then leave a CLEAR 0.5–1.0s pause, then continue (e.g. 「あのね……〔0.7s〕……昨日の話」).
     NOTE: pauses < VAD_MIN_SILENCE_MS (300ms) never fire a VAD "end", so Smart Turn never runs — the gap
     must be clearly longer than 300ms to reach the Smart Turn decision point.
   - Also do clean sentence endings, and fast vs slow speech, for contrast.
2. First capture a baseline with SMART_TURN_ENABLED=False (silence-timer), then the same patterns with True.
3. Analyze logs/turns.jsonl: separate OFF rows (smart_turn_prob=null) from ON rows. Split turns into
   "cut off mid-sentence (early cut)" vs "made to wait too long". Use response_gap_ms, utterance_ms,
   n_sentences, stt_text, smart_turn_prob, continuation_count, ended_at_max.
4. Tune SMART_TURN_THRESHOLD: HIGHER = more likely "still speaking" = waits longer; lower = cuts sooner.
   Look at the prob distribution of early-cut turns vs clean-ending turns to pick the number. Watch its
   interaction with SMART_TURN_MAX_MS (continuation safety valve) and VAD_MIN_SILENCE_MS.

# Separate issues noted (NOT part of threshold tuning — decide later)
- LLM turn-to-turn repetition: the 1.5B model returns the same generic fallback
  ("わかりました。お手伝いできることが…") to garbage STT input across turns (repetition_penalty only
  works within one generation). Candidates: dedupe vs last reply / suppress that fallback / prompt / bigger model.
- No barge-in: new user speech doesn't cancel the previous turn's in-flight generation / playback queue.
  Mostly masked by the warmup fix, but needed if the user talks over the AI.

# Important notes
- All features are config opt-in and default-OFF; the OFF path is byte-identical to the old behavior.
- Do not commit until explicitly told. This project commits directly on the `main` branch (the older
  handoff said "master" but the real branch is main).
- logs/turns.jsonl is git-ignored; it already contains previous baseline/ON experimental rows.

Reference only: OSS comparison is in ../oss_ref/COMPARISON.md; upstream Smart Turn (BSD-2) is ../oss_ref/pipecat.
```
