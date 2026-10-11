# Pairing with the ByteBunker app

The app reaches a rack three ways: the **engine** (chat, with the engine key),
the **monitor** (telemetry, with its token) and **rack itself** over ssh (up,
down, logs, recipes). Pairing hands it all three in one step, and nothing is
copied by hand.

## What happens

In the app: **Add a rack**, then `user@host` (the head, for a cluster).

1. The app makes its own ssh key the first time (`ssh-keygen`, ed25519, kept in
   its data folder).
2. It connects as you once, with your own ssh access, and runs
   `rack pair --json --key '<its public key>' --name <the app>`.
3. `rack pair` puts that key in `~/.ssh/authorized_keys` as

   ```
   command="/home/you/.local/bin/rack remote",restrict ssh-ed25519 AAAA... bytebunker:<app>
   ```

   and prints what the app needs: this node's name and platform, its addresses
   (tailnet and LAN), the engine's port and key and what it serves, the
   monitor's port and token, and which apps are paired. The app pins the host's
   key the first time it sees it.
4. From then on the app uses only its own key. Whatever command it sends,
   sshd runs `rack remote`, which runs it only if it is one of rack's own
   commands below; anything else is refused and written to
   `~/.local/state/dgx-serve/remote.log`, like every command an app ran.

If your own ssh needs a password the app cannot type, run the printed command
on the machine yourself (`rack pair --key '...' --name ...`), then pair again:
the app connects with its own key.

## What an app's key may run

| Command | Why |
|---|---|
| `version`, `platform`, `status`, `preflight`, `models`, `net` | read the rack |
| `recipes`, `recipes show <name>`, `recipes check` | the Deploy screen |
| `fit`, `pull`, `up`, `down`, `logs`, `bench` | serve, stop, watch |
| `nodes [ls\|test]`, `gateway [status\|sync]`, `monitor status\|up` | the rest of the picture |
| `pair --json` | read the pairing again (a new engine key, a monitor that came up) |

Never: a shell, `--on` (another machine), `init`, `nodes add\|rm`, `new`,
`build`, `install`, `monitor token`, `gateway remove\|adopt`, `pair --key`
(another key), `unpair`. Words are letters, digits and `. _ : / = @ + , -`
only, so nothing reaches a shell. `restrict` also turns off port, agent and X11
forwarding and the terminal.

## Taking it back

```
rack unpair --name <app>      that app's key
rack unpair                   every app's key
```

Your own lines in `authorized_keys` are never touched.
