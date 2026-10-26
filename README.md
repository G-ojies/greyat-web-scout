# GreYat Web Scout

A Match 4 entry for the AMD AI League on lablab.ai (Intelligent Web Information Retrieval). It takes a natural-language question, with an optional start URL, and returns one exact value together with the page it came from.

## How it works

1. **Plan.** A language model (Qwen3-4B-Instruct, served on the AMD GPU by a resident process so each query skips the load time) plans the lookup: search queries, likely official URLs, and package or project names.
2. **Search.** In parallel the agent queries several search engines and Wikipedia. For "latest version" questions it reads the authoritative registries directly (PyPI, npm, GitHub releases, endoflife.date).
3. **Read.** It fetches the best pages and keeps the passages that match the question.
4. **Extract.** The model picks a single value and says which passage it came from.

**Grounding comes first.** An answer is only returned if the value appears verbatim on the cited page, so the source URL always backs up the answer. If no page confirms it, the agent returns an empty answer instead of a guess, and answers to "latest" questions are never taken from the model's memory.

Everything runs locally on AMD ROCm with no external API keys.

## Layout

```
match4/app/app.py          entry point (one question per process)
match4/app/agent/          pipeline, search, resolvers, model server
match4/tests/dev.json      30-question dev set (facts, ports, people, dates, live versions)
match4/tests/run_dev.py    runs a question file the way the harness does
```

## Usage

```
python3 match4/app/app.py --serve &                      # load the model, answer over a unix socket
python3 match4/app/app.py --query-id q01 --query "Which year was the Python Software Foundation founded?"
```

Each query writes `/app/output/<query-id>_output.json`:

```json
{"answer": "2001", "citations": ["https://en.wikipedia.org/wiki/Python_Software_Foundation"], "source_url": "https://en.wikipedia.org/wiki/Python_Software_Foundation", "confidence": 0.9}
```

An empty answer is written first, so a crash or a timeout still leaves a well-formed file.

## Results

On the 30-question dev set the pipeline answered 26 correctly. That run used a hosted stand-in model, because no GPU time was available.
