# Building with Telnyx Inference via OpenCode

## Setup (what to show on demo day)
```bash
opencode plugin @telnyx/opencode
opencode auth login --provider telnyx --method "API Key"
opencode auth list                       # shows the telnyx credential
opencode run --model 'telnyx/moonshotai/Kimi-K3' 'Say hello in one sentence.'
opencode                                 # TUI -> /telnyx to see enabled models
```
`opencode.jsonc` at the repo root enables the plugin and sets the default model. `AGENTS.md` gives the agent the repo rules (keep emulator and actor in sync, KV key format, run tests before finishing).

## Model choice (fill in from your own experience)
| Task | Model | Why / how it went |
|---|---|---|
| Scaffolding, refactors, tests | Kimi-K3 | |
| Quick edits, commit messages | GLM-5.x (small_model) | |
| Second opinion on a tricky bug | DeepSeek-V4 / Qwen3.x | |

## Log (keep it honest - interviewers like specifics)
- What worked well:
- Where the model struggled (e.g. inventing Telnyx API fields; fix: paste the doc page into context):
- Prompts that worked:
- Latency and cost impressions:
