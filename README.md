# hermes-systemone-tools

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that
registers `decide`, a tool for the **System 1** decision API
(`POST /v1/systemone`). The caller writes the `questions` on every call, so a
model can look at the probability and refine the question before asking again.

This plugin registers a tool only. The model-provider side (using System 1 as
the main-loop or auxiliary model) lives in
[`hermes-systemone`](https://github.com/0ppaikoa/hermes-systemone).

## Install

```bash
hermes plugins install 0ppaikoa/hermes-systemone-tools
```

Enable the `systemone` toolset once, with `hermes tools` or in `config.yaml`:

```yaml
toolsets:
  - systemone
```

## Config

The tool config lives under `plugins.entries.hermes-systemone-tools`.

```yaml
plugins:
  enabled:
    - hermes-systemone-tools
  entries:
    hermes-systemone-tools:
      endpoints:
        ollama:
          api: https://ollama.com/v1
          key_env: OLLAMA_API_KEY
      models:
        clef27:
          endpoint: ollama
          base_model: clef:27b
      default_model: clef27
```

- `endpoints` holds `api` and `key_env` only. `api` is the System 1 base URL
  (`/v1` is added when missing).
- `models` holds `endpoint` and `base_model` only. `base_model` is the id the
  endpoint serves.
- Model names are global. A dangling `endpoint` reference, an unknown key, or a
  `default_model` that names no model is rejected at load time.

## Tool: `decide`

| Argument | Meaning |
|---|---|
| `model` | A configured model name. Optional; `default_model` is used when omitted. |
| `state` | The content the questions refer to: a string, object, or array. Passed through unchanged. |
| `questions` | The raw System One questions, keyed by name. Validated before the request. |

The tool returns the API's `answers` unchanged and nothing else. The API's own
`model` and `usage` fields are not returned.

Errors are raised; Hermes dispatch turns them into `{"error": ...}`. Messages
carry the path, the expected shape, the received value and an example.

## Two schemas

- `schemas/decide.tool.schema.json` — the model-facing parameters schema. It
  shows the per-type shape (noul / choice / score) with `oneOf`.
- `schemas/decide.validate.schema.json` — the strict gate. It adds the name
  pattern, length and count limits, the both-polarity rule for noul criteria,
  no `null` criteria, and duplicate rejection for score levels. A call that
  fails here raises before any HTTP request is made.

## Scope

`noul` / `choice` / `score` only. `box` and `assets` are rejected.

## Tests

```bash
python tests/test_decide.py
```

## License

MIT — see [LICENSE](LICENSE).
