# 🌞 Claude ICA — Designer Quickstart

**What you're getting:** your own AI teammate in the terminal — it reads FigJam
boards, digs through Jira and Confluence, summarizes anything, and runs on the
**company's ICA** (your IBMDT access, not a personal subscription). Setup takes
about 15 minutes, once, ever.

> ไม่ต้องเป็นสายเทคนิค — ทุกขั้นตอนมีบอกไว้หมดแล้ว ทำตามทีละข้อได้เลย~

---

## Before you start ✅

- [ ] You're on the **company network / VPN** (needed to reach ICA)
- [ ] Your **ICA API key**: open the ICA website → log in with your IBMDT email → copy your key
- [ ] ~15 minutes and one coffee ☕

---

## Step 0 — Open Terminal *(30 sec)*

Press `Cmd + Space`, type **Terminal**, press Enter.

A plain text window opens. That's it — that's the whole scary part. เก่งมาก~

---

## Step 1 — Apple's toolbox *(5–10 min, mostly waiting)*

Copy-paste this and press Enter:

```
xcode-select --install
```

A macOS dialog pops up → click **Install** → accept → wait. This installs
Apple's official developer basics (git + python). One time only.

**Check it worked** — paste this; if you see a version number, you're done:

```
git --version
```

### 🚑 Step 1 rescue guide (ถ้ามันงอแง — อาการยอดฮิตทั้งนั้น)

**"Can't install the software because it is not currently available from the Software Update server"**
→ Two possible causes, try in order:

1. **VPN blocking Apple's servers** — disconnect the VPN (or hop onto normal
   Wi-Fi / phone hotspot) *just for this step*, run `xcode-select --install`
   again, then reconnect the VPN afterwards.
2. **Still failing off-VPN? Your Mac is company-managed** (Company Portal /
   Intune) and its update catalog points at the company's server, which
   doesn't carry these tools. The reliable bypass — download directly from
   Apple instead:
   - Go to **developer.apple.com/download/all** (sign in with any Apple ID —
     free account is fine, your personal one works)
   - Search **"Command Line Tools for Xcode"** → download the newest `.dmg`
   - Open it and install like any normal app
   - Verify with `git --version`

⚠️ Note: **installing Homebrew won't fix this** — Homebrew itself needs these
same tools underneath. Solve this step first; everything else follows.

**No dialog appears at all**
→ Just run `git --version` — macOS offers to install the tools when anything
needs them. Still nothing? Run the install command once more; the dialog is shy
sometimes.

**"Command line tools are already installed" — but `git --version` still fails**
→ The previous install is half-broken. Run:
```
sudo xcode-select --reset
```
(it will ask for your Mac password — typing shows nothing, that's normal),
then try `xcode-select --install` again.

**Download says 20+ hours remaining**
→ Apple's servers being moody. Cancel, try again later (or off VPN — see above).
It's normally a few minutes.

**Company Mac says installation is not allowed**
→ That's the IT policy (MDM) blocking it — this one needs an IT ticket, not a
workaround. Tell whoever gave you this guide so they know it happens.

---

## Step 2 — Your ICA key *(1 min)*

Open the ICA website → log in with your **IBMDT email** → copy your API key.
Keep it handy for the next step. (Your key stays on your Mac only.)

---

## Step 3 — The one-liner *(4 min)*

Make sure you're **back on the company network/VPN**, then paste:

```
curl -fsSL https://raw.githubusercontent.com/teer823/claude-proxy/main/kit/install.sh | bash
```

The installer narrates its 9 steps. You only do two things:

1. **Paste your ICA key** when asked (typing shows nothing — that's a security
   feature, not a bug~)
2. **Answer 5 fun questions** to design your AI buddy: your name, your role,
   the buddy's name, personality, cuteness level. No wrong answers.

At the end you should see: `ICA answered: "WELCOME ABOARD" 🎉`
If you see a warning instead, it tells you exactly what to check (usually VPN
or the key).

---

## Step 4 — First hello *(1 min)*

**Open a NEW Terminal window** (`Cmd + N` — important! old windows don't know
the new command), then type:

```
claude-ica
```

Your buddy wakes up and says hi. ทักน้องได้เลย~

---

## Step 5 — Connect Figma & Atlassian *(3 min, once ever)*

Inside the session, type:

```
/mcp
```

Authenticate each of these — every one opens your browser for a normal login:

| Select | Log in with | Note |
|---|---|---|
| `figma` | your Figma account | just Allow |
| `atlassian-ktbinnovation` | your Atlassian account | pick site **ktbinnovation** |
| `atlassian-krungthaibank` | same account | pick site **krungthaibank** |

Done. Your buddy can now see FigJam, Figma, Jira, and Confluence.

---

## Try these first 🎨

> Get the contents of this FigJam board and summarize what's on it: *(paste a FigJam link)*

> ดึง Jira issue ล่าสุด 5 ตัวจาก project *(ชื่อ project)* มาสรุปให้หน่อยว่าทีมกำลังทำอะไรกัน

> Search Confluence for pages about *(topic)* and give me the highlights

---

## When something acts weird 🔧

| Symptom | Fix |
|---|---|
| `command not found: claude-ica` | You're in an old Terminal window — open a new one (`Cmd + N`) |
| Worked yesterday, now API Error 500 | `claude-ica restart` — fixes it in 5 seconds |
| Anything else strange | Re-run the installer from Step 3 — it repairs itself, never breaks what works |
| Want a different buddy personality | `bash ~/claude-proxy/kit/setup-buddy.sh` — redesign anytime (old buddy gets backed up) |

---

## Good to know 💡

- **Data boundary:** this runs on company ICA — same rules as any company AI
  tool. You already know what belongs there and what doesn't~
- **One session = one mission** works best: finish a topic, ask your buddy to
  summarize into a file, start fresh for the next topic. Long sessions get
  slower.
- Your buddy's personality lives in `~/.claude/CLAUDE.md` — it's just a text
  file, and it's yours.

*Work is hard. This should be fun.* 🌞
