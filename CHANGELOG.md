# Changelog

All notable changes to this project are documented here.

## [1.0.0] - 2026-10-02

First public release.

### Features
- Tracks any number of Amazon.in PSN gift-card listings from `products.json` (1000 / 2000 / 3000 / 4000 / 5000 ₹ by default).
- Restock alert with buy buttons; one combined message when several amounts restock in the same cycle.
- "Sold out again" notice with how long the item stayed in stock; optional reminders while it is still in stock (off / 10 / 30 min, up to 6).
- Inline menu (Hebrew UI): status table, check-now (rate-limited to once a minute), history with 24-hour request success rate, help.
- Multi-chat subscribers (private chats and groups), supergroup migration handling, auto-unsubscribe when the bot is blocked or removed.
- Owner-only settings (`OWNER_ID`): mute, reminders, per-amount tracking toggles, controls for other subscribed chats.
- Optional restricted mode (`ALLOWED_GROUPS`): the bot stays only in allowed groups and private access requires group membership.
- Optional group sharing (`GROUP_INVITE`): share link, copy-text button and `/share`.
- Gentle Amazon polling: persistent Chrome-impersonated session, staggered requests, retry budget for robot pages, optional proxy.
- Checker-only mode when no token is set; token and proxy are redacted from logs.
- Reads settings from a `.env` file next to `bot.py` (or from real environment variables).
