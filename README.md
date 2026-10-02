# psn-stock-bot

A Telegram bot that watches **PlayStation Store (PSN) gift cards on Amazon India** and messages you
(and your friends or group) as soon as one comes back in stock.

You run it yourself, on your own computer or any always-on machine. This guide walks you through it
step by step, no experience needed.

> **Note:** the bot's messages and buttons in Telegram are in **Hebrew**. This guide and the settings are in
> English. All the texts live in `bot.py` if you want to translate them.

---

## What it does

- 🔔 **Restock alert** with a "buy on Amazon" button the moment a gift card is back in stock. If several amounts
  come back at once, you get one combined message.
- ❌ **Sold-out notice** saying how long the item stayed in stock.
- 🔁 **Reminders** while it's still in stock (off / every 10 min / every 30 min, max 6).
- 🎯 **Several amounts**: 1000, 2000, 3000, 4000 and 5000 ₹ by default. You can add or remove products in `products.json`.
- 📋 **Menu buttons**: current status, "check now", stock history and Amazon success rate for the last 24 hours.
- 👥 Works in **private chats and groups**.
- 🔒 **Only you (the owner)** can change settings.
- 🛡️ **Optional private mode**: keep the bot to your own group(s) and their members.

### How it works (short version)

Every 5 minutes (configurable) the bot opens each product page on amazon.in and checks whether it can be
bought. When an item goes from "not available" to "in stock", every chat that pressed `/start` gets an alert.
Sometimes Amazon answers with a "robot check" page instead. The bot then retries a couple of times and
**never** sends an alert based on a blocked check.

The bot talks to Telegram with *long polling*: it only makes outgoing connections. You don't need a domain,
a public IP, open ports or HTTPS, so a home PC or laptop works fine (it just has to stay on).

---

## 1. Prerequisites

You need **one** of these:

- **Option A: Python 3.12 or newer.** Download it from [python.org](https://www.python.org/downloads/).
  On Windows, tick **"Add python.exe to PATH"** in the installer.
- **Option B: Docker** with Docker Compose ([Docker Desktop](https://www.docker.com/products/docker-desktop/) on
  Windows/macOS, or Docker Engine on Linux).

You also need [Git](https://git-scm.com/downloads), or just download the code as a ZIP from GitHub
(**Code → Download ZIP**) and unzip it.

## 2. Create your bot and get its token

1. In Telegram, open [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, then pick a display name and a username that ends in `bot`.
3. BotFather replies with a **token** that looks like `123456789:AAH...`. Copy it.
   **Keep it secret**: anyone with the token controls your bot.

## 3. Get your Telegram user id

Open [@userinfobot](https://t.me/userinfobot) in Telegram and press **Start**. It replies with your numeric
**Id** (for example `123456789`). This makes you the bot's owner.

## 4. Download the code

```bash
git clone https://github.com/d0raka/psn-stock-bot.git
cd psn-stock-bot
```

## 5. Create your `.env` settings file

Copy the example file to `.env`:

| System | Command |
|---|---|
| macOS / Linux | `cp .env.example .env` |
| Windows (PowerShell) | `Copy-Item .env.example .env` |
| Windows (Command Prompt) | `copy .env.example .env` |

Open `.env` in any text editor (Notepad, TextEdit, VS Code...) and fill in at least these two lines:

```ini
TELEGRAM_BOT_TOKEN=123456789:AAH...your-token...
OWNER_ID=123456789
```

Everything else is optional; see [Settings](#settings). Never share or upload your `.env` file.

---

## 6. Run the bot

### Option A: with Python

**macOS / Linux**

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python bot.py
```

**Windows (PowerShell)**

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python bot.py
```

> If PowerShell says running scripts is disabled, run
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, or use Command Prompt and run
> `.venv\Scripts\activate.bat` instead.

The bot reads `.env` automatically. When it's running you'll see lines like
`Telegram connected as @your_bot` and `check(poll) 1000 [...] result=OUT`.
Leave the window open: closing it (or pressing `Ctrl+C`) stops the bot.

Next time, just activate the virtual environment again (`source .venv/bin/activate` or
`.venv\Scripts\Activate.ps1`) and run `python bot.py`.

### Option B: with Docker Compose

```bash
docker compose up -d --build
```

That's it. The bot runs in the background and restarts automatically (also after a reboot, as long as Docker
starts). Useful commands:

```bash
docker compose logs -f        # watch the log (Ctrl+C to stop watching)
docker compose restart        # restart, e.g. after editing products.json
docker compose down           # stop the bot
```

After changing `.env`, run `docker compose up -d` again so the new settings are picked up.

---

## 7. First use

1. Open your bot in Telegram (search for its username) and press **Start** (`/start`).
   You'll get a welcome message with the menu, and alerts will now come to this chat.
2. **Use it in a group (optional):** add the bot to the group. It posts a welcome message with menu
   buttons that work for everyone, and it's worth pinning. Alerts now also go to the group.
3. Open **⚙️ Settings** in your **private** chat with the bot to change reminders, mute chats or turn
   individual amounts on and off. Other people see the settings read-only.

To find a group's id (needed for `ALLOWED_GROUPS`), send `/start` in the group and look in the bot's log for
a line like `CHAT_ID=-1001234567890`.

---

## Settings

All settings go in `.env` (real environment variables work too, and take priority).

| Setting | Default | What it does |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *(empty)* | Token from @BotFather. If empty, the bot only checks Amazon and writes the results to its log (*checker-only mode*). |
| `OWNER_ID` | *(empty)* | Your Telegram user id. Only the owner can change settings (from a private chat with the bot), and only the owner gets a warning when Amazon blocks every check 6 cycles in a row. If empty, settings are read-only for everyone. |
| `ALLOWED_GROUPS` | *(empty)* | Group ids separated by commas, e.g. `-1001234567890`. **Empty = open:** anyone can use the bot and add it to any group. **Set = private:** the bot leaves any other group or channel, and in private chat it only answers members of these groups (and you). |
| `GROUP_INVITE` | *(empty)* | Invite link to your group (`https://t.me/+...`). Adds "share the group" buttons and the `/share` command. |
| `CHAT_ID` | *(empty)* | A chat id that gets alerts from the very first start, before anyone presses `/start`. |
| `BOT_USERNAME` | *(automatic)* | Your bot's username without `@`. Only needed if automatic detection fails. |
| `POLL_SECONDS` | `300` | Seconds between checks (minimum 60). Please keep it gentle. |
| `CHECK_ATTEMPTS` | `3` | How many times to try a product per check when Amazon shows a robot page. |
| `AMAZON_PROXY` | *(empty)* | Proxy used only for Amazon, e.g. `socks5h://user:pass@host:1080`. |
| `BOT_TIMEZONE` | `Asia/Jerusalem` | Timezone for the times shown in messages ([list](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones)). |
| `DATA_DIR` | `data` folder next to `bot.py` | Where the bot keeps `state.json`, `bot.log`, `amazon_cookies.json` and `products.json`. |
| `PRODUCTS_FILE` | `<data folder>/products.json` | Use a different product list file. |

## Commands

| Command | Who can use it | What it does |
|---|---|---|
| `/start` | everyone | Subscribe this chat to alerts and show the menu |
| `/menu` | everyone | Show the main menu |
| `/status` | everyone | Stock status of every tracked amount |
| `/check` | everyone | Check Amazon right now (at most once a minute) |
| `/history` | everyone | Recent stock changes and Amazon success rate for the last 24 hours |
| `/help` | everyone | How the bot works |
| `/share` | everyone | Promo message for your group (only if `GROUP_INVITE` is set) |
| `/settings` | owner (read-only for others) | Reminders, which amounts to track, other chats |
| `/mute`, `/unmute` | owner | Turn alerts off or on in the current chat |

The bot registers this command list with Telegram by itself, so it shows up in the `/` menu.

## products.json

`data/products.json` is the list of Amazon.in products to watch. If it's missing, the bot creates it with the
five default PSN gift cards.

```json
[
  { "asin": "B07K6RYVHR", "amount": 1000, "label": "1000 ₹", "enabled": true },
  { "asin": "B07K6RYVJ5", "amount": 4000, "label": "4000 ₹", "enabled": true }
]
```

| Field | Meaning |
|---|---|
| `asin` | The 10-character product code from the Amazon link: `https://www.amazon.in/dp/`**`B07K6RYVJ5`** |
| `amount` | The number shown in messages. The list is sorted by it. |
| `label` | Display text (optional). |
| `enabled` | `true` to watch it, `false` to skip it. The owner can also toggle amounts from ⚙️ Settings. |

Restart the bot after editing the file.

---

## Keeping it running and updating

- The bot only works while it's running. For 24/7 alerts, use a computer that stays on (an old laptop,
  a Raspberry Pi or a small cloud server). Docker (Option B) restarts it automatically.
- Your data lives in the `data` folder (`state.json` = subscribers and history). Keep it when updating.
- Run **only one copy** of the bot per token.

**Update to the latest version**

```bash
git pull
# Option A (Python):
pip install -r requirements.txt
python bot.py
# Option B (Docker):
docker compose up -d --build
```

## Troubleshooting

**"Amazon blocked the check" / log shows `robot page` or `Amazon BLOCKED all products ...`**
Amazon sometimes shows a robot check, more often to cloud/datacenter IP addresses. The bot retries
automatically and never sends false alerts, so an alert may just come a few minutes late. If it keeps
happening, increase `POLL_SECONDS` (e.g. `600`), run the bot from a home connection, or set `AMAZON_PROXY`.
If the log says `IMAGE captcha variant`, Amazon wants a picture CAPTCHA. The bot doesn't solve those; it
starts a fresh session and tries again next cycle. Deleting `data/amazon_cookies.json` also resets the session.

**The bot doesn't reply at all**
- Check the log: `Telegram connected as @...` means the token works.
- If the log says `telegram=NO (checker-only)`, the token isn't set. Make sure `.env` is in the same folder as
  `bot.py` (for Docker, next to `docker-compose.yml`) and the line is exactly `TELEGRAM_BOT_TOKEN=...` with no spaces.
- If `ALLOWED_GROUPS` is set, the bot ignores people who aren't members of those groups and leaves other groups.
  That's expected in private mode.

**Token errors (`Unauthorized`, `Not Found`)**
The token is wrong or was revoked. Copy it again from @BotFather (`/mybots` → your bot → *API Token*), paste it
into `.env` and restart.

**`Conflict: terminated by other getUpdates request`**
Another copy of the bot is running with the same token (maybe an old window or another computer). Stop it.

**`Conflict: can't use getUpdates method while webhook is active`**
The token has a webhook set from somewhere else. Open
`https://api.telegram.org/bot<YOUR_TOKEN>/deleteWebhook` once in your browser, then restart the bot.

**I get no alerts**
- Alerts only go out when an item *changes* to in stock. Use `/status` to see the current state.
- The chat may be muted (🔕 on the menu). The owner can unmute it with `/unmute` or from ⚙️ Settings.
- Make sure the chat pressed `/start`, or the bot was added to the group.

**Settings say "only the admin can change settings"**
Set `OWNER_ID` to your own user id (from @userinfobot), restart, and open Settings in your **private** chat with the bot.

---

## Disclaimer

This project is not affiliated with, endorsed by or sponsored by Amazon or Sony Interactive Entertainment.
"PlayStation" and "Amazon" are trademarks of their respective owners. Automated access to amazon.in may be
restricted by Amazon's Conditions of Use. You're responsible for how you use this software. Keep checks
infrequent, use it for personal alerts only, and respect the site's terms and your local laws. The bot never
logs into an Amazon account and never solves image CAPTCHAs.

## License

[MIT](LICENSE) © 2026 Dor Aka
