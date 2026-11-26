# /trader: trading superpowers for Claude

Built by [Brian MacDonald](https://github.com/bmacbuilds).

Two ways to get it. Pick the one that matches where you use Claude.

**In the Claude app (phone, web or desktop):**
1. Download [trader-claude-app.zip](https://github.com/traderskill/claude/releases/latest/download/trader-claude-app.zip).
2. In Claude, open **Customize > Skills**, tap **+**, then **Create skill > Upload a skill**, and pick the file. Code execution must be on in your settings.
3. In any chat, type **`/trader`**.

**In Claude Code on a Mac or Linux computer (the full version, with saved history):** paste this one line and press Enter:

```
! curl -fsSL https://raw.githubusercontent.com/traderskill/claude/main/install.sh | bash
```

Then type **`/trader`**. That's it: pick what you want to do and go. Nothing else needed: no Python, no Git, no account, no API key.

In the Claude app, results last for that chat, and downloading prices needs network access turned on for code execution in your settings; otherwise use web research, your own CSV file or the practice data.

Using ChatGPT instead? Paste [github.com/traderskill/chatgpt](https://github.com/traderskill/chatgpt) into a ChatGPT chat.

## What /trader gives Claude

- **Test any trading claim**: paste a tip, a tweet or your own idea. /trader researches it on the web, tests it on real prices after trading costs, has a skeptic attack it, and gives a verdict with receipts.
- **Research any market on the web**: what is moving a stock, the news, an earnings reaction, with dated sources.
- **Test a strategy idea** on real prices for any US stock or ETF (free from Yahoo Finance, fetched for you), your own CSV file or the built-in practice data, with trading costs, a held-back test period and the checks that catch a lucky result.
- **Get the market picture** and scan for setups on data you hold.
- **Read the filings**: quarterly earnings, a fund's 13F holdings and a fundamentals screen, straight from the SEC.
- **Size a trade and check its risk** before you place anything.
- **Keep the evidence**: every result is saved on your computer, so you can reopen it and reproduce it.

Ask in plain words: "what is moving NVDA this week?", "test a strategy that buys pullbacks", "show me the market", "what did Apple earn last quarter?". /trader never invents a number. When the evidence is thin, it says so.

Free and open source. It runs on your computer, with no account, no sign-up and no API key. **Research only, not investment advice. Paper trading only: it never trades real money.**

---

## How the install works

The line above runs [install.sh](install.sh), a short script you can read first. It:
- installs uv (Astral's official installer) into `~/.local/bin`, only if you do not have it; uv sets up Python 3.12;
- downloads the current release (v2.0.0) of this repository to `~/.traderos-app` and installs the `traderos` command into `~/.local/bin`;
- copies the skill to `~/.claude/skills/trader`, so `/trader` works in every Claude Code session.

It needs no admin password and changes nothing else. Run it again to update. If `traderos` is not found later, add `export PATH="$HOME/.local/bin:$PATH"` to your `~/.zshrc`.

---

## Safety

- **Paper only.** /trader can send an order only to an Alpaca paper-trading account, with your own paper keys and your yes for each order. Live trading is refused in its code.
- **Not advice.** Every result is research. Nothing is a recommendation, a forecast or a promise.
- **Your data stays with you.** Everything is saved in a folder on your computer. Nothing is sent anywhere unless you ask for web research, prices, SEC filings or a paper order. Prices come free from Yahoo Finance and are unofficial: every result says so.

## For developers

Under the hood, /trader runs a local engine called TraderOS (`traderos`, Python 3.12 or newer, no runtime dependencies; the install above gets Python 3.12 through uv). The skill lives in `.claude/skills/trader`. Run `traderos help` for every command, and `traderos capabilities` for what this build enables.

```sh
pip install git+https://github.com/traderskill/claude
traderos init
traderos demo
```

`traderos demo` runs the whole lab on made-up tutorial data. Its numbers mean nothing about any real market. The SEC tools need your contact address once: `traderos config set sec_user_agent "Your Name you@example.com"`. Exporting and importing research comes in a later update.

## License

MIT. See [LICENSE](LICENSE).
