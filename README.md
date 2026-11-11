# WeChat Bot

Source for an AstrBot-based WeChat group assistant connected to Sub2API.

The bot supports general question answering, public web research, League of Legends knowledge, read-only service diagnostics, reminders, usage summaries, and investment research. Runtime credentials, group bindings, and deployment evidence are supplied separately and are not part of this repository.

Run the offline test suite with:

```sh
python3 -B -m unittest discover -s tests
```

The Native WeChat adapter under `vendor/astrbot_plugin_wechat_native` is vendored from its upstream project; its license is included with the source. Use `config/policy.example.json` as a template. The runtime `config/policy.json` is ignored and must be supplied separately.

## Source synchronization

This private GitHub repository is the durable source copy. Server-side code fixes must be brought back here, verified, committed, and pushed; planned changes should be developed from this repository. See `AGENTS.md`. Record user-facing changes in `CHANGELOG.md`.

## Investment failure diagnostics

Investment model failures retain only a normalized error code and timestamp in the existing delivery metadata for up to seven days. From the deployed source directory, operators can run `python3 admin/model_failures.py --limit 20` to read them. The output omits message text, sender IDs, and credentials.
