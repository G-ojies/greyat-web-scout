"""Language model backends.

`hf` runs a local model on the AMD GPU with transformers (the submission path; ROCm exposes the
GPU through torch's `cuda` device). `groq` calls an OpenAI-compatible API and exists only for
developing the pipeline on a machine without a GPU; the submitted image never sets a key.
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

MODEL_DIR = os.environ.get('AGENT_MODEL', '/models/qwen')
DEVICE = os.environ.get('AGENT_DEVICE', 'cuda')  # ROCm builds of torch expose the AMD GPU as 'cuda'


class HF:
    def __init__(self, path=MODEL_DIR):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(path)
        dtype = torch.bfloat16 if DEVICE == 'cuda' else torch.float32
        self.model = AutoModelForCausalLM.from_pretrained(path, dtype=dtype).to(DEVICE)
        self.model.eval()
        self.name = os.path.basename(path.rstrip('/'))

    def chat(self, system, user, max_new_tokens=200):
        msgs = [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]
        kw = {'add_generation_prompt': True, 'return_tensors': 'pt', 'return_dict': True}
        try:
            enc = self.tok.apply_chat_template(msgs, enable_thinking=False, **kw)
        except TypeError:
            enc = self.tok.apply_chat_template(msgs, **kw)
        enc = {k: v.to(DEVICE) for k, v in enc.items()}
        with self.torch.inference_mode():
            out = self.model.generate(**enc, max_new_tokens=max_new_tokens, do_sample=False,
                                      pad_token_id=self.tok.eos_token_id)
        return self.tok.decode(out[0][enc['input_ids'].shape[1]:], skip_special_tokens=True).strip()


class Groq:
    """Development stand-in only."""
    def __init__(self, model=None):
        self.key = os.environ['GROQ_API_KEY']
        self.name = model or os.environ.get('GROQ_MODEL', 'openai/gpt-oss-20b')

    def chat(self, system, user, max_new_tokens=200):
        msg = {'model': self.name, 'temperature': 0, 'max_completion_tokens': max_new_tokens,
               'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}
        if 'gpt-oss' in self.name:  # reasoning models spend extra tokens before answering
            msg.update(reasoning_effort='low', max_completion_tokens=max_new_tokens + 800)
        body = json.dumps(msg)
        req = urllib.request.Request('https://api.groq.com/openai/v1/chat/completions', data=body.encode(),
                                     headers={'Authorization': 'Bearer ' + self.key, 'Content-Type': 'application/json',
                                              'User-Agent': 'match4-dev'})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    return json.load(r)['choices'][0]['message']['content'].strip()
            except urllib.error.HTTPError as e:
                if e.code != 429 or attempt == 2:
                    raise
                time.sleep(min(6.0, float(e.headers.get('retry-after') or 2)))


def load():
    backend = os.environ.get('AGENT_BACKEND', 'hf')
    return Groq() if backend == 'groq' else HF()


def parse_json(text):
    """Pull the first JSON object out of a model reply."""
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.S)
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except ValueError:
        return {}
