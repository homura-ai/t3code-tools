# board-relay

Agents post cards on a self-hosted Wekan board. When a human comments on a
card, `board-replies.py` sends the comment to the T3 Code thread of the agent
named in the card title (`<agent name>: <matter>`). Each comment is delivered
once, even across restarts. Delivered ids are saved atomically, and only after
a successful send. If delivery fails, the relay comments on the card, keeps
retrying, and comments again once it has delivered. Cards with no name prefix
get one comment explaining the format. With no state file a real run refuses to
start: run `--seed` once first, which marks every comment and unprefixed card already
on the board as handled and sends nothing, or start with `--deliver-existing` to send
them all.

Also here: Podman quadlets for Wekan + MongoDB + a Caddy proxy (`wekan/`), and
agent rules for using the board (`BOARD-RULES.md`). The proxy adds the agents'
API token, so agents need no login, and it has a "who are you?" picker with
auto-login pages for humans.

## Status

- `board-replies.py`: **working.** The same code (apart from the config
  loading) has run as a service for days, and `--dry-run` against that live
  board read it and exited 0 without touching its state. `python3 board-replies/test_board_replies.py` passes 8
  offline tests against a fake Wekan API and a fake `t3`: dry run, delivery,
  delivered only once, failure notice then recovery notice, config checks,
  refusing to start without state, `--seed` then only new comments, and a bad
  `card_url`.
- `wekan/`: **working in its original setup, untested as edited here.** The
  placeholders (ROOT_URL, users, the published address) replaced real values,
  and the edited files have not been started.
- `board-setup/board_setup.py`: builds a new Wekan board (lists, labels,
  custom fields, card and view settings, rules, webhooks, template cards) from
  a JSON config, or prints a read-only inventory of an existing board. Writes
  are refused anywhere except the board the run itself created.
  `python3 board-setup/test_board_setup.py` passes 8 offline tests against a
  fake Wekan API.

## Dependencies

Python 3.9+ (standard library only), the `t3` CLI from T3 Code, and a Wekan
board with the API enabled. For `wekan/`: Podman with quadlet support and a
systemd user session.

## Install

If you are replacing an older install at these paths, write the config before
replacing either file, or the running relay exits for lack of one. Keep
`state_dir` where the old state is, and don't `--seed` there: the existing
state already holds the delivered list.

```bash
install -m 755 board-replies/board-replies.py ~/.local/bin/board-replies.py
mkdir -p ~/.config/board-relay
cp board-replies/config.example.json ~/.config/board-relay/config.json   # edit: board, agents_user, project
board-replies.py --dry-run          # one read-only pass: shows what it would send
board-replies.py --seed             # once: mark what is already on the board as handled
cp board-replies/board-replies.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now board-replies.service
```

Config keys are listed at the top of `board-replies.py`. `lists` may be empty,
which means every list on the board. If the API needs a token directly (no
proxy), set `token_file`. Thread lookup tries `<project>/threads.tsv`
(`name<TAB>threadId`, last line wins) first, then the newest T3 thread in
`project` whose title is the name or starts with `<name>:`, `<name> ` or
`You are <name>.`.

Wekan: copy `wekan/quadlet/*` to `~/.config/containers/systemd/`, and the
Caddyfile and `site/` to `~/.local/share/wekan/proxy/`. Make one
`<user>.html` per person from `site/as-user.html.template` and add a matching
`handle` in the Caddyfile and a link in `pick.html`. Put
`AGENTS_TOKEN=...` in `~/.local/share/wekan/proxy/env` (mode 600). Set
ROOT_URL, then run `systemctl --user daemon-reload && systemctl --user start wekan-pod`.

Board setup:

```bash
python3 board-setup/board_setup.py check --config board-setup/board.example.json   # offline
python3 board-setup/board_setup.py create --config my-board.json --dry-run
python3 board-setup/board_setup.py inventory <board id>
```

## Uninstall

```bash
systemctl --user disable --now board-replies.service
rm ~/.config/systemd/user/board-replies.service ~/.local/bin/board-replies.py
systemctl --user daemon-reload
# Optional: rm -r ~/.config/board-relay ~/.local/state/board-replies
# Wekan: systemctl --user stop wekan-pod; remove the quadlets; data is in ~/.local/share/wekan
```

## Limits

- It polls every card and its comments every 5 s. That is fine for tens of
  cards but costly for thousands.
- The auto-login pages hold user passwords in plain text and the proxy gives
  any API caller the agents' identity. Only expose the board on a private
  network (localhost or a tailnet).
- Messages are queued with `t3 thread send --mode queue`. If T3 is down, they
  are retried every `retry_seconds` and never dropped.
