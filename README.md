# printer2telegram

A Telegram bot that turns a multifunction printer into a chat
contact: send it a PDF or a photo and it prints, send `/scan` and it
answers with a PDF of whatever is in the document feeder (or on the
flatbed). Printing goes through CUPS, scanning through SANE — network
scanners that speak eSCL/AirScan work driverless.

The bot speaks German.

## Features

- **Print**: send a PDF, JPEG or PNG (as document or photo).
  Options as message text or caption: `2x` (copies), `beidseitig` /
  `einseitig`, `schwarzweiss` / `farbe`, `a4` / `a5` / `letter`.
- **Scan**: `/scan`, with options like `600dpi`, `grau`, `farbe`.
  Uses the ADF if it holds paper, the flatbed otherwise; multi-page
  ADF scans become one PDF.
- **`/einstellungen`**: interactive menu for per-chat defaults
  (duplex, black/white, paper size, scan resolution, grayscale).
- **`/status`**: CUPS printer state and the bot's job queue.
- **Access control**: only whitelisted Telegram user ids are served.
  When an unknown user writes, every admin gets an
  Erlauben/Ablehnen button; approvals are persisted in `state.yaml`.
- **Robustness**: jobs are queued and run one at a time; a busy
  device is retried with backoff (15s … 5min) before giving up.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and note
   the token.
2. `cp config.example.yaml config.yaml` and fill in token, CUPS
   printer name (`lpstat -p`) and your Telegram user id (message the
   bot once and read the log, or ask
   [@userinfobot](https://t.me/userinfobot)).
3. For a network scanner with eSCL/AirScan (most modern
   multifunction devices), no driver is needed:

   ```yaml
   scanner: "escl:http://192.168.1.2:80"
   ```

   Otherwise set any SANE device name (`scanimage -L`), or omit
   `scanner` to use the first device SANE finds.

Run it:

```console
$ nix run .            # uses ./config.yaml
$ nix run . -- /path/to/config.yaml
```

## NixOS module

```nix
{
  inputs.printer2telegram.url = "github:AWildLeon/printer2telegram";

  # in your nixosConfiguration:
  imports = [ inputs.printer2telegram.nixosModules.default ];

  services.printer2telegram = {
    enable = true;
    # keep this file out of the nix store, it contains the token;
    # root-only permissions are fine (loaded as a systemd credential)
    configFile = "/etc/printer2telegram/config.yaml";
  };
}
```

The service runs as a `DynamicUser` with its state (approved users,
per-chat settings) in `/var/lib/printer2telegram`. CUPS must be
running on the same host (`services.printing.enable = true`).

## Development

```console
$ nix develop          # python with all dependencies
$ nix build            # also runs flake8 over main.py
```

## License

[MIT](LICENSE)
