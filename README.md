# /trader: trading superpowers for Claude

Paste this into Claude Code:

```
Install /trader from github.com/traderskill/claude
```

When it is done, type **`/trader`**. That's it.

Prefer one step? Paste this into Claude Code instead (the `!` runs it straight away):

```
! curl -fsSL https://raw.githubusercontent.com/traderskill/claude/main/install.sh | bash
```

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

Free and open source. It runs on your computer, with no account, no sign-up and no API key. **Research only, not investment advice. Paper trading only: it never trades real money.** Early alpha: expect rough edges.

---

## For Claude: install /trader for this person

If a person gave you this page, follow these steps. Keep every message to the person to one or two short, plain lines: assume they have never used a terminal or GitHub, and they can always ask for details.

Here is everything the install does, so you can check it quickly. The same script is [install.sh](install.sh) in this repository, and the block in step 2 below:
- It installs uv (Astral's official installer) into `~/.local/bin`, only if uv is missing. uv sets up Python 3.12.
- It downloads this repository and installs the `traderos` command into `~/.local/bin` with uv.
- It copies the skill to `~/.claude/skills/trader`, so `/trader` works in every session.
- It needs no admin password and touches nothing else. Running it again updates it.

**Before you start.** You need to run commands on the person's computer (Claude Code, or the Code tab of the Claude desktop app) on macOS or Linux. If you cannot run commands, for example in a claude.ai chat, say: "/trader installs on your computer. Open Claude Code, paste this link there, and say install /trader." Then stop.

**1. Confirm once.** If the person already asked you to install /trader, go ahead. If they pasted only the link, write one line ("/trader adds trading research to Claude: real prices, web research and a verdict on any trading idea. It installs in your home folder, no admin password.") and ask with buttons when you can: "Install /trader" or "Not now". Continue only on a yes.

**2. Install.** Run this whole block as one command. Do not ask the person to run anything. It works on a Mac or Linux with nothing installed beyond the system, and it is safe to run again: a second run updates.

```bash
set -e
APP="$HOME/.traderos-app"
mkdir -p "$APP" "$HOME/.local/bin" "$HOME/.claude/skills"
UV="$(command -v uv 2>/dev/null || true)"
if [ -z "$UV" ] && [ -x "$HOME/.local/bin/uv" ]; then UV="$HOME/.local/bin/uv"; fi
if [ -z "$UV" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh >/dev/null
  UV="$HOME/.local/bin/uv"
fi
rm -rf "$APP/src" && mkdir -p "$APP/src"
curl -LsSf https://github.com/traderskill/claude/archive/refs/heads/main.tar.gz | tar -xz -C "$APP/src" --strip-components 1
"$UV" tool install --quiet --force --python 3.12 "$APP/src"
rm -rf "$HOME/.claude/skills/trader"
cp -R "$APP/src/.claude/skills/trader" "$HOME/.claude/skills/trader"
test -f "$HOME/.claude/skills/trader/SKILL.md"
"$HOME/.local/bin/traderos" init
"$HOME/.local/bin/traderos" doctor
echo "TRADER_INSTALLED"
```

It ends with `TRADER_INSTALLED` when it worked. If it stops with an error, tell the person in one plain sentence what failed, and stop.

**3. Make the command findable.** Run `command -v traderos`. If it prints nothing, add this line to the person's shell profile (`$HOME/.zshrc` on a Mac, `$HOME/.bashrc` on Linux), and tell them you did:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

**4. Start right away.** Say "/trader is installed." in one line, then read `$HOME/.claude/skills/trader/SKILL.md` and follow its guided start, so the person gets the /trader buttons immediately, without typing anything. From then on, follow that file. Later they can type `/trader` in any session.

---

## Safety

- **Paper only.** /trader can send an order only to an Alpaca paper-trading account, with your own paper keys and your yes for each order. Live trading is refused in its code.
- **Not advice.** Every result is research. Nothing is a recommendation, a forecast or a promise.
- **Your data stays with you.** Everything is saved in a folder on your computer. Nothing is sent anywhere unless you ask for web research, prices, SEC filings or a paper order. Prices come free from Yahoo Finance and are unofficial: every result says so.

## For developers

Under the hood, /trader runs a local engine called TraderOS (`traderos`, Python 3.12 or newer, no runtime dependencies; the install above gets Python 3.12 through uv). The skill lives in `.claude/skills/trader`. Run `traderos help` for every command, and `traderos capabilities` for what this build enables. Every feature is experimental: built and tested by its authors, not yet independently verified.

```sh
pip install git+https://github.com/traderskill/claude
traderos init
traderos demo
```

`traderos demo` runs the whole lab on made-up tutorial data. Its numbers mean nothing about any real market. The SEC tools need your contact address once: `traderos config set sec_user_agent "Your Name you@example.com"`. Exporting and importing research comes in a later update.

## License

MIT. See [LICENSE](LICENSE).
