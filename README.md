# WeChat Bot

Source for an AstrBot-based WeChat group assistant connected to Sub2API.

The bot supports general question answering, public web research, League of Legends knowledge, read-only service diagnostics, reminders, usage summaries, and investment research. Runtime credentials, group bindings, and deployment evidence are supplied separately and are not part of this repository.

Run the offline test suite with:

```sh
python3 -B -m unittest discover -s tests
```

The Native WeChat adapter under `vendor/astrbot_plugin_wechat_native` is vendored from its upstream project; its license is included with the source. Use `config/policy.example.json` as a template. The runtime `config/policy.json` is ignored and must be supplied separately.
